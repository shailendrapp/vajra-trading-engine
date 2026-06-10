"""
engine.py — SPX 0DTE Paper Trading Engine
==========================================
Supports two run modes:

  CRON MODE (short-lived, one phase per invocation — recommended):
    python engine.py --mode startup     # 6:25 AM PT  — init DB, morning brief
    python engine.py --mode bic_scan   # 10:15, 11:15, 12:15 ET — scan + entry
    python engine.py --mode eod_close  # 2:30 PM ET  — close all + summaries

  LEGACY (single persistent process — local testing only):
    python engine.py --dry-run

GitHub Actions workflow calls --mode, keeping each job under 5 minutes.
"""

import argparse
import calendar
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

from config import (
    POLL_INTERVAL_SECONDS, MARKET_CLOSE_PT,
    DAILY_SUMMARY_TIME_PT, WEEKLY_SUMMARY_DAY, STARTING_ACCOUNT_EQUITY,
    LOG_DIR, BIC_ENTRY_WINDOWS_ET
)
from core.database import (
    init_db, get_or_create_daily_state, update_daily_state,
    get_open_spreads, get_legs_for_spread, close_spread,
    get_daily_trades, insert_weekly_summary
)
from core.tradier import get_account_balance, get_vix
from core.risk_manager import update_daily_pnl
from modules.position_monitor import monitor_tick
from modules.bic_scanner import run_bic_scan
from modules.telegram_bot import (
    send, send_daily_summary, send_weekly_summary,
    CommandHandler, is_paused, pause_trading
)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(f"{LOG_DIR}/engine.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("engine")

PT = pytz.timezone("America/Los_Angeles")
ET = pytz.timezone("America/New_York")

# Manually add FOMC / high-impact news dates as YYYY-MM-DD strings
NEWS_DAYS: set = set()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now_pt() -> datetime:
    return datetime.now(PT)


def _today() -> str:
    return _now_pt().strftime("%Y-%m-%d")


def _hhmm_pt() -> str:
    return _now_pt().strftime("%H:%M")


def _get_equity() -> float:
    balance = get_account_balance()
    if balance and balance.get("total_equity"):
        equity = float(balance["total_equity"])
        logger.info("Account equity from Tradier: $%.2f", equity)
        return equity
    logger.warning("Could not fetch Tradier balance — using default $%.0f",
                   STARTING_ACCOUNT_EQUITY)
    return STARTING_ACCOUNT_EQUITY


def _load_news_calendar() -> set:
    logger.info("News calendar loaded — %d flagged dates", len(NEWS_DAYS))
    return NEWS_DAYS


# ─────────────────────────────────────────────────────────────────────────────
# CRON MODE: STARTUP  (6:25 AM PT)
# Runtime target: ~2 min
# ─────────────────────────────────────────────────────────────────────────────

def run_startup(dry_run: bool = False):
    """
    Initialize DB, send Engine Online message, morning brief.
    Runs once and exits — called by the 6:25 AM PT cron.
    """
    logger.info("=== STARTUP MODE ===")
    init_db()
    news_days  = _load_news_calendar()
    trade_date = _today()
    account_equity = _get_equity()

    get_or_create_daily_state(trade_date, account_equity)

    is_news_day = trade_date in news_days
    mode_str    = "DRY RUN" if dry_run else "PAPER"

    news_line = "⚠️ NEWS DAY — reduced size active" if is_news_day else "✅ Clear — no scheduled news"
    dry_line  = "\n⚠️ DRY RUN — no orders will be placed" if dry_run else ""

    send(
        f"🚀 <b>SPX 0DTE Engine online</b>\n"
        f"Date: {trade_date} | Mode: {mode_str}\n"
        f"Equity: ${account_equity:,.0f}\n"
        f"{news_line}"
        f"{dry_line}"
    )
    logger.info("Startup complete — exiting")


# ─────────────────────────────────────────────────────────────────────────────
# CRON MODE: BIC SCAN  (10:15, 11:15, 12:15 ET)
# Runtime target: ~3 min
# ─────────────────────────────────────────────────────────────────────────────

def run_bic_scan_mode(dry_run: bool = False):
    """
    Run one BIC scan and exit.
    Called independently at each entry window by the cron schedule.
    Uses get_daily_trades() to count prior entries so entry_num is correct
    even across separate job invocations.
    """
    logger.info("=== BIC SCAN MODE ===")
    init_db()
    trade_date     = _today()
    account_equity = _get_equity()
    daily_state    = get_or_create_daily_state(trade_date, account_equity)
    news_days      = _load_news_calendar()
    is_news_day    = trade_date in news_days

    now_et = datetime.now(ET).strftime("%H:%M")
    logger.info("BIC scan triggered at %s ET", now_et)

    if is_paused():
        logger.info("Engine paused — skipping BIC scan")
        send("⏸️ BIC scan skipped — engine paused")
        return

    if daily_state.get("circuit_breaker_hit") or daily_state.get("entries_halted"):
        logger.info("Circuit breaker / entries halted — skipping scan")
        send("🛑 BIC scan skipped — circuit breaker active")
        return

    # Count today's entries from DB so entry_num is correct across separate jobs
    prior_entries = get_daily_trades(trade_date)
    entry_num = len(prior_entries) + 1

    logger.info("Running BIC scan #%d (prior entries today: %d)",
                entry_num, len(prior_entries))

    if not dry_run:
        result = run_bic_scan(
            entry_num=entry_num,
            daily_state=daily_state,
            account_equity=account_equity,
            is_news_day=is_news_day,
        )
        status = result.get("status", "no_entry")
        if status == "entered":
            logger.info("BIC #%d entered — order=%s credit=%.2f",
                        entry_num, result.get("order_id"), result.get("credit", 0))
        else:
            logger.info("BIC #%d — no entry: %s", entry_num, result.get("reason", "n/a"))
    else:
        logger.info("[DRY RUN] BIC scan #%d skipped — no order placed", entry_num)
        send(f"🔍 [DRY RUN] BIC scan #{entry_num} at {now_et} ET — no order placed")

    logger.info("BIC scan complete — exiting")


# ─────────────────────────────────────────────────────────────────────────────
# CRON MODE: EOD CLOSE  (2:30 PM ET)
# Runtime target: ~3 min
# ─────────────────────────────────────────────────────────────────────────────

def run_eod_close(dry_run: bool = False):
    """
    Close all open positions, send daily/weekly/monthly/yearly summaries.
    Called by the 2:30 PM ET cron. Runs once and exits.
    """
    logger.info("=== EOD CLOSE MODE ===")
    init_db()
    trade_date     = _today()
    account_equity = _get_equity()
    daily_state    = get_or_create_daily_state(trade_date, account_equity)

    # ── Close all open positions ──────────────────────────────────────────────
    from core.tradier import place_close_order

    open_spreads = get_open_spreads(trade_date)

    if open_spreads:
        logger.info("Closing %d open position(s)", len(open_spreads))
        closed = 0
        for spread in open_spreads:
            legs = get_legs_for_spread(spread["id"])
            close_legs = []
            for leg in legs:
                side = ("buy_to_close"
                        if leg["leg_type"].startswith("SHORT")
                        else "sell_to_close")
                close_legs.append({
                    "symbol":   leg["option_symbol"],
                    "side":     side,
                    "quantity": spread["contracts"],
                })

            if not dry_run:
                order = place_close_order(close_legs, order_type="market")
                if order and order.get("id"):
                    pnl = close_spread(
                        spread["id"], 0.0, "EOD_CLOSE",
                        spread["credit_received"]
                    )
                    daily_state = update_daily_pnl(trade_date, daily_state, pnl)
                    closed += 1
                    logger.info("Closed spread %s — pnl=%.2f", spread["id"][:8], pnl)
                else:
                    logger.error("Failed to close spread %s", spread["id"][:8])
            else:
                logger.info("[DRY RUN] Would close spread %s", spread["id"][:8])
                closed += 1

        send(f"🔒 EOD: Closed {closed}/{len(open_spreads)} position(s)")
    else:
        logger.info("No open positions — nothing to close")

    # ── Refresh equity after closes ───────────────────────────────────────────
    balance = get_account_balance()
    if balance and balance.get("total_equity"):
        account_equity = float(balance["total_equity"])
        update_daily_state(trade_date, account_equity=account_equity)

    # ── Daily summary ─────────────────────────────────────────────────────────
    send_daily_summary(trade_date, account_equity)

    # ── Weekly summary (Fridays) ───────────────────────────────────────────────
    now_dt = _now_pt()
    if now_dt.weekday() == WEEKLY_SUMMARY_DAY:
        send_weekly_summary(STARTING_ACCOUNT_EQUITY, account_equity)

    # ── Monthly summary (last calendar day of month) ──────────────────────────
    last_day = calendar.monthrange(now_dt.year, now_dt.month)[1]
    if now_dt.day == last_day:
        from modules.telegram_bot import send_monthly_summary
        send_monthly_summary(account_equity)

    # ── Yearly summary (Dec 31) ───────────────────────────────────────────────
    if now_dt.month == 12 and now_dt.day == 31:
        from modules.telegram_bot import send_yearly_summary
        send_yearly_summary(STARTING_ACCOUNT_EQUITY, account_equity)

    logger.info("EOD close complete — exiting")


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY: PERSISTENT ENGINE  (local testing / manual runs only)
# ─────────────────────────────────────────────────────────────────────────────

class Engine:
    """
    Single long-running process. Kept for local --dry-run testing.
    Do NOT use this via GitHub Actions — it consumes ~400 min/day.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run                  = dry_run
        self._running                 = False
        self._daily_summary_sent      = False
        self._weekly_summary_sent     = False
        self._monthly_summary_sent    = False
        self._yearly_summary_sent     = False
        self._bic_windows_fired: set  = set()
        self._bic_entry_count: int    = 0
        self.account_equity           = STARTING_ACCOUNT_EQUITY
        self.daily_state: dict        = {}
        self.news_days: set           = set()
        self.cmd_handler: CommandHandler = CommandHandler(engine_ref=self)

    def start(self):
        logger.info("=" * 60)
        logger.info("SPX 0DTE Paper Engine starting — LEGACY mode dry_run=%s",
                    self.dry_run)
        logger.info("⚠️  Legacy mode uses ~400 min/day of GitHub Actions minutes.")
        logger.info("   Use --mode startup|bic_scan|eod_close for cron mode.")
        logger.info("=" * 60)

        init_db()
        self.news_days = _load_news_calendar()

        trade_date = _today()
        self.account_equity = _get_equity()
        self.daily_state = get_or_create_daily_state(trade_date, self.account_equity)

        mode_str = "DRY RUN" if self.dry_run else "PAPER"
        send(
            f"🚀 <b>SPX 0DTE Engine online</b>\n"
            f"Date: {trade_date} | Mode: {mode_str} (legacy)\n"
            f"Equity: ${self.account_equity:,.0f}\n"
            f"{'⚠️ DRY RUN — no orders placed' if self.dry_run else '✅ Ready to trade'}"
        )

        self.cmd_handler.start()
        self._running = True
        self._main_loop()

    def stop(self):
        logger.info("Engine shutting down")
        self._running = False
        self.cmd_handler.stop()
        send("🛑 Engine shutting down")

    def handle_signal(self, sig, frame):
        logger.info("Signal %s received — stopping", sig)
        self.stop()
        sys.exit(0)

    def _main_loop(self):
        logger.info("Entering main polling loop (interval=%ds)", POLL_INTERVAL_SECONDS)
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.exception("Unhandled error in main loop: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)
            if _hhmm_pt() >= MARKET_CLOSE_PT:
                logger.info("Market close time reached — stopping engine")
                self.stop()
                break

    def _tick(self):
        now_hhmm   = _hhmm_pt()
        trade_date = _today()
        is_news_day = trade_date in self.news_days

        now_et_hhmm = datetime.now(ET).strftime("%H:%M")
        for window_et in BIC_ENTRY_WINDOWS_ET:
            if now_et_hhmm >= window_et and window_et not in self._bic_windows_fired:
                self._bic_windows_fired.add(window_et)
                if not self.dry_run and not is_paused():
                    self._bic_entry_count += 1
                    result = run_bic_scan(
                        entry_num=self._bic_entry_count,
                        daily_state=self.daily_state,
                        account_equity=self.account_equity,
                        is_news_day=is_news_day,
                    )
                    if result.get("status") == "entered":
                        logger.info("BIC #%d entered: order=%s credit=%.2f",
                                    self._bic_entry_count,
                                    result.get("order_id"), result.get("credit", 0))
                else:
                    logger.info("[DRY RUN / PAUSED] BIC window %s ET skipped", window_et)

        if now_hhmm >= DAILY_SUMMARY_TIME_PT and not self._daily_summary_sent:
            self._send_eod_summary(trade_date)
            self._daily_summary_sent = True

        now_dt2  = _now_pt()
        last_day = calendar.monthrange(now_dt2.year, now_dt2.month)[1]
        if (now_dt2.day == last_day
                and now_hhmm >= DAILY_SUMMARY_TIME_PT
                and not self._monthly_summary_sent):
            from modules.telegram_bot import send_monthly_summary
            send_monthly_summary(self.account_equity)
            self._monthly_summary_sent = True

        now_dt3 = _now_pt()
        if (now_dt3.month == 12 and now_dt3.day == 31
                and now_hhmm >= DAILY_SUMMARY_TIME_PT
                and not self._yearly_summary_sent):
            from modules.telegram_bot import send_yearly_summary
            send_yearly_summary(STARTING_ACCOUNT_EQUITY, self.account_equity)
            self._yearly_summary_sent = True

        now_dt = _now_pt()
        if (now_dt.weekday() == WEEKLY_SUMMARY_DAY
                and now_hhmm >= DAILY_SUMMARY_TIME_PT
                and not self._weekly_summary_sent):
            balance = get_account_balance()
            equity_end = float(balance["total_equity"]) if balance else self.account_equity
            send_weekly_summary(STARTING_ACCOUNT_EQUITY, equity_end)
            self._weekly_summary_sent = True

        if now_hhmm < "06:30" or now_hhmm > "13:05":
            return

        if is_paused():
            logger.debug("Engine paused — skipping tick")
            return

        if not self.dry_run:
            self.daily_state = monitor_tick(
                daily_state=self.daily_state,
                account_equity=self.account_equity,
            )
        else:
            logger.debug("[DRY RUN] monitor_tick skipped")

    def _send_eod_summary(self, trade_date: str):
        balance = get_account_balance()
        if balance and balance.get("total_equity"):
            self.account_equity = float(balance["total_equity"])
            update_daily_state(trade_date, account_equity=self.account_equity)
        send_daily_summary(trade_date, self.account_equity)

    def emergency_close_all(self):
        """Called by Telegram /close_all command."""
        from core.tradier import place_close_order

        trade_date   = _today()
        open_spreads = get_open_spreads(trade_date)

        if not open_spreads:
            send("📭 No open positions to close.")
            return

        closed = 0
        for spread in open_spreads:
            legs = get_legs_for_spread(spread["id"])
            close_legs = []
            for leg in legs:
                side = ("buy_to_close"
                        if leg["leg_type"].startswith("SHORT")
                        else "sell_to_close")
                close_legs.append({
                    "symbol":   leg["option_symbol"],
                    "side":     side,
                    "quantity": spread["contracts"],
                })

            if not self.dry_run:
                order = place_close_order(close_legs, order_type="market")
                if order and order.get("id"):
                    pnl = close_spread(
                        spread["id"], 0.0, "EMERGENCY_CLOSE",
                        spread["credit_received"]
                    )
                    self.daily_state = update_daily_pnl(
                        trade_date, self.daily_state, pnl
                    )
                    closed += 1
            else:
                logger.info("[DRY RUN] Would close spread %s", spread["id"][:8])
                closed += 1

        send(f"🚨 Emergency close complete — {closed}/{len(open_spreads)} positions closed")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SPX 0DTE Paper Trading Engine")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate logic without placing any orders"
    )
    parser.add_argument(
        "--mode", default=None,
        choices=["startup", "bic_scan", "eod_close"],
        help=(
            "Cron mode — runs one phase and exits:\n"
            "  startup   → 6:25 AM PT  (init + morning brief)\n"
            "  bic_scan  → 10:15/11:15/12:15 ET  (scan + entry)\n"
            "  eod_close → 2:30 PM ET  (close all + summaries)"
        )
    )
    args = parser.parse_args()

    if args.mode == "startup":
        run_startup(dry_run=args.dry_run)
    elif args.mode == "bic_scan":
        run_bic_scan_mode(dry_run=args.dry_run)
    elif args.mode == "eod_close":
        run_eod_close(dry_run=args.dry_run)
    else:
        # Legacy persistent mode — local testing only
        engine = Engine(dry_run=args.dry_run)
        signal.signal(signal.SIGINT,  engine.handle_signal)
        signal.signal(signal.SIGTERM, engine.handle_signal)
        engine.start()


if __name__ == "__main__":
    main()
