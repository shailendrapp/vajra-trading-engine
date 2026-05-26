"""
Module: position_monitor.py
THE CORE ENGINE — runs every 45 seconds during market hours.

For each open spread it:
  1. Fetches current quotes + Greeks for all legs
  2. Evaluates profit tier targets (tier1 / tier2 / free runner)
  3. Checks breach conditions (delta + P&L)
  4. Executes closes via Tradier when triggered
  5. Persists snapshots and updates daily P&L

This is what fixes the 10-minute lag. 45s polling, no cron.
"""

import logging
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import math
import pytz

from config import (
    PROFIT_TIER_1, PROFIT_TIER_2, SOLO_PROFIT_TARGET,
    BREACH_DELTA_THRESHOLD, BREACH_PNL_MULTIPLIER,
    HARD_CLOSE_TIME_PT,
)
from core.database import (
    get_open_spreads, get_legs_for_spread, close_spread,
    update_leg_last_quote, insert_snapshot, update_daily_state,
    get_or_create_daily_state,
)
from core.tradier import get_multi_option_quotes, get_vix, place_close_order
from core.risk_manager import record_stopout, check_and_apply_circuit_breaker, update_daily_pnl

logger = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")


def _now_pt() -> datetime:
    return datetime.now(PT)


def _today() -> str:
    return _now_pt().strftime("%Y-%m-%d")


def _is_hard_close_time() -> bool:
    now = _now_pt()
    hh, mm = map(int, HARD_CLOSE_TIME_PT.split(":"))
    hard_close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= hard_close


# ─────────────────────────────────────────────────────────────────────────────
# TIER ASSIGNMENT
# ─────────────────────────────────────────────────────────────────────────────

def assign_tiers(total_contracts: int, position_index: int, total_positions: int) -> Dict:
    """
    Determines how many contracts of a spread fall into each tier.
    Called at position open time.

    tier_assignment = {
        "tier1_contracts": N,   close at PROFIT_TIER_1 (50%)
        "tier2_contracts": N,   close at PROFIT_TIER_2 (60%) — 0 if solo
        "free_contracts":  N,   free runner
        "tier1_closed": False,
        "tier2_closed": False,
    }

    With 1 position open: 100% tier1, no tier2, no free runner.
    With 2+ positions:    50% tier1, 25% tier2, 25% free runner.
    """
    if total_positions == 1:
        return {
            "tier1_contracts": total_contracts,
            "tier2_contracts": 0,
            "free_contracts":  0,
            "tier1_closed":    False,
            "tier2_closed":    False,
        }

    t1 = math.ceil(total_contracts * 0.50)
    t2 = math.ceil((total_contracts - t1) * 0.50)
    fr = total_contracts - t1 - t2

    return {
        "tier1_contracts": t1,
        "tier2_contracts": max(t2, 0),
        "free_contracts":  max(fr, 0),
        "tier1_closed":    False,
        "tier2_closed":    False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# P&L CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calc_spread_pnl(spread: Dict, legs: List[Dict], quotes: Dict) -> Dict:
    """
    Given current quotes, compute:
      - net_debit_to_close: what it costs to close right now per spread
      - pnl_pct: % of original credit captured (0.50 = 50%)
      - short_leg_delta: worst-case short leg delta (abs)
      - current_pnl_dollars: per contract dollar P&L

    For an IC or credit spread:
      credit_received = short legs credit - long legs debit (at entry)
      current_debit   = short legs current ask - long legs current bid (to close)
      pnl_pct         = (credit_received - current_debit) / credit_received
    """
    short_legs = [l for l in legs if l["leg_type"].startswith("SHORT")]
    long_legs  = [l for l in legs if l["leg_type"].startswith("LONG")]

    short_debit = 0.0   # cost to buy back short legs
    long_credit = 0.0   # proceeds from selling long legs
    max_short_delta = 0.0

    for leg in short_legs:
        q = quotes.get(leg["option_symbol"])
        if q:
            short_debit += q.get("ask", q["mid"])  # use ask to be conservative
            update_leg_last_quote(
                leg["spread_id"], leg["leg_type"],
                q["mid"], q.get("delta", 0), q.get("iv", 0)
            )
            max_short_delta = max(max_short_delta, abs(q.get("delta", 0)))

    for leg in long_legs:
        q = quotes.get(leg["option_symbol"])
        if q:
            long_credit += q.get("bid", q["mid"])  # use bid to be conservative
            update_leg_last_quote(
                leg["spread_id"], leg["leg_type"],
                q["mid"], q.get("delta", 0), q.get("iv", 0)
            )

    net_debit      = max(short_debit - long_credit, 0)
    credit_received = spread["credit_received"]
    pnl_pct        = (credit_received - net_debit) / credit_received if credit_received else 0

    return {
        "net_debit":        round(net_debit, 2),
        "pnl_pct":          round(pnl_pct, 4),
        "short_leg_delta":  round(max_short_delta, 4),
        "credit_received":  credit_received,
        "pnl_dollars":      round((credit_received - net_debit) * spread["contracts"] * 100, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLOSE EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def _execute_close(spread: Dict, legs: List[Dict], contracts: int,
                   close_debit: float, close_reason: str) -> bool:
    """
    Send close order to Tradier for `contracts` worth of a spread.
    Returns True on success.
    """
    close_legs = []
    for leg in legs:
        if leg["leg_type"].startswith("SHORT"):
            close_legs.append({
                "symbol":   leg["option_symbol"],
                "side":     "buy_to_close",
                "quantity": contracts,
            })
        else:
            close_legs.append({
                "symbol":   leg["option_symbol"],
                "side":     "sell_to_close",
                "quantity": contracts,
            })

    order = place_close_order(close_legs, order_type="market")

    if order and order.get("id"):
        logger.info(
            "CLOSE executed: spread=%s contracts=%d reason=%s debit=%.2f order_id=%s",
            spread["id"][:8], contracts, close_reason, close_debit, order["id"]
        )
        return True
    else:
        logger.error("CLOSE FAILED: spread=%s reason=%s", spread["id"][:8], close_reason)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN MONITOR TICK
# ─────────────────────────────────────────────────────────────────────────────

def monitor_tick(daily_state: Dict, account_equity: float) -> Dict:
    """
    Single polling cycle. Call this every 45 seconds.
    Processes ALL open spreads, executes exits as needed.
    Returns updated daily_state.
    """
    trade_date   = _today()
    hard_close   = _is_hard_close_time()
    vix          = get_vix() or 0.0
    open_spreads = get_open_spreads(trade_date)

    if not open_spreads:
        logger.debug("monitor_tick: no open positions")
        return daily_state

    # Collect all leg symbols for one batched quote call
    all_legs    = {}
    all_symbols = []
    for spread in open_spreads:
        legs = get_legs_for_spread(spread["id"])
        all_legs[spread["id"]] = legs
        all_symbols.extend(leg["option_symbol"] for leg in legs)

    quotes = get_multi_option_quotes(list(set(all_symbols)))

    total_open = len(open_spreads)

    for spread in open_spreads:
        sid  = spread["id"]
        legs = all_legs.get(sid, [])

        if not legs:
            logger.warning("No legs found for spread %s — skipping", sid[:8])
            continue

        pnl_data = calc_spread_pnl(spread, legs, quotes)

        # Persist snapshot
        insert_snapshot(
            spread_id      = sid,
            net_debit      = pnl_data["net_debit"],
            pnl_pct        = pnl_data["pnl_pct"],
            short_leg_delta= pnl_data["short_leg_delta"],
            vix            = vix,
            raw            = {"quotes_count": len(quotes), "hard_close": hard_close},
        )

        tier = json.loads(spread.get("tier_assignment") or "{}")
        credit_received = pnl_data["credit_received"]

        # ── HARD CLOSE — override everything ──────────────────────────────
        if hard_close:
            logger.info("HARD CLOSE triggered for spread %s", sid[:8])
            success = _execute_close(
                spread, legs, spread["contracts"],
                pnl_data["net_debit"], "HARD_CLOSE"
            )
            if success:
                pnl = close_spread(sid, pnl_data["net_debit"], "HARD_CLOSE", credit_received)
                daily_state = update_daily_pnl(trade_date, daily_state, pnl)
            continue

        # ── BREACH CHECK ──────────────────────────────────────────────────
        breach_reason = None
        if pnl_data["short_leg_delta"] >= BREACH_DELTA_THRESHOLD:
            breach_reason = f"BREACH_DELTA ({pnl_data['short_leg_delta']:.2f})"
        elif pnl_data["net_debit"] >= credit_received * BREACH_PNL_MULTIPLIER:
            breach_reason = f"BREACH_PNL (debit={pnl_data['net_debit']:.2f} vs credit={credit_received:.2f})"

        if breach_reason:
            logger.warning("BREACH: spread=%s reason=%s", sid[:8], breach_reason)
            success = _execute_close(
                spread, legs, spread["contracts"],
                pnl_data["net_debit"], breach_reason
            )
            if success:
                pnl = close_spread(sid, pnl_data["net_debit"], breach_reason, credit_received)
                daily_state = update_daily_pnl(trade_date, daily_state, pnl)
                daily_state = record_stopout(trade_date, daily_state)
                check_and_apply_circuit_breaker(trade_date, daily_state, account_equity)
            continue

        # ── PROFIT TIER EXITS ─────────────────────────────────────────────
        pnl_pct = pnl_data["pnl_pct"]

        # Tier 1 — 50% profit target
        if not tier.get("tier1_closed") and tier.get("tier1_contracts", 0) > 0:
            target = SOLO_PROFIT_TARGET if total_open == 1 else PROFIT_TIER_1
            if pnl_pct >= target:
                t1_contracts = tier["tier1_contracts"]
                logger.info(
                    "TIER1 close: spread=%s contracts=%d pnl_pct=%.1f%%",
                    sid[:8], t1_contracts, pnl_pct * 100
                )
                success = _execute_close(
                    spread, legs, t1_contracts,
                    pnl_data["net_debit"], "TIER1"
                )
                if success:
                    partial_pnl = (credit_received - pnl_data["net_debit"]) * t1_contracts * 100
                    daily_state = update_daily_pnl(trade_date, daily_state, partial_pnl)
                    tier["tier1_closed"] = True
                    update_daily_state(trade_date)  # tier state updated in spread record below
                    # Update tier in spread record
                    from core.database import get_conn
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE spreads SET tier_assignment = ? WHERE id = ?",
                            (json.dumps(tier), sid)
                        )

        # Tier 2 — 60% profit target
        if (tier.get("tier1_closed") and not tier.get("tier2_closed")
                and tier.get("tier2_contracts", 0) > 0):
            if pnl_pct >= PROFIT_TIER_2:
                t2_contracts = tier["tier2_contracts"]
                logger.info(
                    "TIER2 close: spread=%s contracts=%d pnl_pct=%.1f%%",
                    sid[:8], t2_contracts, pnl_pct * 100
                )
                success = _execute_close(
                    spread, legs, t2_contracts,
                    pnl_data["net_debit"], "TIER2"
                )
                if success:
                    partial_pnl = (credit_received - pnl_data["net_debit"]) * t2_contracts * 100
                    daily_state = update_daily_pnl(trade_date, daily_state, partial_pnl)
                    tier["tier2_closed"] = True
                    from core.database import get_conn
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE spreads SET tier_assignment = ? WHERE id = ?",
                            (json.dumps(tier), sid)
                        )

        # Free runner — only breach or hard close will close it (handled above)
        if (tier.get("tier1_closed") and tier.get("tier2_closed")
                and tier.get("free_contracts", 0) > 0):
            logger.debug(
                "Free runner active: spread=%s contracts=%d pnl=%.1f%%",
                sid[:8], tier["free_contracts"], pnl_pct * 100
            )

    return daily_state
