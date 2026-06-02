"""
Module: trade_entry.py
Handles inbound trade signals, validates entry gates,
selects strikes using Option A logic (GEX-first, delta fallback),
calculates contracts, and submits orders to Tradier.

Option A strike selection:
  1. Fetch GEX walls from FlashAlpha
  2. Find the nearest positive GEX wall above price where short leg delta
     falls within GEX_DELTA_MIN–GEX_DELTA_MAX range → use it
  3. If no wall aligns with delta range → fall back to pure delta targeting
  4. Long leg = short strike + SPREAD_WIDTH_PTS in both cases
"""

import logging
import uuid
import json
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
import math
import pytz

from config import (
    SPREAD_WIDTH_PTS, RISK_PCT_PER_TRADE,
    GEX_DELTA_MIN, GEX_DELTA_MAX, GEX_WALL_DELTA_FALLBACK,
)
from core.database import insert_spread, insert_leg, get_or_create_daily_state
from core.tradier import get_option_chain, place_spread_order, get_spx_price, get_vix
from core.risk_manager import (
    check_entry_gates, calculate_contracts, EntryGateResult
)
from core.flashalpha import get_client as get_gex_client, GEXWall
from modules.position_monitor import assign_tiers

logger = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def _today() -> str:
    return datetime.now(PT).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# OPTION A — GEX-FIRST STRIKE SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def _find_strike_gex_first(
    chain: List[Dict],
    option_type: str,       # 'call' | 'put'
    direction: str,         # 'above' | 'below' (relative to spx_price)
    spx_price: float,
    gex_walls: List[GEXWall],
) -> Tuple[Optional[Dict], str]:
    """
    Option A logic:
      Step 1 — Walk GEX walls nearest to price outward.
               For each wall, find the chain strike at that level.
               Check if its delta is within [GEX_DELTA_MIN, GEX_DELTA_MAX].
               First wall that passes → use it. Return (strike_dict, "GEX").

      Step 2 — No wall passed delta check → fall back to pure delta targeting.
               Find strike with delta closest to 0.15 in the valid range.
               Return (strike_dict, "DELTA_FALLBACK").

    Returns: (option_dict or None, method_used)
    """
    # Filter chain to correct side and minimum liquidity
    candidates = [
        o for o in chain
        if o["type"] == option_type
        and o.get("bid", 0) > 0.10
        and (
            (direction == "above" and o["strike"] > spx_price) or
            (direction == "below" and o["strike"] < spx_price)
        )
    ]

    if not candidates:
        return None, "NO_CANDIDATES"

    # ── Step 1: GEX wall scan ─────────────────────────────────────────────
    relevant_walls = [
        w for w in gex_walls
        if (direction == "above" and w.strike > spx_price) or
           (direction == "below" and w.strike < spx_price)
    ]
    # Sort nearest-first
    relevant_walls.sort(
        key=lambda w: abs(w.strike - spx_price)
    )

    for wall in relevant_walls:
        # Find chain strike at or nearest to this GEX wall
        nearest = min(
            candidates,
            key=lambda o: abs(o["strike"] - wall.strike)
        )
        # Only accept if within 5 pts of the wall
        if abs(nearest["strike"] - wall.strike) > 5:
            continue
        # Check delta in valid range
        delta_abs = abs(nearest.get("delta", 0))
        if GEX_DELTA_MIN <= delta_abs <= GEX_DELTA_MAX:
            logger.info(
                "GEX wall strike selected: %.0f (wall=%.0f delta=%.2f net_gex=%.0f)",
                nearest["strike"], wall.strike, delta_abs, wall.net_gex
            )
            return nearest, "GEX"

    # ── Step 2: Delta fallback ────────────────────────────────────────────
    if not GEX_WALL_DELTA_FALLBACK:
        logger.info("No GEX wall in delta range and fallback disabled — skipping")
        return None, "NO_WALL_NO_FALLBACK"

    delta_candidates = [
        o for o in candidates
        if GEX_DELTA_MIN <= abs(o.get("delta", 0)) <= GEX_DELTA_MAX
    ]
    if not delta_candidates:
        return None, "NO_DELTA_CANDIDATES"

    delta_candidates.sort(key=lambda o: abs(abs(o.get("delta", 0)) - 0.15))
    best = delta_candidates[0]
    logger.info(
        "Delta fallback strike selected: %.0f (delta=%.2f, no GEX wall aligned)",
        best["strike"], abs(best.get("delta", 0))
    )
    return best, "DELTA_FALLBACK"


def _find_long_leg(chain: List[Dict], option_type: str,
                   short_strike: float, direction: str) -> Optional[Dict]:
    """Long leg is always SPREAD_WIDTH_PTS beyond the short leg."""
    if direction == "above":   # call spread — long leg higher
        target = short_strike + SPREAD_WIDTH_PTS
    else:                      # put spread — long leg lower
        target = short_strike - SPREAD_WIDTH_PTS

    return next(
        (o for o in chain
         if o["type"] == option_type and o["strike"] == target),
        None
    )


# ─────────────────────────────────────────────────────────────────────────────
# SETUP-LEVEL STRIKE SELECTORS
# ─────────────────────────────────────────────────────────────────────────────

def select_ic_strikes(
    spx_price: float,
    chain_calls: List[Dict],
    chain_puts: List[Dict],
    gex: Optional["SPXSummary"],
) -> Optional[Dict]:
    """Select Iron Condor strikes using Option A logic on both sides."""
    walls_above = [w for w in (gex.positive_walls if gex else [])
                   if w.strike > spx_price]
    walls_below = [w for w in (gex.positive_walls if gex else [])
                   if w.strike < spx_price]

    short_call, call_method = _find_strike_gex_first(
        chain_calls, "call", "above", spx_price, walls_above
    )
    short_put, put_method = _find_strike_gex_first(
        chain_puts, "put", "below", spx_price, walls_below
    )

    if not short_call or not short_put:
        logger.warning("IC strike selection failed: call=%s put=%s",
                       call_method, put_method)
        return None

    long_call = _find_long_leg(chain_calls, "call", short_call["strike"], "above")
    long_put  = _find_long_leg(chain_puts,  "put",  short_put["strike"],  "below")

    if not long_call or not long_put:
        logger.warning("IC long leg not found at %.0f / %.0f",
                       short_call["strike"] + SPREAD_WIDTH_PTS,
                       short_put["strike"]  - SPREAD_WIDTH_PTS)
        return None

    logger.info("IC strikes: call %s/%s via %s | put %s/%s via %s",
                short_call["strike"], long_call["strike"], call_method,
                short_put["strike"],  long_put["strike"],  put_method)
    return {
        "short_call": short_call, "long_call": long_call,
        "short_put":  short_put,  "long_put":  long_put,
        "call_method": call_method, "put_method": put_method,
    }


def select_bear_call_strikes(
    spx_price: float,
    chain_calls: List[Dict],
    gex: Optional["SPXSummary"],
) -> Optional[Dict]:
    walls_above = [w for w in (gex.positive_walls if gex else [])
                   if w.strike > spx_price]
    short_call, method = _find_strike_gex_first(
        chain_calls, "call", "above", spx_price, walls_above
    )
    if not short_call:
        return None
    long_call = _find_long_leg(chain_calls, "call", short_call["strike"], "above")
    if not long_call:
        return None
    logger.info("Bear Call: %s/%s via %s",
                short_call["strike"], long_call["strike"], method)
    return {"short_call": short_call, "long_call": long_call, "method": method}


def select_bull_put_strikes(
    spx_price: float,
    chain_puts: List[Dict],
    gex: Optional["SPXSummary"],
) -> Optional[Dict]:
    walls_below = [w for w in (gex.positive_walls if gex else [])
                   if w.strike < spx_price]
    short_put, method = _find_strike_gex_first(
        chain_puts, "put", "below", spx_price, walls_below
    )
    if not short_put:
        return None
    long_put = _find_long_leg(chain_puts, "put", short_put["strike"], "below")
    if not long_put:
        return None
    logger.info("Bull Put: %s/%s via %s",
                short_put["strike"], long_put["strike"], method)
    return {"short_put": short_put, "long_put": long_put, "method": method}


# ─────────────────────────────────────────────────────────────────────────────
# CREDIT CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calc_net_credit(strikes: Dict, setup_type: str) -> float:
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
    setup_type: str,
    signal_grade: str,
    is_news_day: bool,
    daily_state: Dict,
    account_equity: float,
    total_open_positions: int,
) -> Tuple[bool, str]:
    """
    Full entry flow:
      1. Entry gate check
      2. Fetch GEX context from FlashAlpha (cached 30 min)
      3. Fetch options chain from Tradier
      4. Select strikes — Option A (GEX-first, delta fallback)
      5. Calculate contracts (VIX-adjusted)
      6. Place order on Tradier paper account
      7. Persist spread + legs to DB
    """
    # ── 1. Entry gates ───────────────────────────────────────────────────────
    vix = get_vix() or 0.0
    gate = check_entry_gates(vix, signal_grade, is_news_day, daily_state)
    if not gate.allowed:
        logger.info("Entry blocked: %s", gate.reason)
        return False, str(gate)

    # ── 2. Market data + GEX ─────────────────────────────────────────────────
    spx_price = get_spx_price()
    if not spx_price:
        return False, "Could not fetch SPX price"

    expiry    = _today()
    chain_all = get_option_chain(expiry, "all")
    if not chain_all:
        return False, "Could not fetch option chain"

    chain_calls = [o for o in chain_all if o["type"] == "call"]
    chain_puts  = [o for o in chain_all if o["type"] == "put"]

    # FlashAlpha — non-blocking: None = fall back to delta
    gex_client = get_gex_client()
    gex = gex_client.get_gex_context(spx_price, expiration=expiry)
    if gex:
        logger.info(
            "GEX context: call_wall=%.0f put_wall=%.0f gamma_flip=%.0f "
            "positive_walls=%d calls_left=%d",
            gex.call_wall, gex.put_wall, gex.gamma_flip,
            len(gex.positive_walls), gex.calls_left
        )
    else:
        logger.warning("GEX unavailable — using delta-only strike selection")

    # ── 3. Strike selection ───────────────────────────────────────────────────
    if setup_type == "IC":
        strikes = select_ic_strikes(spx_price, chain_calls, chain_puts, gex)
    elif setup_type == "BEAR_CALL":
        strikes = select_bear_call_strikes(spx_price, chain_calls, gex)
    elif setup_type == "BULL_PUT":
        strikes = select_bull_put_strikes(spx_price, chain_puts, gex)
    else:
        return False, f"Unknown setup_type: {setup_type}"

    if not strikes:
        return False, f"Strike selection failed for {setup_type}"

    # ── 4. Credit check ───────────────────────────────────────────────────────
    net_credit = calc_net_credit(strikes, setup_type)
    if net_credit < 0.30:
        return False, f"Net credit ${net_credit:.2f} below $0.30 minimum"

    # ── 5. Contract sizing ────────────────────────────────────────────────────
    # IC: always IC_CONTRACTS (3). VIX multiplier may reduce if elevated.
    # Bear Call / Bull Put: standard account-based sizing.
    from config import IC_CONTRACTS
    from core.risk_manager import vix_size_multiplier
    consecutive_wins = daily_state.get("consecutive_win_days", 0)
    if setup_type == "IC":
        contracts = max(1, math.floor(IC_CONTRACTS * vix_size_multiplier(vix)))
    else:
        contracts = calculate_contracts(account_equity, consecutive_wins, vix=vix)

    # ── 6. Build order legs ───────────────────────────────────────────────────
    spread_id  = str(uuid.uuid4())
    order_legs = []
    db_legs    = []

    def _add_leg(option: Dict, side_open: str, leg_type: str):
        order_legs.append({
            "symbol":   option["symbol"],
            "side":     side_open,
            "quantity": contracts,
        })
        db_legs.append({
            "id":              str(uuid.uuid4()),
            "spread_id":       spread_id,
            "leg_type":        leg_type,
            "strike":          option["strike"],
            "expiry":          expiry,
            "option_symbol":   option["symbol"],
            "entry_price":     option["mid"],
            "entry_delta":     option.get("delta", 0),
            "entry_iv":        option.get("iv", 0),
            "entry_theta":     option.get("theta", 0),
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

    # ── 7. Place order ────────────────────────────────────────────────────────
    order = place_spread_order(order_legs, order_type="market")
    if not order or not order.get("id"):
        return False, "Order placement failed — Tradier returned no order ID"

    order_id = order["id"]
    for leg in db_legs:
        leg["tradier_order_id"] = order_id

    # ── 8. Persist ────────────────────────────────────────────────────────────
    new_total = total_open_positions + 1
    tier      = assign_tiers(contracts, new_total, new_total)

    # Build strike selection note for audit trail
    method_note = ""
    if setup_type == "IC":
        method_note = (f"call_method={strikes.get('call_method','?')} "
                       f"put_method={strikes.get('put_method','?')}")
    else:
        method_note = f"method={strikes.get('method','?')}"

    # Include GEX context in notes if available
    gex_note = ""
    if gex:
        gex_note = (f" | call_wall={gex.call_wall:.0f} "
                    f"put_wall={gex.put_wall:.0f} "
                    f"gamma_flip={gex.gamma_flip:.0f}")

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
        "notes":           f"{method_note}{gex_note}",
    }

    insert_spread(spread_record)
    for leg in db_legs:
        insert_leg(leg)

    msg = (
        f"✅ {setup_type} entered | credit=${net_credit:.2f} | "
        f"{contracts}c | grade={signal_grade} | {method_note} | order={order_id}"
    )
    logger.info(msg)
    return True, msg
