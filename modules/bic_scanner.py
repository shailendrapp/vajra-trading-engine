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

    return True, "Gates passed"


# ─────────────────────────────────────────────────────────────────────────────
# STRIKE SELECTION — BIC DELTA METHOD
# ─────────────────────────────────────────────────────────────────────────────

def find_strike_at_delta(
    chain: List[Dict],
    option_type: str,
    target_delta: float = BIC_SHORT_DELTA_TARGET,
) -> Optional[Dict]:
    """
    Find the option in the chain where abs(delta) is closest to target_delta
    and within [BIC_SHORT_DELTA_MIN, BIC_SHORT_DELTA_MAX].
    """
    candidates = [
        o for o in chain
        if o.get("type") == option_type
        and BIC_SHORT_DELTA_MIN <= abs(o.get("delta", 0)) <= BIC_SHORT_DELTA_MAX
        and o.get("bid", 0) > 0.05
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda o: abs(abs(o.get("delta", 0)) - target_delta))
    return candidates[0]


def select_bic_strikes(
    chain: List[Dict],
    spx_price: float,
    wing_width: int,
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

    short_call = find_strike_at_delta(calls, "call")
    short_put  = find_strike_at_delta(puts,  "put")

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
            return r.json()["content"][0]["text"].strip()
        else:
            logger.warning("Claude verdict failed: %s", r.status_code)
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
        send(format_bic_alert(
            entry_num, spx, vix, {}, 0, "", "—", None,
            blocked_reason=reason
        ))
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

    # ── Strike selection ──────────────────────────────────────────────────
    strikes = select_bic_strikes(chain, spx, wing)
    if not strikes:
        msg = (
            f"⚠️ <b>BIC NO SETUP #{entry_num}</b>  —  {_now_pt().strftime('%H:%M')} PT\n"
            f"SPX {spx:.2f}  |  VIX {vix:.1f}  |  Wing {wing}pt\n"
            f"No delta {BIC_SHORT_DELTA_MIN}–{BIC_SHORT_DELTA_MAX} legs with adequate credit.\n"
            f"<i>Next window in ~60 min</i>"
        )
        send(msg)
        return {"status": "no_setup", "reason": "no_valid_strikes"}

    # Credit check
    if strikes["total_credit"] < BIC_MIN_CREDIT:
        msg = (
            f"⚠️ BIC #{entry_num} — credit ${strikes['total_credit']:.2f} "
            f"below minimum ${BIC_MIN_CREDIT:.2f}"
        )
        send(msg)
        return {"status": "blocked", "reason": "low_credit"}

    # ── Contract sizing ───────────────────────────────────────────────────
    contracts = size_contracts(account_equity, vix, wing)

    # ── GEX regime ────────────────────────────────────────────────────────
    try:
        from core.flashalpha import get_client
        gex_client = get_client()
        gex = gex_client.get_gex_context(spx, expiration=expiry)
        gex_regime = "🟢 GO" if gex and gex.net_gex > 0 else "🟡 CAUTION"
    except Exception:
        gex_regime = "⚪ UNKNOWN"

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

    order = place_spread_order(order_legs, order_type="market")
    order_id = order.get("id") if order else None

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
