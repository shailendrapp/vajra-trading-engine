"""
Module: risk_manager.py
Entry gate validation, circuit breaker, cooling-off, and contract sizing.
All go/no-go decisions for trade entry flow through here.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
import math
import pytz

from config import (
    DAILY_MAX_LOSS_PCT, COOLOFF_MINUTES, MAX_STOPOUTS_PER_DAY,
    VIX_KILL_SWITCH, VIX_SIZE_TIERS, VALID_SIGNAL_GRADES, ENTRY_WINDOWS_ET,
    RISK_PCT_PER_TRADE, SPREAD_WIDTH_PTS, MAX_CONCURRENT_SPREADS,
    SCALE_UP_AFTER_N_WINS
)
from core.database import update_daily_state, get_open_spreads

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
# VIX SIZE MULTIPLIER
# ─────────────────────────────────────────────────────────────────────────────

def vix_size_multiplier(vix: float) -> float:
    """
    Returns a contract size multiplier (0.25 – 1.0) based on current VIX.
    Derived from backtest: win rate drops ~17pp per VIX tier above 20.

    VIX < 20   → 1.00  (full size — 88–92% win rate zone)
    VIX 20–25  → 0.50  (half size — 75% win rate zone)
    VIX 25–30  → 0.25  (quarter size — 47% win rate zone)
    VIX >= 30  → kill switch fires before we ever get here
    """
    for threshold, multiplier in VIX_SIZE_TIERS:
        if vix < threshold:
            return multiplier
    # Fallback: VIX at or above the highest tier (shouldn't reach here — kill switch fires first)
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT SIZING
# ─────────────────────────────────────────────────────────────────────────────

def calculate_contracts(account_equity: float, consecutive_win_days: int = 0,
                         vix: float = 0.0) -> int:
    """
    Contracts = floor((equity × risk%) / (spread_width × $100))
               × VIX size multiplier
               + win-streak scale-up bonus

    VIX multiplier is applied BEFORE the scale-up bonus, then floored.
    Minimum 1 contract always (unless VIX kills it entirely).

    Examples at $50K:
      VIX 17, 0-day streak  → floor(0.50 × 1.00) + 0 = 1c  (base)
      VIX 22, 0-day streak  → floor(0.50 × 0.50) + 0 = 1c  (min floor saves it)
      VIX 27, 0-day streak  → floor(0.50 × 0.25) + 0 = 1c  (min floor)
      VIX 17, 10-day streak → floor(0.50 × 1.00) + 1 = 2c
      VIX 22, 10-day streak → floor(0.25 × 1.00) + 1... wait — multiplier applied to base
    """
    base_raw     = (account_equity * RISK_PCT_PER_TRADE) / (SPREAD_WIDTH_PTS * 100)
    multiplier   = vix_size_multiplier(vix) if vix > 0 else 1.0
    base_sized   = math.floor(base_raw * multiplier)
    base_sized   = max(base_sized, 1)

    scale_bonus  = consecutive_win_days // SCALE_UP_AFTER_N_WINS
    total        = base_sized + scale_bonus

    logger.info(
        "Sizing: equity=$%.0f base_raw=%.2f vix=%.1f multiplier=%.2f "
        "base_sized=%d scale_bonus=%d total=%dc",
        account_equity, base_raw, vix, multiplier, base_sized, scale_bonus, total
    )
    return total


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY GATE
# ─────────────────────────────────────────────────────────────────────────────

class EntryGateResult:
    def __init__(self, allowed: bool, reason: str):
        self.allowed = allowed
        self.reason  = reason

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        status = "✅ GO" if self.allowed else "🚫 BLOCKED"
        return f"{status}: {self.reason}"


def check_entry_gates(
    vix: float,
    signal_grade: str,
    is_news_day: bool,
    daily_state: Dict,
) -> EntryGateResult:
    """
    Run all entry gate checks in order of severity.
    Returns EntryGateResult — check .allowed and .reason.
    """

    # 1. VIX kill switch
    if vix >= VIX_KILL_SWITCH:
        return EntryGateResult(False, f"VIX {vix:.1f} ≥ {VIX_KILL_SWITCH} kill switch")

    # 2. Circuit breaker — daily loss cap already hit
    if daily_state.get("circuit_breaker_hit"):
        return EntryGateResult(False, "Daily circuit breaker triggered — no more entries today")

    # 3. Entries explicitly halted (2nd stop-out)
    if daily_state.get("entries_halted"):
        return EntryGateResult(False, "Entries halted after 2nd stop-out today")

    # 4. Cooling-off period
    cooloff_until = daily_state.get("cooloff_until")
    if cooloff_until:
        cooloff_dt = datetime.fromisoformat(cooloff_until).replace(tzinfo=pytz.utc)
        if datetime.now(pytz.utc) < cooloff_dt:
            remaining = int((cooloff_dt - datetime.now(pytz.utc)).total_seconds() / 60)
            return EntryGateResult(False, f"Cooling off — {remaining}m remaining after stop-out")

    # 5. News day
    if is_news_day:
        return EntryGateResult(False, "News day (FOMC/CPI/NFP) — auto-entry blocked")

    # 6. Signal grade
    if signal_grade not in VALID_SIGNAL_GRADES:
        return EntryGateResult(False, f"Signal grade {signal_grade!r} below threshold — need A or A+")

    # 7. Concurrent position limit
    open_spreads = get_open_spreads(_today())
    if len(open_spreads) >= MAX_CONCURRENT_SPREADS:
        return EntryGateResult(False, f"Max concurrent spreads ({MAX_CONCURRENT_SPREADS}) already open")

    # 8. Time window check (ET)
    now_et   = _now_et()
    now_hhmm = now_et.strftime("%H:%M")
    in_window = any(start <= now_hhmm <= end for start, end in ENTRY_WINDOWS_ET)
    if not in_window:
        windows_str = " or ".join(f"{s}–{e} ET" for s, e in ENTRY_WINDOWS_ET)
        return EntryGateResult(False, f"Outside entry windows ({windows_str})")

    # 9. VIX soft limit — allowed but log the sizing reduction
    multiplier = vix_size_multiplier(vix)
    if multiplier < 1.0:
        logger.info(
            "VIX soft limit active: vix=%.1f multiplier=%.2f — contracts reduced",
            vix, multiplier
        )

    return EntryGateResult(True, f"All gates passed (VIX={vix:.1f}, size={multiplier:.0%})")


# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER UPDATES
# ─────────────────────────────────────────────────────────────────────────────

def record_stopout(trade_date: str, daily_state: Dict) -> Dict:
    """
    Called after any position is stopped out.
    Updates stopout count, sets cooling-off, halts entries if 2nd stopout.
    Returns updated daily_state dict.
    """
    stopout_count = daily_state.get("stopout_count", 0) + 1
    cooloff_until = (
        datetime.utcnow() + timedelta(minutes=COOLOFF_MINUTES)
    ).isoformat()

    updates = {
        "stopout_count":  stopout_count,
        "cooloff_until":  cooloff_until,
    }

    if stopout_count >= MAX_STOPOUTS_PER_DAY:
        updates["entries_halted"] = 1
        logger.warning("2nd stop-out today — entries halted for rest of day")

    update_daily_state(trade_date, **updates)
    daily_state.update(updates)
    return daily_state


def check_and_apply_circuit_breaker(
    trade_date: str,
    daily_state: Dict,
    account_equity: float,
) -> bool:
    """
    Checks if daily P&L loss has exceeded the daily max loss cap.
    Returns True if circuit breaker just fired (or was already on).
    """
    if daily_state.get("circuit_breaker_hit"):
        return True

    daily_pnl = daily_state.get("daily_pnl", 0.0)
    max_loss   = account_equity * DAILY_MAX_LOSS_PCT

    if daily_pnl <= -abs(max_loss):
        logger.warning(
            "⚡ CIRCUIT BREAKER: daily P&L $%.2f ≤ -$%.2f (%.0f%% of equity)",
            daily_pnl, max_loss, DAILY_MAX_LOSS_PCT * 100
        )
        update_daily_state(trade_date, circuit_breaker_hit=1, entries_halted=1)
        daily_state["circuit_breaker_hit"] = 1
        daily_state["entries_halted"]      = 1
        return True

    return False


def update_daily_pnl(trade_date: str, daily_state: Dict, pnl_delta: float) -> Dict:
    """Add pnl_delta to daily P&L and persist."""
    new_pnl = daily_state.get("daily_pnl", 0.0) + pnl_delta
    update_daily_state(trade_date, daily_pnl=new_pnl)
    daily_state["daily_pnl"] = new_pnl
    return daily_state
