"""
engine.py — SPX 0DTE Paper Trading Engine
==========================================
Main orchestrator. Runs a persistent process from 6:30 AM to 1:15 PM PT.

Lifecycle:
  1. Startup: init DB, load daily state, send "Engine online" message
  2. Pre-market (until 9:30 AM ET): load VIX, check news calendar
  3. Market hours: 45s poll loop — monitor positions + accept entry signals
  4. Hard close (12:30 PM PT): close all open positions
  5. EOD (1:05 PM PT): send daily summary
  6. Friday EOD: send weekly summary
  7. Shutdown: 1:15 PM PT

Trade entry is triggered via Telegram command:
  /enter IC        — Iron Condor, grade A
  /enter IC A+     — Iron Condor, grade A+
  /enter BEAR_CALL — Bear Call Spread
  /enter BULL_PUT  — Bull Put Spread

Usage:
  python engine.py                  # run live (paper mode)
  python engine.py --dry-run        # smoke test, no orders placed
"""

import argparse
import logging
import signal
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
import pytz

from config import (
    POLL_INTERVAL_SECONDS, MARKET_OPEN_PT, MARKET_CLOSE_PT,
    DAILY_SUMMARY_TIME_PT, WEEKLY_SUMMARY_DAY, STARTING_ACCOUNT_EQUITY,
    NEWS_DAY_SYMBOLS, LOG_DIR
)
from core.database import init_db, get_or_create_daily_state, update_daily_state
from core.tradier import get_account_balance, get_vix
from modules.position_monitor import monitor_tick
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


def _now_pt() -> datetime:
    return datetime.now(PT)


def _today() -> str:
    return _now_pt().strftime("%Y-%m-%d")


def _hhmm_pt() -> str:
    return _now_pt().strftime("%H:%M")


# ─────────────────────────────────────────────────────────────────────────────
# NEWS CALENDAR (stub — replace with real economic calendar API)
# ─────────────────────────────────────────────────────────────────────────────

NEWS_DAYS: set = set()   # populated at startup

def load_news_calendar() -> set:
    """
    Stub: returns today's date if any known news event is scheduled.
    In production: integrate with Tradier's calendar or econoday API.
    """
    # For now, operator manually adds dates as YYYY-MM-DD strings
    # e.g. NEWS_DAYS.add("2026-06-12")  on FOMC days
    logger.info("News calendar loaded — %d flagged dates", len(NEWS_DAYS))
    return NEWS_DAYS


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, dry_run: bool = False):
        self.dry_run          = dry_run
        self._running         = False
        self._daily_summary_sent  = False
        self._weekly_summary_sent = False
        self.account_equity   = STARTING_ACCOUNT_EQUITY
        self.daily_state: dict = {}
        self.news_days: set   = set()
        self.cmd_handler: CommandHandler = CommandHandler(engine_ref=self)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        logger.info("=" * 60)
        logger.info("SPX 0DTE Paper Engine starting — mode=%s dry_run=%s",
                    "PAPER", self.dry_run)
        logger.info("=" * 60)

        init_db()
        self.news_days = load_news_calendar()

        # Load / create today's state
        trade_date = _today()
        balance = get_account_balance()
        if balance and balance.get("total_equity"):
            self.account_equity = float(balance["total_equity"])
            logger.info("Account equity from Tradier: $%.2f", self.account_equity)
        else:
            logger.warning(
                "Could not fetch Tradier balance — using configured default $%.0f",
                self.account_equity
            )

        self.daily_state = get_or_create_daily_state(trade_date, self.account_equity)

        send(
            f"🚀 *SPX 0DTE Engine online*\n"
            f"Date: {trade_date} | Mode: PAPER\n"
            f"Equity: ${self.account_equity:,.0f}\n"
            f"{'⚠️ DRY RUN — no orders placed' if self.dry_run else '✅ Ready to trade'}"
        )

        # Start Telegram command listener
        self.cmd_handler.start()

        self._running = True
        self._main_loop()

    def stop(self):
        logger.info("Engine shutting down")
        self._running = False
        self.cmd_handler.stop()
        send("🛑 Engine shutting down")

    # ── signal handlers ───────────────────────────────────────────────────────

    def handle_signal(self, sig, frame):
        logger.info("Signal %s received — stopping", sig)
        self.stop()
        sys.exit(0)

    # ── main loop ─────────────────────────────────────────────────────────────

    def _main_loop(self):
        logger.info("Entering main polling loop (interval=%ds)", POLL_INTERVAL_SECONDS)

        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.exception("Unhandled error in main loop: %s", e)

            time.sleep(POLL_INTERVAL_SECONDS)

            # Check shutdown time
            if _hhmm_pt() >= MARKET_CLOSE_PT:
                logger.info("Market close time reached — stopping engine")
                self.stop()
                break

    def _tick(self):
        now_hhmm = _hhmm_pt()
        trade_date = _today()
        is_news_day = trade_date in self.news_days

        # ── Daily summary ─────────────────────────────────────────────────────
        if now_hhmm >= DAILY_SUMMARY_TIME_PT and not self._daily_summary_sent:
            self._send_eod_summary(trade_date)
            self._daily_summary_sent = True

        # ── Weekly summary (Friday) ───────────────────────────────────────────
        now_dt = _now_pt()
        if (now_dt.weekday() == WEEKLY_SUMMARY_DAY
                and now_hhmm >= DAILY_SUMMARY_TIME_PT
                and not self._weekly_summary_sent):
            balance = get_account_balance()
            equity_end = float(balance["total_equity"]) if balance else self.account_equity
            send_weekly_summary(STARTING_ACCOUNT_EQUITY, equity_end)
            self._weekly_summary_sent = True

        # ── Outside market hours — skip monitor ───────────────────────────────
        if now_hhmm < "06:30" or now_hhmm > "13:05":
            return

        # ── Paused ────────────────────────────────────────────────────────────
        if is_paused():
            logger.debug("Engine paused — skipping tick")
            return

        # ── Position monitor (every tick) ─────────────────────────────────────
        if not self.dry_run:
            self.daily_state = monitor_tick(
                daily_state    = self.daily_state,
                account_equity = self.account_equity,
            )
        else:
            logger.debug("[DRY RUN] monitor_tick skipped")

    def _send_eod_summary(self, trade_date: str):
        balance = get_account_balance()
        if balance and balance.get("total_equity"):
            self.account_equity = float(balance["total_equity"])
            update_daily_state(trade_date, account_equity=self.account_equity)
        send_daily_summary(trade_date, self.account_equity)

    # ── Telegram-triggered actions ────────────────────────────────────────────

    def emergency_close_all(self):
        """Called by Telegram /close_all command."""
        from modules.position_monitor import _is_hard_close_time
        from core.database import get_open_spreads, get_legs_for_spread
        from core.tradier import place_close_order
        from core.database import close_spread
        from core.risk_manager import update_daily_pnl

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
                side = "buy_to_close" if leg["leg_type"].startswith("SHORT") else "sell_to_close"
                close_legs.append({
                    "symbol":   leg["option_symbol"],
                    "side":     side,
                    "quantity": spread["contracts"],
                })

            if not self.dry_run:
                order = place_close_order(close_legs, order_type="market")
                if order and order.get("id"):
                    pnl = close_spread(
                        spread["id"], 0.0, "EMERGENCY_CLOSE", spread["credit_received"]
                    )
                    self.daily_state = update_daily_pnl(trade_date, self.daily_state, pnl)
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
        help="Validate all logic without placing any orders"
    )
    args = parser.parse_args()

    engine = Engine(dry_run=args.dry_run)

    # Graceful shutdown on SIGINT / SIGTERM
    signal.signal(signal.SIGINT,  engine.handle_signal)
    signal.signal(signal.SIGTERM, engine.handle_signal)

    engine.start()


if __name__ == "__main__":
    main()
