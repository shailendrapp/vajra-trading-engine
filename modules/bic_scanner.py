"""
Module: bic_scanner.py
BIC (Breakeven Iron Condor) Scanner for Argus Trading Engine

Generates SPX 0DTE IC signals automatically and executes via Tradier.
Replaces manual /enter IC Telegram commands.

Signal logic:
  - Scans at scheduled windows (10:15, 11:15, 12:15, 13:15, 14:15 ET)
  - Selects strikes at delta 0.06–0.12 (target 0.09)
  - Dynamic wing width: 25pt (VIX<20), 30pt (VIX 20-25), 35pt (VIX 25-30)
  - Skips: VIX < 12, VIX ≥ 30, news days (CPI/FOMC/NFP/PCE/JOLTS)
  - Auto-enters on GO and WAIT verdicts
  - Sends formatted alert to Telegram matching your existing BIC format

Exit logic:
  - Handled by position_monitor.py (C1@50%, C2@70%, C3 free runner)
  - Hard close 12:30 PT
  - Breach: delta ≥ 0.40 or debit ≥ 2× credit
"""

import logging
import math
import json
import uuid
from datetime import datetime, date
from typing import Optional, Dict, List, Tuple
import pytz
import requests

from config import (
    BIC_WING_TIERS, BIC_SHORT_DELTA_TARGET, BIC_SHORT_DELTA_MIN,
    BIC_SHORT_DELTA_MAX, BIC_MIN_CREDIT, BIC_VIX_FLOOR,
    BIC_ENTRY_WINDOWS_ET, BIC_NEWS_EVENTS,
    BIC_ADAPTIVE_DELTA, BIC_CREDIT_VIX_TIERS,
    BIC_EM_MULT, BIC_TRADING_HOURS, BIC_USE_TIME_ADJ_EM,
    VIX_KILL_SWITCH, IC_CONTRACTS, SPREAD_WIDTH_PTS,
    ANTHROPIC_API_KEY,
)
from core.tradier import (
    get_spx_price, get_vix, get_option_chain, place_spread_order
)
from core.database import (
    insert_spread, insert_leg, get_open_spreads,
    get_or_create_daily_state
)
from core.risk_manager import (
    vix_size_multiplier, check_and_apply_circuit_breaker,
    update_daily_pnl
)
from modules.position_monitor import assign_tiers
from modules.telegram_bot import send
from core.flashalpha import get_client, get_spx_summary, GEXWall

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")
PT = pytz.timezone("America/Los_Angeles")


def _now_et() -> datetime:
    return datetime.now(ET)

def _now_pt() -> datetime:
    return datetime.now(PT)

def _today() -> str:
    return _now_pt().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC WING WIDTH
# ─────────────────────────────────────────────────────────────────────────────

def get_wing_width(vix: float) -> int:
    """
    Returns wing width in SPX points based on VIX level.
    Backtest validated: wider wings in higher VIX = 98.7% win rate.
    """
    for threshold, width in BIC_WING_TIERS:
        if vix < threshold:
            return width
    return BIC_WING_TIERS[-1][1]  # max width for high VIX


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY GATE — BIC SPECIFIC
# ─────────────────────────────────────────────────────────────────────────────

def bic_entry_allowed(
    vix: float,
    is_news_day: bool,
    daily_state: Dict,
    account_equity: float,
) -> Tuple[bool, str]:
    """
    BIC-specific entry gates.
    Returns (allowed, reason).
    """
    if vix < BIC_VIX_FLOOR:
        return False, f"VIX {vix:.1f} < {BIC_VIX_FLOOR} floor — credit too thin"

    if vix >= VIX_KILL_SWITCH:
        return False, f"VIX {vix:.1f} ≥ {VIX_KILL_SWITCH} kill switch"

    if is_news_day:
        return False, "News day (CPI/FOMC/NFP/PCE/JOLTS) — skipping"

    if daily_state.get("circuit_breaker_hit"):
        return False, "Daily circuit breaker triggered"

    if daily_state.get("entries_halted"):
        return False, "Entries halted after 2nd stop-out"

    # Check max concurrent positions
    open_spreads = get_open_spreads(_today())
    if len(open_spreads) >= 3:
        return False, f"Max 3 concurrent positions already open"

    # Build existing strike sets for duplicate check (used in run_bic_scan)
    return True, "Gates passed"


def _get_existing_strikes() -> tuple:
    """Returns (short_put_strikes, short_call_strikes) currently open."""
    from core.database import get_legs_for_spread
    open_spreads    = get_open_spreads(_today())
    short_puts  = set()
    short_calls = set()
    for spread in open_spreads:
        for leg in get_legs_for_spread(spread["id"]):
            if leg["leg_type"] == "SHORT_PUT":
                short_puts.add(float(leg["strike"]))
            elif leg["leg_type"] == "SHORT_CALL":
                short_calls.add(float(leg["strike"]))
    return short_puts, short_calls


# ─────────────────────────────────────────────────────────────────────────────
# STRIKE SELECTION — BIC DELTA METHOD
# ─────────────────────────────────────────────────────────────────────────────

def min_credit_for_vix(vix: float) -> float:
    """
    Returns minimum acceptable credit based on VIX level.
    At higher VIX, we need more credit to justify the risk —
    the 2× breach multiplier fires too quickly on thin credits.

    VIX < 17:  $0.50 (normal conditions)
    VIX 17-20: $0.70 (elevated — today's scenario: VIX 18.3 → blocked $0.55)
    VIX 20-30: $1.00 (high volatility — need substantial premium)
    """
    for vix_threshold, min_credit in BIC_CREDIT_VIX_TIERS:
        if vix < vix_threshold:
            return min_credit
    return BIC_CREDIT_VIX_TIERS[-1][1]


def adaptive_delta_target(atm_iv: float) -> float:
    """
    Returns delta target based on ATM IV level.
    Lower IV → lower delta → strikes go further OTM → safer.
    Higher IV → higher delta → more premium available.

    BIC_ADAPTIVE_DELTA tiers (from config):
      atm_iv < 0.11 → 0.05  (very low IV, very far OTM)
      atm_iv < 0.13 → 0.06  (low IV)
      atm_iv < 0.16 → 0.09  (normal — standard BIC)
      atm_iv ≥ 0.16 → 0.12  (high IV)
    """
    for iv_threshold, delta in BIC_ADAPTIVE_DELTA:
        if atm_iv < iv_threshold:
            return delta
    return BIC_SHORT_DELTA_TARGET


def find_strike_at_delta(
    chain: List[Dict],
    option_type: str,
    target_delta: float = BIC_SHORT_DELTA_TARGET,
    delta_min: float = BIC_SHORT_DELTA_MIN,
    delta_max: float = BIC_SHORT_DELTA_MAX,
) -> Optional[Dict]:
    """
    Find the option in the chain where abs(delta) is closest to target_delta.
    Accepts custom delta_min/max to support adaptive delta ranges.
    """
    candidates = [
        o for o in chain
        if o.get("type") == option_type
        and delta_min <= abs(o.get("delta", 0)) <= delta_max
        and o.get("bid", 0) > 0.05
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda o: abs(abs(o.get("delta", 0)) - target_delta))
    return candidates[0]


def _select_strikes_near_target(
    chain: List[Dict],
    spx: float,
    target_call: float,
    target_put: float,
    wing_width: int,
) -> Optional[Dict]:
    """
    Find chain strikes nearest to GEX-anchored targets.
    Validates that selected strikes have delta in acceptable range.
    Falls back to None if no valid match found.
    """
    calls = [o for o in chain if o.get("type") == "call"
             and o.get("strike", 0) >= target_call - 10
             and o.get("strike", 0) >= spx]
    puts  = [o for o in chain if o.get("type") == "put"
             and o.get("strike", 0) <= target_put + 10
             and o.get("strike", 0) <= spx]

    if not calls or not puts:
        return None

    # Nearest call strike to target
    short_call = min(calls, key=lambda o: abs(o.get("strike", 0) - target_call))
    short_put  = min(puts,  key=lambda o: abs(o.get("strike", 0) - target_put))

    long_call_strike = short_call["strike"] + wing_width
    long_put_strike  = short_put["strike"]  - wing_width

    long_call = next(
        (o for o in chain if o.get("type") == "call"
         and o.get("strike") == long_call_strike), None
    )
    long_put = next(
        (o for o in chain if o.get("type") == "put"
         and o.get("strike") == long_put_strike), None
    )

    if not long_call or not long_put:
        logger.warning(
            "GEX selection: long legs not found at %.0f/%.0f",
            long_call_strike, long_put_strike
        )
        return None

    call_credit = round(
        max(short_call.get("bid", 0) - long_call.get("ask", 0), 0), 2
    )
    put_credit = round(
        max(short_put.get("bid", 0) - long_put.get("ask", 0), 0), 2
    )
    total_credit = round(call_credit + put_credit, 2)

    stop_per_side = round(wing_width * 0.10, 2)

    return {
        "short_call":       short_call,
        "long_call":        long_call,
        "short_put":        short_put,
        "long_put":         long_put,
        "call_credit":      call_credit,
        "put_credit":       put_credit,
        "total_credit":     total_credit,
        "stop_per_side":    stop_per_side,
        "wing_width":       wing_width,
        "defended_low":     short_put["strike"],
        "defended_high":    short_call["strike"],
        "defended_range":   round(short_call["strike"] - short_put["strike"], 0),
        "short_call_delta": round(abs(short_call.get("delta", 0)), 3),
        "short_put_delta":  round(abs(short_put.get("delta", 0)), 3),
        "selection_method": "GEX",
    }


def select_bic_strikes(
    chain: List[Dict],
    spx_price: float,
    wing_width: int,
    target_delta: float = BIC_SHORT_DELTA_TARGET,
    delta_min: float = BIC_SHORT_DELTA_MIN,
    delta_max: float = BIC_SHORT_DELTA_MAX,
) -> Optional[Dict]:
    """
    Select BIC strikes:
    - Short call: delta ~0.09 above price
    - Long call:  short_call + wing_width
    - Short put:  delta ~0.09 below price
    - Long put:   short_put - wing_width

    Returns dict with all 4 strikes and per-side credits.
    """
    calls = [o for o in chain if o.get("type") == "call" and o.get("strike", 0) > spx_price]
    puts  = [o for o in chain if o.get("type") == "put"  and o.get("strike", 0) < spx_price]

    short_call = find_strike_at_delta(calls, "call",
                                      target_delta=target_delta,
                                      delta_min=delta_min,
                                      delta_max=delta_max)
    short_put  = find_strike_at_delta(puts,  "put",
                                      target_delta=target_delta,
                                      delta_min=delta_min,
                                      delta_max=delta_max)

    if not short_call or not short_put:
        logger.warning("BIC: could not find valid short legs at delta %.2f", BIC_SHORT_DELTA_TARGET)
        return None

    long_call_strike = short_call["strike"] + wing_width
    long_put_strike  = short_put["strike"]  - wing_width

    long_call = next(
        (o for o in chain if o.get("type") == "call" and o.get("strike") == long_call_strike),
        None
    )
    long_put = next(
        (o for o in chain if o.get("type") == "put" and o.get("strike") == long_put_strike),
        None
    )

    if not long_call or not long_put:
        logger.warning(
            "BIC: long legs not found at %.0f/%.0f",
            long_call_strike, long_put_strike
        )
        return None

    call_credit = round(
        max(short_call.get("bid", 0) - long_call.get("ask", 0), 0), 2
    )
    put_credit = round(
        max(short_put.get("bid", 0) - long_put.get("ask", 0), 0), 2
    )
    total_credit = round(call_credit + put_credit, 2)

    stop_per_side = round(wing_width * 0.10, 2)   # BIC rule: stop at 10% of wing
    em = round(spx_price * (short_call["strike"] - short_put["strike"]) / spx_price, 0)

    return {
        "short_call":    short_call,
        "long_call":     long_call,
        "short_put":     short_put,
        "long_put":      long_put,
        "call_credit":   call_credit,
        "put_credit":    put_credit,
        "total_credit":  total_credit,
        "stop_per_side": stop_per_side,
        "wing_width":    wing_width,
        "defended_low":  short_put["strike"],
        "defended_high": short_call["strike"],
        "defended_range": round(short_call["strike"] - short_put["strike"], 0),
        "short_call_delta": round(abs(short_call.get("delta", 0)), 3),
        "short_put_delta":  round(abs(short_put.get("delta", 0)), 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AI VERDICT — Claude analysis
# ─────────────────────────────────────────────────────────────────────────────

def get_claude_verdict(
    spx: float,
    vix: float,
    strikes: Dict,
    entry_num: int,
    gex_regime: str = "UNKNOWN",
) -> str:
    """
    Calls Claude Haiku for GO/WAIT/SKIP verdict with reasoning.
    Matches the format of your existing BIC alert system.
    Returns formatted verdict string.
    """
    if not ANTHROPIC_API_KEY:
        return "VERDICT: GO ✅\n\nREASONING: AI analysis unavailable — entering on delta/credit criteria."

    time_remaining = max(0, round(((12 * 60 + 30) - (_now_pt().hour * 60 + _now_pt().minute)) / 60, 1))
    bp_required    = strikes["wing_width"] * 100

    prompt = f"""You are analyzing a BIC (Breakeven Iron Condor) 0DTE SPX trade setup.

MARKET CONDITIONS:
- SPX: {spx:.2f}
- VIX: {vix:.1f}
- GEX Regime: {gex_regime}
- Entry #{entry_num} of the day
- Time remaining to 12:30 PM PT hard close: {time_remaining:.1f} hrs

PROPOSED TRADE:
- Short Call: {strikes['short_call']['strike']:.0f} / Long Call: {strikes['long_call']['strike']:.0f}  Δ{strikes['short_call_delta']}  ${strikes['call_credit']*100:.0f}
- Short Put:  {strikes['short_put']['strike']:.0f}  / Long Put:  {strikes['long_put']['strike']:.0f}   Δ{strikes['short_put_delta']}  ${strikes['put_credit']*100:.0f}
- Total credit: ${strikes['total_credit']*100:.0f}/contract
- Defended zone: {strikes['defended_low']:.0f} – {strikes['defended_high']:.0f} ({strikes['defended_range']:.0f} pts)
- Wing width: {strikes['wing_width']}pt
- BP required: ${bp_required:,}/contract

RULES:
- GO if: VIX regime stable, delta balanced, credit adequate, time sufficient
- WAIT if: timing suboptimal but setup valid — still enter
- SKIP if: VIX unstable, delta imbalanced >25%, credit insufficient, <1hr remaining

Respond in exactly this format (2-3 sentences max for reasoning):
VERDICT: [GO ✅ / WAIT ⏳ / SKIP ❌]

REASONING: [Your analysis here]

ADJUSTMENTS: [Any suggested tweaks, or "None — proceed as planned"]"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if r.ok:
            data = r.json()
            if data.get("content") and len(data["content"]) > 0:
                return data["content"][0]["text"].strip()
            logger.warning("Claude verdict empty response: %s", data)
        else:
            logger.warning("Claude verdict failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("Claude verdict error: %s", e)

    return "VERDICT: GO ✅\n\nREASONING: AI analysis unavailable — entering on technical criteria."


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM ALERT FORMAT — matches your existing BIC alert style
# ─────────────────────────────────────────────────────────────────────────────

def format_bic_alert(
    entry_num: int,
    spx: float,
    vix: float,
    strikes: Dict,
    contracts: int,
    verdict: str,
    gex_regime: str,
    order_id: Optional[str],
    blocked_reason: Optional[str] = None,
) -> str:
    """
    Formats alert matching your existing BIC Telegram format.
    """
    now_pt = _now_pt()
    now_et = _now_et()
    time_str = f"{now_pt.strftime('%H:%M')} PT / {now_et.strftime('%H:%M')} ET"

    # Extract verdict line
    verdict_lines = verdict.strip().split("\n")
    verdict_line = next((l for l in verdict_lines if l.startswith("VERDICT:")), "VERDICT: GO ✅")
    verdict_short = verdict_line.replace("VERDICT:", "").strip()

    total_credit_dollars = round(strikes["total_credit"] * contracts * 100, 0)
    stop_value = round(strikes["stop_per_side"] * 100, 0)
    target_50  = round(total_credit_dollars * 0.50, 0)
    max_loss   = round(total_credit_dollars * 2, 0)
    bp_req     = strikes["wing_width"] * 100 * contracts

    if blocked_reason:
        return (
            f"⛔ <b>BIC SKIP #{entry_num}</b>  —  {time_str}\n"
            f"SPX {spx:.2f}  |  VIX {vix:.1f}\n"
            f"Reason: {blocked_reason}\n"
            f"<i>Next window in ~60 min</i>"
        )

    status = "🎯" if "GO" in verdict_short else "⏳"
    order_line = f"  Order ID:       {order_id}" if order_id else "  ⚠️ Order placement failed — check Tradier"

    return (
        f"{status} <b>BIC ENTRY #{entry_num}</b>  —  {time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>MARKET</b>\n"
        f"  SPX {spx:.2f}    VIX {vix:.1f}\n"
        f"  GEX {gex_regime}  |  Wing {strikes['wing_width']}pt\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TRADE LEGS</b>\n"
        f"  📉 SELL {strikes['short_put']['strike']:.0f}P  /  BUY {strikes['long_put']['strike']:.0f}P"
        f"    Δ{strikes['short_put_delta']}  ${strikes['put_credit']*100:.0f}\n"
        f"  📈 SELL {strikes['short_call']['strike']:.0f}C  /  BUY {strikes['long_call']['strike']:.0f}C"
        f"    Δ{strikes['short_call_delta']}  ${strikes['call_credit']*100:.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>RISK MANAGEMENT</b>\n"
        f"  Total credit    ${total_credit_dollars:.0f}  ({contracts}c)\n"
        f"  Stop per side   ${stop_value:.0f}  (spread value > ${strikes['stop_per_side']:.2f}/c)\n"
        f"  C1 target (50%) ${target_50:.0f}\n"
        f"  C2 target (70%) ${round(total_credit_dollars*0.70,0):.0f}\n"
        f"  Max loss (2×)   ${max_loss:.0f}\n"
        f"  BP required     ${bp_req:,}\n"
        f"  Defended        {strikes['defended_low']:.0f} – {strikes['defended_high']:.0f}"
        f"  ({strikes['defended_range']:.0f} pts)\n"
        f"  Hard exit:      12:30 PT\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>AI ANALYSIS</b>\n"
        f"{verdict}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"  {order_line}\n"
        f"<i>Wings: {strikes['wing_width']}pt  |  Exit: C1@50% C2@70% Free</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT SIZING WITH MARGIN CHECK
# ─────────────────────────────────────────────────────────────────────────────

def size_contracts(
    account_equity: float,
    vix: float,
    wing_width: int,
) -> int:
    """
    Calculate how many contracts to open.
    - Base: IC_CONTRACTS (3)
    - Scale down by VIX tier
    - Scale down if margin insufficient (BP = wing_width × 100 per contract)
    - Always at least 1 if gates pass
    """
    bp_per_contract = wing_width * 100   # e.g. 25pt = $2,500

    # VIX-adjusted base
    vix_adjusted = max(1, math.floor(IC_CONTRACTS * vix_size_multiplier(vix)))

    # Margin check: use max 30% of account equity for BP
    max_by_margin = max(1, math.floor((account_equity * 0.30) / bp_per_contract))

    contracts = min(vix_adjusted, max_by_margin, IC_CONTRACTS)

    logger.info(
        "BIC sizing: equity=$%.0f vix=%.1f wing=%dpt "
        "vix_adj=%d max_margin=%d → %dc",
        account_equity, vix, wing_width, vix_adjusted, max_by_margin, contracts
    )
    return contracts


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCAN + EXECUTE
# ─────────────────────────────────────────────────────────────────────────────

def run_bic_scan(
    entry_num: int,
    daily_state: Dict,
    account_equity: float,
    is_news_day: bool = False,
) -> Dict:
    """
    Full BIC scan and auto-execution cycle.
    Called at each scheduled entry window.

    Returns result dict with status and details.
    """
    logger.info("=== BIC SCAN #%d ===", entry_num)

    # ── Fetch market data ─────────────────────────────────────────────────
    spx = get_spx_price()
    vix = get_vix() or 0.0

    if not spx or spx == 0:
        msg = f"⚠️ BIC #{entry_num} — could not fetch SPX price"
        send(msg); logger.error(msg)
        return {"status": "error", "reason": "no_spx_price"}

    # ── Entry gates ───────────────────────────────────────────────────────
    allowed, reason = bic_entry_allowed(vix, is_news_day, daily_state, account_equity)
    if not allowed:
        logger.info("BIC blocked: %s", reason)
        now_pt = _now_pt()
        now_et = _now_et()
        time_str = f"{now_pt.strftime('%H:%M')} PT / {now_et.strftime('%H:%M')} ET"
        send(
            f"⛔ <b>BIC SKIP #{entry_num}</b>  —  {time_str}\n"
            f"SPX {spx:.2f}  |  VIX {vix:.1f}\n"
            f"Reason: {reason}\n"
            f"<i>Next window in ~60 min</i>"
        )
        return {"status": "blocked", "reason": reason}

    # ── Wing width + expiry ───────────────────────────────────────────────
    wing = get_wing_width(vix)
    expiry = _today()

    # ── Option chain ──────────────────────────────────────────────────────
    chain = get_option_chain(expiry, "all")
    if not chain:
        msg = f"⚠️ BIC #{entry_num} — option chain unavailable"
        send(msg); logger.error(msg)
        return {"status": "error", "reason": "no_chain"}

    # Initialize all local variables to prevent UnboundLocalError
    summary      = None
    strikes      = None
    anchors      = None
    verdict      = None
    order        = None
    order_id     = None
    delta_target = BIC_SHORT_DELTA_TARGET
    delta_min    = BIC_SHORT_DELTA_MIN
    delta_max    = BIC_SHORT_DELTA_MAX

    # ── Strike selection via FlashAlpha anchors ───────────────────────────
    # Use GEX walls + expected move to anchor strikes, then find nearest
    # chain strike. Falls back to delta search if FlashAlpha unavailable.
    if summary and summary.call_wall > 0:
        anchors = get_client().get_strike_anchors(spx, summary)
        if anchors.get("method") == "GEX":
            target_call = anchors["short_call"]
            target_put  = anchors["short_put"]
            wing        = anchors["wing_width"]
            logger.info(
                "GEX anchors: short_call=%.0f short_put=%.0f wing=%dpt em=±%.0f",
                target_call, target_put, wing, anchors.get("em", 0)
            )
            # Find nearest chain strikes to GEX anchor targets
            strikes = _select_strikes_near_target(
                chain, spx, target_call, target_put, wing
            )
            if strikes:
                logger.info(
                    "GEX strike selection: call=%s put=%s",
                    strikes.get("short_call", {}).get("strike"),
                    strikes.get("short_put", {}).get("strike")
                )

    # Fall through to delta search if GEX selection failed
    if not strikes:
        logger.info("Using delta-based strike selection (GEX unavailable or no match)")

    # Log chain diagnostics before delta selection attempt
    calls_in_chain = [o for o in chain if o.get("type") == "call"]
    puts_in_chain  = [o for o in chain if o.get("type") == "put"]
    calls_above    = [o for o in calls_in_chain if o.get("strike", 0) > spx]
    puts_below     = [o for o in puts_in_chain  if o.get("strike", 0) < spx]
    delta_calls    = [o for o in calls_above if BIC_SHORT_DELTA_MIN <= abs(o.get("delta", 0)) <= BIC_SHORT_DELTA_MAX]
    delta_puts     = [o for o in puts_below  if BIC_SHORT_DELTA_MIN <= abs(o.get("delta", 0)) <= BIC_SHORT_DELTA_MAX]

    logger.info(
        "BIC chain: total=%d calls_above_spx=%d puts_below_spx=%d "
        "delta_calls(%.2f-%.2f)=%d delta_puts=%d",
        len(chain), len(calls_above), len(puts_below),
        BIC_SHORT_DELTA_MIN, BIC_SHORT_DELTA_MAX,
        len(delta_calls), len(delta_puts)
    )
    if delta_calls:
        best_call = min(delta_calls, key=lambda o: abs(abs(o.get("delta",0)) - BIC_SHORT_DELTA_TARGET))
        logger.info("Best call candidate: strike=%.0f delta=%.3f bid=%.2f",
                    best_call["strike"], best_call.get("delta",0), best_call.get("bid",0))
    if delta_puts:
        best_put = min(delta_puts, key=lambda o: abs(abs(o.get("delta",0)) - BIC_SHORT_DELTA_TARGET))
        logger.info("Best put candidate:  strike=%.0f delta=%.3f bid=%.2f",
                    best_put["strike"], best_put.get("delta",0), best_put.get("bid",0))

    strikes = select_bic_strikes(
        chain, spx, wing,
        target_delta=delta_target,
        delta_min=delta_min,
        delta_max=delta_max
    )
    if not strikes:
        reason_detail = (
            f"calls_above={len(calls_above)} puts_below={len(puts_below)} "
            f"delta_calls={len(delta_calls)} delta_puts={len(delta_puts)}"
        )
        logger.warning("BIC no setup: %s", reason_detail)
        msg = (
            "BIC NO SETUP #" + str(entry_num) + " -- " +
            _now_pt().strftime("%H:%M") + " PT" + chr(10) +
            "SPX " + str(round(spx,2)) + " | VIX " + str(round(vix,1)) +
            " | Wing " + str(wing) + "pt" + chr(10) +
            "No delta " + str(BIC_SHORT_DELTA_MIN) + "-" +
            str(BIC_SHORT_DELTA_MAX) + " legs found" + chr(10) +
            reason_detail
        )
        send(msg)
        return {"status": "no_setup", "reason": "no_valid_strikes"}

    # Credit-to-VIX sanity check — dynamic minimum based on VIX level
    # Higher VIX = need more credit: 2x breach fires too fast on thin credits
    # VIX <17: $0.50 | VIX 17-20: $0.70 | VIX >20: $1.00
    min_credit = min_credit_for_vix(vix)
    if strikes["total_credit"] < min_credit:
        reason = (
            "credit $%.2f < VIX-adjusted min $%.2f (VIX=%.1f)" %
            (strikes["total_credit"], min_credit, vix)
        )
        logger.warning("BIC blocked: %s", reason)
        now_pt = _now_pt(); now_et = _now_et()
        time_str = now_pt.strftime("%H:%M") + " PT / " + now_et.strftime("%H:%M") + " ET"
        send(
            "⚠️ BIC SKIP #" + str(entry_num) + "  --  " + time_str + chr(10) +
            "Credit $" + str(round(strikes["total_credit"], 2)) +
            " < min $" + str(round(min_credit, 2)) +
            " for VIX " + str(round(vix, 1))
        )
        return {"status": "blocked", "reason": reason}

    # ── FlashAlpha summary — fresh fetch at each BIC window ─────────────────
    # force=True ensures one fresh API call per 60-min window (5 calls/day)
    # MIN_FORCE_INTERVAL (10 min) prevents burst on simultaneous window fires
    summary    = get_spx_summary(force=True)

    # ── FlashAlpha fallback gate ──────────────────────────────────────────────
    # If FlashAlpha is unavailable AND VIX >= 18 → skip entry.
    # At VIX >= 18 without regime confirmation we cannot distinguish
    # positive from negative gamma. Negative gamma + VIX >= 18 is a skip
    # condition — trading blind risks entering on the wrong side.
    # Backtest: blocks ~44 days/year, saves 8 breaches, -12.8pp max drawdown.
    # At VIX < 18 → allow entry with delta+VIX fallback (lower risk days).
    if summary is None and vix >= 18.0:
        reason = (
            "FlashAlpha unavailable + VIX %.1f >= 18 — "
            "cannot verify regime, skipping to avoid negative gamma risk" % vix
        )
        logger.warning("BIC blocked: %s", reason)
        now_pt = _now_pt(); now_et = _now_et()
        time_str = now_pt.strftime("%H:%M") + " PT / " + now_et.strftime("%H:%M") + " ET"
        send(
            "⛔ BIC SKIP #" + str(entry_num) + "  --  " + time_str + chr(10) +
            "SPX " + str(round(spx, 2)) + "  |  VIX " + str(round(vix, 1)) + chr(10) +
            "Reason: FlashAlpha down + VIX ≥ 18 — regime unknown"
        )
        return {"status": "blocked", "reason": "flashalpha_down_vix_elevated"}

    gex_regime = "⚪ UNKNOWN"
    if summary:
        if summary.regime == "positive_gamma":
            gex_regime = "🟢 GO"
        elif summary.regime == "negative_gamma":
            gex_regime = "🟡 CAUTION"
        else:
            gex_regime = "⚪ NEUTRAL"

        # Override wing width with FlashAlpha recommendation
        if summary.wing_width != wing:
            logger.info(
                "Wing width updated by FlashAlpha: %dpt → %dpt "
                "(expected_move=±%.0f, vix=%.1f)",
                wing, summary.wing_width, summary.expected_move, vix
            )
            wing = summary.wing_width

        logger.info(
            "FlashAlpha: regime=%s em=±%.0f call_wall=%.0f put_wall=%.0f "
            "gamma_flip=%.0f go=%s",
            summary.regime, summary.expected_move,
            summary.call_wall, summary.put_wall,
            summary.gamma_flip, summary.go_signal
        )

        # VVIX spike alert — warn but don't block (preserves trades)
        # VVIX > 100 = elevated vol-of-vol = potential spike day
        vvix = getattr(summary, 'vvix', None)
        if vvix and vvix > 100:
            logger.warning(
                "VVIX %.1f > 100 — elevated spike risk (alert only, not blocking)",
                vvix
            )
            # Flag in gex_regime for Claude verdict context
            gex_regime += " ⚠️VVIX%.0f" % vvix

    # ── Adaptive delta target ─────────────────────────────────────────────
    # Use ATM IV from FlashAlpha to determine optimal delta target
    # Lower IV → lower delta → further OTM → safer strikes
    if summary:
        atm_iv = summary.atm_iv
    else:
        atm_iv = vix / 100
    delta_target = adaptive_delta_target(atm_iv)
    delta_min    = max(0.03, delta_target - 0.03)
    delta_max    = min(0.15, delta_target + 0.04)
    logger.info(
        "Adaptive delta: atm_iv=%.1f%% → target=%.2f range=[%.2f, %.2f]",
        atm_iv * 100, delta_target, delta_min, delta_max
    )

    # ── Regime-based contract sizing ─────────────────────────────────────────
    # positive_gamma → full entry
    # negative_gamma + VIX < 18 → cautious (1 contract, wider wings)
    # negative_gamma + VIX ≥ 18 → skip
    #
    # Backtest basis: negative_gamma days have 81% win rate (vs 95%+ positive)
    # Sizing down protects capital while keeping the strategy active on mild days
    if summary and summary.regime == "negative_gamma":
        if vix < 18.0:
            # Enter cautiously — 1 contract, 45pt wings minimum
            contracts   = 1
            wing        = max(wing, 45)
            gex_regime  = "🟡 CAUTION (1c)"
            logger.info(
                "Negative gamma + VIX %.1f < 18 — reduced to 1c, wing=%dpt",
                vix, wing
            )
        else:
            # VIX ≥ 18 in negative gamma = too risky, skip
            reason = (
                "negative_gamma + VIX %.1f >= 18 — skipping "
                "(dealers amplify moves, elevated volatility)" % vix
            )
            logger.info("BIC blocked: %s", reason)
            now_pt = _now_pt(); now_et = _now_et()
            time_str = now_pt.strftime("%H:%M") + " PT / " + now_et.strftime("%H:%M") + " ET"
            send(
                "⛔ BIC SKIP #" + str(entry_num) + "  --  " + time_str + chr(10) +
                "SPX " + str(round(spx, 2)) + "  |  VIX " + str(round(vix, 1)) + chr(10) +
                "Reason: " + reason
            )
            return {"status": "blocked", "reason": "negative_gamma_high_vix"}
    else:
        contracts = size_contracts(account_equity, vix, wing)

    # ── Expected move validation ──────────────────────────────────────────────
    # Short strikes must be at least 80% of expected move away from SPX
    # Prevents entering ICs where strikes are inside the expected move
    # ── Duplicate strike gate ────────────────────────────────────────────────
    existing_short_puts, existing_short_calls = _get_existing_strikes()
    if strikes and (existing_short_puts or existing_short_calls):
        proposed_put  = float(strikes["short_put"]["strike"])
        proposed_call = float(strikes["short_call"]["strike"])
        if proposed_put in existing_short_puts:
            reason = (
                "Duplicate short put %.0f already open — "
                "concentration risk (late-start cascade prevention)" % proposed_put
            )
            logger.warning("BIC blocked: %s", reason)
            now_pt = _now_pt(); now_et = _now_et()
            time_str = now_pt.strftime("%H:%M") + " PT / " + now_et.strftime("%H:%M") + " ET"
            send(
                "⛔ BIC SKIP #" + str(entry_num) + "  --  " + time_str + chr(10) +
                "SPX " + str(round(spx, 2)) + chr(10) +
                "Reason: Duplicate short put " + str(round(proposed_put, 0)) +
                " already open"
            )
            return {"status": "blocked", "reason": "duplicate_strike"}
        if proposed_call in existing_short_calls:
            reason = (
                "Duplicate short call %.0f already open — "
                "concentration risk (late-start cascade prevention)" % proposed_call
            )
            logger.warning("BIC blocked: %s", reason)
            now_pt = _now_pt(); now_et = _now_et()
            time_str = now_pt.strftime("%H:%M") + " PT / " + now_et.strftime("%H:%M") + " ET"
            send(
                "⛔ BIC SKIP #" + str(entry_num) + "  --  " + time_str + chr(10) +
                "SPX " + str(round(spx, 2)) + chr(10) +
                "Reason: Duplicate short call " + str(round(proposed_call, 0)) +
                " already open"
            )
            return {"status": "blocked", "reason": "duplicate_strike"}

    if summary and summary.expected_move > 0 and strikes:
        # Time-adjusted EM — shrinks through the day as expiry approaches
        # 10:15 AM: ~94% of full EM remaining
        # 2:15 PM:  ~52% of full EM remaining
        if BIC_USE_TIME_ADJ_EM:
            now_et = _now_et()
            hours_since_open = (now_et.hour - 9) + (now_et.minute - 30) / 60
            hours_remaining  = max(BIC_TRADING_HOURS - hours_since_open, 0.5)
            import math as _math
            time_factor      = _math.sqrt(hours_remaining / BIC_TRADING_HOURS)
            adj_em           = summary.expected_move * time_factor
            logger.info(
                "EM time-adjusted: full=±%.0f hrs_remaining=%.2f "
                "factor=%.2f adj_em=±%.0f",
                summary.expected_move, hours_remaining, time_factor, adj_em
            )
        else:
            adj_em = summary.expected_move

        min_distance  = adj_em * BIC_EM_MULT
        short_call_k  = strikes["short_call"]["strike"]
        short_put_k   = strikes["short_put"]["strike"]
        call_distance = short_call_k - spx
        put_distance  = spx - short_put_k

        if call_distance < min_distance or put_distance < min_distance:
            reason = (
                "Strike inside expected move: "
                "call %.0f (%.0fpts, min %.0fpts) "
                "put %.0f (%.0fpts, min %.0fpts)" % (
                    short_call_k, call_distance, min_distance,
                    short_put_k,  put_distance,  min_distance
                )
            )
            logger.warning("BIC blocked: %s", reason)
            now_pt = _now_pt(); now_et2 = _now_et()
            time_str = now_pt.strftime("%H:%M") + " PT / " + now_et2.strftime("%H:%M") + " ET"
            send(
                "⛔ BIC SKIP #" + str(entry_num) + "  --  " + time_str + chr(10) +
                "SPX " + str(round(spx, 2)) + "  |  EM +-" + str(round(adj_em, 0)) + chr(10) +
                "Reason: " + reason
            )
            return {"status": "blocked", "reason": "strike_inside_em"}
        else:
            logger.info(
                "EM validation passed: call %.0fpts OTM (min %.0f) "
                "put %.0fpts OTM (min %.0f) [adj_em=±%.0f mult=%.2f]",
                call_distance, min_distance, put_distance, min_distance,
                adj_em, BIC_EM_MULT
            )

    # ── Claude verdict ────────────────────────────────────────────────────
    verdict = get_claude_verdict(spx, vix, strikes, entry_num, gex_regime)
    verdict_upper = verdict.upper()

    # Skip only on SKIP verdict
    if "SKIP" in verdict_upper and "GO" not in verdict_upper and "WAIT" not in verdict_upper:
        alert = format_bic_alert(
            entry_num, spx, vix, strikes, contracts,
            verdict, gex_regime, None,
            blocked_reason=f"AI verdict: SKIP — {verdict.split(chr(10))[2] if chr(10) in verdict else ''}"
        )
        send(alert)
        return {"status": "skip", "reason": "ai_skip"}

    # ── Place order ───────────────────────────────────────────────────────
    spread_id = str(uuid.uuid4())
    order_legs = [
        {"symbol": strikes["short_call"]["symbol"], "side": "sell_to_open", "quantity": contracts},
        {"symbol": strikes["long_call"]["symbol"],  "side": "buy_to_open",  "quantity": contracts},
        {"symbol": strikes["short_put"]["symbol"],  "side": "sell_to_open", "quantity": contracts},
        {"symbol": strikes["long_put"]["symbol"],   "side": "buy_to_open",  "quantity": contracts},
    ]

    order    = place_spread_order(order_legs, order_type="market")
    order_id = order.get("id") if order else None

    # ── Check for rejection ────────────────────────────────────────────────
    if order and order.get("_rejected"):
        reject_reason = order.get("_reject_reason", "Unknown")
        logger.error("BIC #%d ORDER REJECTED: %s", entry_num, reject_reason)
        now_pt = _now_pt(); now_et = _now_et()
        time_str = now_pt.strftime("%H:%M") + " PT / " + now_et.strftime("%H:%M") + " ET"
        send(
            "🚨 ORDER REJECTED #" + str(entry_num) + "  --  " + time_str + chr(10) +
            "SPX " + str(round(spx, 2)) + "  |  VIX " + str(round(vix, 1)) + chr(10) +
            "Reason: " + reject_reason + chr(10) +
            "Action: Check Tradier account permissions"
        )
        return {
            "status":   "order_rejected",
            "reason":   reject_reason,
            "order_id": order_id,
        }

    if not order_id:
        logger.error("BIC #%d order placement failed — no order ID returned", entry_num)
        now_pt = _now_pt()
        send(
            "⚠️ ORDER FAILED #" + str(entry_num) + "  --  " +
            now_pt.strftime("%H:%M") + " PT" + chr(10) +
            "No order ID returned from Tradier — check logs"
        )
        return {"status": "order_failed", "order_id": None}

    # ── Persist to DB ─────────────────────────────────────────────────────
    if order_id:
        tier = assign_tiers(contracts, 1, len(get_open_spreads(_today())) + 1)
        insert_spread({
            "id":              spread_id,
            "trade_date":      _today(),
            "setup_type":      "IC",
            "signal_grade":    "A" if "WAIT" in verdict_upper else "A+",
            "entry_time":      datetime.utcnow().isoformat(),
            "credit_received": strikes["total_credit"],
            "spread_width":    wing,
            "contracts":       contracts,
            "tier_assignment": json.dumps(tier),
            "notes": (
                f"BIC #{entry_num} | wing={wing}pt | "
                f"VIX={vix:.1f} | call_credit={strikes['call_credit']} "
                f"put_credit={strikes['put_credit']} | "
                f"gex={gex_regime}"
            ),
        })
        for leg_type, option, side_open in [
            ("SHORT_CALL", strikes["short_call"], "sell_to_open"),
            ("LONG_CALL",  strikes["long_call"],  "buy_to_open"),
            ("SHORT_PUT",  strikes["short_put"],  "sell_to_open"),
            ("LONG_PUT",   strikes["long_put"],   "buy_to_open"),
        ]:
            insert_leg({
                "id":              str(uuid.uuid4()),
                "spread_id":       spread_id,
                "leg_type":        leg_type,
                "strike":          option["strike"],
                "expiry":          expiry,
                "option_symbol":   option["symbol"],
                "entry_price":     option.get("mid", 0),
                "entry_delta":     option.get("delta", 0),
                "entry_iv":        option.get("iv", 0),
                "entry_theta":     option.get("theta", 0),
                "tradier_order_id": str(order_id),
            })

        logger.info(
            "BIC #%d entered: credit=%.2f contracts=%d wing=%dpt order=%s",
            entry_num, strikes["total_credit"], contracts, wing, order_id
        )

    # ── Send Telegram alert ───────────────────────────────────────────────
    alert = format_bic_alert(
        entry_num, spx, vix, strikes, contracts,
        verdict, gex_regime, str(order_id) if order_id else None
    )
    send(alert)

    return {
        "status":    "entered" if order_id else "order_failed",
        "spread_id": spread_id,
        "order_id":  order_id,
        "credit":    strikes["total_credit"],
        "contracts": contracts,
        "wing":      wing,
        "vix":       vix,
        "spx":       spx,
    }
