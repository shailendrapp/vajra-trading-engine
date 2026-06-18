"""
Module: position_monitor.py
THE CORE ENGINE — runs every 45 seconds during market hours.

IC Contract Structure (fixed):
  Contract 1 → close at 50% of credit (IC_CONTRACT_1_TARGET)
  Contract 2 → close at 70% of credit (IC_CONTRACT_2_TARGET)
  Contract 3 → free runner — breach or hard close only

Breach rules apply to ALL remaining open contracts simultaneously.
"""

import logging
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional
import math
import pytz

from config import (
    IC_CONTRACTS, IC_CONTRACT_1_TARGET, IC_CONTRACT_2_TARGET,
    BREACH_DELTA_THRESHOLD, BREACH_PNL_MULTIPLIER,
    HARD_CLOSE_TIME_PT, FOMC_EXIT_TIME_PT,
)
from core.database import (
    get_open_spreads, get_legs_for_spread, close_spread,
    update_leg_last_quote, insert_snapshot, get_conn,
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
    return now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0)


# FOMC decision days — force-exit at FOMC_EXIT_TIME_PT (10:30 AM PT)
# before the 2:00 PM ET announcement. Update each December.
FOMC_DAYS = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}


def _is_fomc_exit_time(trade_date: str) -> bool:
    """
    Returns True if it's an FOMC day AND past the early exit time (10:30 AM PT).
    Forces all positions closed 30 minutes before the 2:00 PM ET announcement.
    """
    if trade_date not in FOMC_DAYS:
        return False
    now = _now_pt()
    hh, mm = map(int, FOMC_EXIT_TIME_PT.split(":"))
    return now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0)


# ─────────────────────────────────────────────────────────────────────────────
# TIER ASSIGNMENT  — IC only, always 3 contracts: 1 / 1 / 1
# ─────────────────────────────────────────────────────────────────────────────

def assign_tiers(total_contracts: int, position_index: int,
                 total_positions: int) -> Dict:
    """
    IC fixed structure: 3 contracts split 1/1/1.
      tier1 = 1 contract → exits at IC_CONTRACT_1_TARGET (50%)
      tier2 = 1 contract → exits at IC_CONTRACT_2_TARGET (70%)
      free  = 1 contract → breach or hard close only

    If VIX sizing reduces contracts below 3:
      2 contracts → tier1=1, tier2=1, free=0
      1 contract  → tier1=1, tier2=0, free=0
    """
    t1 = min(1, total_contracts)
    t2 = min(1, max(total_contracts - 1, 0))
    fr = max(total_contracts - 2, 0)

    return {
        "tier1_contracts": t1,
        "tier2_contracts": t2,
        "free_contracts":  fr,
        "tier1_closed":    False,
        "tier2_closed":    False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# P&L CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calc_spread_pnl(spread: Dict, legs: List[Dict], quotes: Dict) -> Dict:
    """
    Calculate current P&L for an open spread using live quotes.
    Returns net_debit, pnl_pct, short_leg_delta, pnl_dollars.
    """
    short_legs = [l for l in legs if l["leg_type"].startswith("SHORT")]
    long_legs  = [l for l in legs if l["leg_type"].startswith("LONG")]

    short_debit    = 0.0
    long_credit    = 0.0
    max_short_delta = 0.0

    for leg in short_legs:
        q = quotes.get(leg["option_symbol"])
        if q:
            short_debit += q.get("ask", q["mid"])
            update_leg_last_quote(
                leg["spread_id"], leg["leg_type"],
                q["mid"], q.get("delta", 0), q.get("iv", 0)
            )
            max_short_delta = max(max_short_delta, abs(q.get("delta", 0)))

    for leg in long_legs:
        q = quotes.get(leg["option_symbol"])
        if q:
            long_credit += q.get("bid", q["mid"])
            update_leg_last_quote(
                leg["spread_id"], leg["leg_type"],
                q["mid"], q.get("delta", 0), q.get("iv", 0)
            )

    net_debit       = max(short_debit - long_credit, 0)
    credit_received = spread["credit_received"]
    pnl_pct         = (credit_received - net_debit) / credit_received if credit_received else 0

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
    close_legs = []
    for leg in legs:
        side = "buy_to_close" if leg["leg_type"].startswith("SHORT") else "sell_to_close"
        close_legs.append({
            "symbol":   leg["option_symbol"],
            "side":     side,
            "quantity": contracts,
        })

    # Use limit order at debit price to avoid Tradier sandbox rejections
    # on low-value close orders. Add 10% buffer for fills.
    # Minimum debit of $0.10 to ensure order is accepted.
    limit_price = max(round(close_debit * 1.10, 2), 0.10) if close_debit > 0 else None
    order_type  = "debit" if limit_price else "market"

    order = place_close_order(close_legs, order_type=order_type,
                              limit_price=limit_price)

    # Retry with market order if limit rejected
    if not (order and order.get("id")):
        logger.warning("CLOSE limit order failed — retrying as market order")
        order = place_close_order(close_legs, order_type="market")

    if order and order.get("id"):
        logger.info("CLOSE: spread=%s contracts=%d reason=%s debit=%.2f order=%s",
                    spread["id"][:8], contracts, close_reason, close_debit, order["id"])
        return True
    logger.error("CLOSE FAILED: spread=%s reason=%s", spread["id"][:8], close_reason)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN MONITOR TICK — called every 45 seconds
# ─────────────────────────────────────────────────────────────────────────────

def monitor_tick(daily_state: Dict, account_equity: float) -> Dict:
    trade_date   = _today()
    hard_close   = _is_hard_close_time() or _is_fomc_exit_time(trade_date)
    fomc_exit    = _is_fomc_exit_time(trade_date)
    vix          = get_vix() or 0.0
    open_spreads = get_open_spreads(trade_date)

    if not open_spreads:
        logger.debug("monitor_tick: no open positions")
        return daily_state

    # Batch quote fetch — one API call for all legs
    all_legs    = {}
    all_symbols = []
    for spread in open_spreads:
        legs = get_legs_for_spread(spread["id"])
        all_legs[spread["id"]] = legs
        all_symbols.extend(leg["option_symbol"] for leg in legs)

    quotes = get_multi_option_quotes(list(set(all_symbols)))

    for spread in open_spreads:
        sid  = spread["id"]
        legs = all_legs.get(sid, [])
        if not legs:
            continue

        pnl_data        = calc_spread_pnl(spread, legs, quotes)
        credit_received = pnl_data["credit_received"]
        pnl_pct         = pnl_data["pnl_pct"]
        tier            = json.loads(spread.get("tier_assignment") or "{}")

        insert_snapshot(sid, pnl_data["net_debit"], pnl_pct,
                        pnl_data["short_leg_delta"], vix,
                        {"hard_close": hard_close})

        # ── HARD CLOSE — 12:30 PM PT, close everything remaining ────────────
        if hard_close:
            close_reason = "FOMC_EXIT" if fomc_exit and not _is_hard_close_time() else "HARD_CLOSE"
            remaining = (
                (tier.get("tier1_contracts", 0) if not tier.get("tier1_closed") else 0) +
                (tier.get("tier2_contracts", 0) if not tier.get("tier2_closed") else 0) +
                tier.get("free_contracts", 0)
            )
            if remaining > 0:
                logger.info("HARD CLOSE: spread=%s remaining=%dc", sid[:8], remaining)
                success = _execute_close(spread, legs, remaining,
                                         pnl_data["net_debit"], "HARD_CLOSE")
                if success:
                    pnl = close_spread(sid, pnl_data["net_debit"],
                                       "HARD_CLOSE", credit_received)
                    daily_state = update_daily_pnl(trade_date, daily_state, pnl)
            continue

        # ── BREACH CHECK — closes ALL remaining contracts immediately ────────
        breach_reason = None
        if pnl_data["short_leg_delta"] >= BREACH_DELTA_THRESHOLD:
            breach_reason = f"BREACH_DELTA ({pnl_data['short_leg_delta']:.2f})"
        elif pnl_data["net_debit"] >= credit_received * BREACH_PNL_MULTIPLIER:
            breach_reason = f"BREACH_PNL (debit={pnl_data['net_debit']:.2f})"

        if breach_reason:
            remaining = (
                (tier.get("tier1_contracts", 0) if not tier.get("tier1_closed") else 0) +
                (tier.get("tier2_contracts", 0) if not tier.get("tier2_closed") else 0) +
                tier.get("free_contracts", 0)
            )
            logger.warning("BREACH: spread=%s reason=%s remaining=%dc",
                           sid[:8], breach_reason, remaining)
            success = _execute_close(spread, legs, remaining,
                                     pnl_data["net_debit"], breach_reason)
            if success:
                pnl = close_spread(sid, pnl_data["net_debit"],
                                   breach_reason, credit_received)
                daily_state = update_daily_pnl(trade_date, daily_state, pnl)
                daily_state = record_stopout(trade_date, daily_state)
                check_and_apply_circuit_breaker(trade_date, daily_state, account_equity)
            continue

        # ── CONTRACT 1 — exit at 50% ─────────────────────────────────────────
        if not tier.get("tier1_closed") and tier.get("tier1_contracts", 0) > 0:
            if pnl_pct >= IC_CONTRACT_1_TARGET:
                logger.info("CONTRACT 1 (50%%) close: spread=%s pnl=%.1f%%",
                            sid[:8], pnl_pct * 100)
                success = _execute_close(spread, legs,
                                         tier["tier1_contracts"],
                                         pnl_data["net_debit"], "CONTRACT_1_50PCT")
                if success:
                    partial_pnl = ((credit_received - pnl_data["net_debit"])
                                   * tier["tier1_contracts"] * 100)
                    daily_state = update_daily_pnl(trade_date, daily_state, partial_pnl)
                    tier["tier1_closed"] = True
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE spreads SET tier_assignment=? WHERE id=?",
                            (json.dumps(tier), sid)
                        )

        # ── CONTRACT 2 — exit at 70% ─────────────────────────────────────────
        if (tier.get("tier1_closed") and not tier.get("tier2_closed")
                and tier.get("tier2_contracts", 0) > 0):
            if pnl_pct >= IC_CONTRACT_2_TARGET:
                logger.info("CONTRACT 2 (70%%) close: spread=%s pnl=%.1f%%",
                            sid[:8], pnl_pct * 100)
                success = _execute_close(spread, legs,
                                         tier["tier2_contracts"],
                                         pnl_data["net_debit"], "CONTRACT_2_70PCT")
                if success:
                    partial_pnl = ((credit_received - pnl_data["net_debit"])
                                   * tier["tier2_contracts"] * 100)
                    daily_state = update_daily_pnl(trade_date, daily_state, partial_pnl)
                    tier["tier2_closed"] = True
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE spreads SET tier_assignment=? WHERE id=?",
                            (json.dumps(tier), sid)
                        )

        # ── CONTRACT 3 — free runner, log status only ────────────────────────
        if (tier.get("tier1_closed") and tier.get("tier2_closed")
                and tier.get("free_contracts", 0) > 0):
            logger.debug("CONTRACT 3 (free runner): spread=%s pnl=%.1f%%",
                         sid[:8], pnl_pct * 100)

    return daily_state
