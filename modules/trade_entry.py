"""
Module: trade_entry.py
Handles inbound trade signals, validates entry gates,
calculates contracts, selects strikes, and submits orders to Tradier.

Signal intake is via Telegram command for V1.
Webhook integration with TradingView V9 is reserved for V2.
"""

import logging
import uuid
import json
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
import pytz

from config import SPREAD_WIDTH_PTS, RISK_PCT_PER_TRADE
from core.database import insert_spread, insert_leg, get_or_create_daily_state
from core.tradier import get_option_chain, place_spread_order, get_spx_price, get_vix
from core.risk_manager import (
    check_entry_gates, calculate_contracts, assign_tiers, EntryGateResult
)

logger = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")
ET = pytz.timezone("America/New_York")


def _today() -> str:
    return datetime.now(PT).strftime("%Y-%m-%d")


def _today_expiry() -> str:
    """0DTE — expiry is today."""
    return _today()


# ─────────────────────────────────────────────────────────────────────────────
# STRIKE SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def _find_best_strike(
    chain: List[Dict],
    option_type: str,   # 'call' | 'put'
    direction: str,     # 'above' | 'below'  (relative to current price)
    spx_price: float,
    target_delta: float = 0.15,
    min_delta: float = 0.10,
    max_delta: float = 0.25,
) -> Optional[Dict]:
    """
    Find the short leg strike with delta closest to target_delta,
    constrained to min/max delta range and correct direction.

    For Bear Call: calls above current price, delta 0.10–0.25
    For Bull Put:  puts below current price, delta -0.10 to -0.25 (abs)
    """
    candidates = [
        o for o in chain
        if o["type"] == option_type
        and min_delta <= abs(o.get("delta", 0)) <= max_delta
        and (
            (direction == "above" and o["strike"] > spx_price)
            or (direction == "below" and o["strike"] < spx_price)
        )
        and o.get("bid", 0) > 0.10  # minimum liquidity filter
    ]

    if not candidates:
        logger.warning("No valid %s candidates found for delta %.2f", option_type, target_delta)
        return None

    # Sort by distance from target delta
    candidates.sort(key=lambda o: abs(abs(o.get("delta", 0)) - target_delta))
    return candidates[0]


def select_ic_strikes(
    spx_price: float,
    chain_calls: List[Dict],
    chain_puts: List[Dict],
) -> Optional[Dict]:
    """
    Select Iron Condor strikes:
    - Short call: delta ~0.15 above market
    - Long call:  SPREAD_WIDTH_PTS above short call
    - Short put:  delta ~0.15 below market
    - Long put:   SPREAD_WIDTH_PTS below short put
    """
    short_call = _find_best_strike(chain_calls, "call", "above", spx_price, 0.15)
    short_put  = _find_best_strike(chain_puts,  "put",  "below", spx_price, 0.15)

    if not short_call or not short_put:
        return None

    long_call_strike = short_call["strike"] + SPREAD_WIDTH_PTS
    long_put_strike  = short_put["strike"]  - SPREAD_WIDTH_PTS

    long_call = next(
        (o for o in chain_calls if o["strike"] == long_call_strike and o["type"] == "call"), None
    )
    long_put = next(
        (o for o in chain_puts if o["strike"] == long_put_strike and o["type"] == "put"), None
    )

    if not long_call or not long_put:
        logger.warning(
            "Could not find long legs at strikes %.0f / %.0f",
            long_call_strike, long_put_strike
        )
        return None

    return {
        "short_call": short_call,
        "long_call":  long_call,
        "short_put":  short_put,
        "long_put":   long_put,
    }


def select_bear_call_strikes(
    spx_price: float, chain_calls: List[Dict]
) -> Optional[Dict]:
    short_call = _find_best_strike(chain_calls, "call", "above", spx_price, 0.15)
    if not short_call:
        return None

    long_call_strike = short_call["strike"] + SPREAD_WIDTH_PTS
    long_call = next(
        (o for o in chain_calls if o["strike"] == long_call_strike and o["type"] == "call"), None
    )
    if not long_call:
        return None
    return {"short_call": short_call, "long_call": long_call}


def select_bull_put_strikes(
    spx_price: float, chain_puts: List[Dict]
) -> Optional[Dict]:
    short_put = _find_best_strike(chain_puts, "put", "below", spx_price, 0.15)
    if not short_put:
        return None

    long_put_strike = short_put["strike"] - SPREAD_WIDTH_PTS
    long_put = next(
        (o for o in chain_puts if o["strike"] == long_put_strike and o["type"] == "put"), None
    )
    if not long_put:
        return None
    return {"short_put": short_put, "long_put": long_put}


# ─────────────────────────────────────────────────────────────────────────────
# CREDIT CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calc_net_credit(strikes: Dict, setup_type: str) -> float:
    """Net credit received per spread (before multiplier)."""
    if setup_type == "IC":
        short_credit = strikes["short_call"]["bid"] + strikes["short_put"]["bid"]
        long_debit   = strikes["long_call"]["ask"]  + strikes["long_put"]["ask"]
    elif setup_type == "BEAR_CALL":
        short_credit = strikes["short_call"]["bid"]
        long_debit   = strikes["long_call"]["ask"]
    else:  # BULL_PUT
        short_credit = strikes["short_put"]["bid"]
        long_debit   = strikes["long_put"]["ask"]

    return round(max(short_credit - long_debit, 0), 2)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def enter_trade(
    setup_type: str,    # 'IC' | 'BEAR_CALL' | 'BULL_PUT'
    signal_grade: str,  # 'A+' | 'A'
    is_news_day: bool,
    daily_state: Dict,
    account_equity: float,
    total_open_positions: int,
) -> Tuple[bool, str]:
    """
    Full entry flow:
      1. Entry gate check
      2. Fetch chain + select strikes
      3. Calculate contracts
      4. Place order
      5. Persist spread + legs to DB

    Returns (success: bool, message: str)
    """
    # ── 1. Entry gates ──────────────────────────────────────────────────────
    vix = get_vix() or 0.0
    gate = check_entry_gates(vix, signal_grade, is_news_day, daily_state)
    if not gate.allowed:
        logger.info("Entry blocked: %s", gate.reason)
        return False, str(gate)

    # ── 2. Market data ───────────────────────────────────────────────────────
    spx_price = get_spx_price()
    if not spx_price:
        return False, "Could not fetch SPX price"

    expiry    = _today_expiry()
    chain_all = get_option_chain(expiry, "all")
    if not chain_all:
        return False, "Could not fetch option chain"

    chain_calls = [o for o in chain_all if o["type"] == "call"]
    chain_puts  = [o for o in chain_all if o["type"] == "put"]

    # ── 3. Strike selection ──────────────────────────────────────────────────
    if setup_type == "IC":
        strikes = select_ic_strikes(spx_price, chain_calls, chain_puts)
    elif setup_type == "BEAR_CALL":
        strikes = select_bear_call_strikes(spx_price, chain_calls)
    elif setup_type == "BULL_PUT":
        strikes = select_bull_put_strikes(spx_price, chain_puts)
    else:
        return False, f"Unknown setup_type: {setup_type}"

    if not strikes:
        return False, f"Could not find valid strikes for {setup_type}"

    # ── 4. Credit + contract sizing ──────────────────────────────────────────
    net_credit = calc_net_credit(strikes, setup_type)
    if net_credit < 0.30:
        return False, f"Net credit ${net_credit:.2f} too low — min $0.30 required"

    consecutive_wins = daily_state.get("consecutive_win_days", 0)
    contracts = calculate_contracts(account_equity, consecutive_wins, vix=vix)

    # ── 5. Build legs for order ──────────────────────────────────────────────
    order_legs = []
    db_legs    = []
    spread_id  = str(uuid.uuid4())

    def _add_leg(option: Dict, side_open: str, leg_type: str):
        order_legs.append({
            "symbol":   option["symbol"],
            "side":     side_open,
            "quantity": contracts,
        })
        db_legs.append({
            "id":             str(uuid.uuid4()),
            "spread_id":      spread_id,
            "leg_type":       leg_type,
            "strike":         option["strike"],
            "expiry":         expiry,
            "option_symbol":  option["symbol"],
            "entry_price":    option["mid"],
            "entry_delta":    option.get("delta", 0),
            "entry_iv":       option.get("iv", 0),
            "entry_theta":    option.get("theta", 0),
            "tradier_order_id": None,
        })

    if setup_type == "IC":
        _add_leg(strikes["short_call"], "sell_to_open", "SHORT_CALL")
        _add_leg(strikes["long_call"],  "buy_to_open",  "LONG_CALL")
        _add_leg(strikes["short_put"],  "sell_to_open", "SHORT_PUT")
        _add_leg(strikes["long_put"],   "buy_to_open",  "LONG_PUT")
    elif setup_type == "BEAR_CALL":
        _add_leg(strikes["short_call"], "sell_to_open", "SHORT_CALL")
        _add_leg(strikes["long_call"],  "buy_to_open",  "LONG_CALL")
    else:  # BULL_PUT
        _add_leg(strikes["short_put"],  "sell_to_open", "SHORT_PUT")
        _add_leg(strikes["long_put"],   "buy_to_open",  "LONG_PUT")

    # ── 6. Place order ───────────────────────────────────────────────────────
    order = place_spread_order(order_legs, order_type="market")
    if not order or not order.get("id"):
        return False, "Order placement failed — Tradier returned no order ID"

    order_id = order["id"]
    for leg in db_legs:
        leg["tradier_order_id"] = order_id

    # ── 7. Persist ───────────────────────────────────────────────────────────
    # tier_assignment depends on how many positions will be open after this entry
    new_total = total_open_positions + 1
    tier       = assign_tiers(contracts, new_total, new_total)

    spread_record = {
        "id":              spread_id,
        "trade_date":      _today(),
        "setup_type":      setup_type,
        "signal_grade":    signal_grade,
        "entry_time":      datetime.utcnow().isoformat(),
        "credit_received": net_credit,
        "spread_width":    SPREAD_WIDTH_PTS,
        "contracts":       contracts,
        "tier_assignment": json.dumps(tier),
    }

    insert_spread(spread_record)
    for leg in db_legs:
        insert_leg(leg)

    msg = (
        f"✅ {setup_type} entered | credit=${net_credit:.2f} | "
        f"{contracts}c | grade={signal_grade} | order={order_id}"
    )
    logger.info(msg)
    return True, msg
