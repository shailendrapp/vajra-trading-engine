"""
engine.py — SPX 0DTE Paper Trading Engine
==========================================
Supports two run modes:

  LEGACY (single persistent process):
    python engine.py --dry-run

  CRON MODE (short-lived, one phase per invocation):
    python engine.py --mode startup     # 6:25 AM PT — init, morning brief
    python engine.py --mode bic_scan   # 10:15, 11:15, 12:15 ET — scan + entry
    python engine.py --mode eod_close  # 2:30 PM ET — close all + summary
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
    NEWS_DAY_SYMBOLS, LOG_DIR, BIC_ENTRY_WINDOWS_ET
)
from core.database import init_db, get_or_create_daily_state, update_daily_state
from core.tradier import get_account_balance, get_vix
from modules.position_monitor import monitor_tick
from modules.bic_scanner import run_bic_scan
from modules.telegram_bot import (
    send, send_daily_summary, send_weekly_summary,
    CommandHandler, is_paused, pause_trading
)

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

NEWS_DAYS: set = set()


def _now_pt() -> datetime:
    return datetime.now(PT)


def _today() -> str:
    return _now_pt().strftime("%Y-%m-%d")


def _hhmm_pt() -> str:
    return _now_pt().strftime("%H:%M")


def load_news_calendar() -> set:
    logger.info("News calendar loaded — %d flagged dates", len(NEWS_DAYS))
    return NEWS_DAYS


def _get_equity() -> float:
    balance = get_account_balance()
    if balance and balance.get("total_equity"):
        equity = float(balance["total_equity"])
        logger.info("Account equity from Tradier: $%.2f", equity)
        return equity
    logger.warning("Could not fetch Tradier balance — using default $%.0f",
                   STARTING_ACCOUNT_EQUITY)
    return STARTING_ACCOUNT_EQUITY


# ─────────────────────────────────────────────────────────────────────────────
# CRON MODES — each runs once and exits
# ─────────────────────────────────────────────────────────────────────────────

def run_startup(dry_run: bool = False):
    """
    6:25 AM PT — initialize DB, send Engine Online message, morning brief.
    Exits immediately after. Runtime: ~1-2 min.
    """
    logger.info("=== STARTUP MODE ===")
    init_db()
    news_days = load_news_calendar()
    trade_date = _today()
    account_equity = _get_equity()

    daily_state = get_or_create_daily_state(trade_date, account_equity)
    is_news_day = trade_date in news_days

    mode_str = "DRY RUN" if dry_run else "PAPER"
    send(
        "🚀 <b>SPX 0DTE Engine online</b>\n"
        f"Date: {trade_date} | Mode: {mode_str}\n"
        f"Equity: ${account_equity:,.0f}\n"
        f"{'⚠️ NEWS DAY — reduced size' if is_news_day else '✅ Ready to trade'}\n"
        f"{'⚠️ DRY RUN — no orders placed' if dry_run else ''}"
    )
    logger.info("Startup complete — exiting")


def run_bic_scan_mode(dry_run: bool = False):
    """
    10:15, 11:15, 12:15 ET — run one BIC scan and exit.
    Runtime: ~2-3 min.
    """
    logger.info("=== BIC SCAN MODE ===")
    init_db()
    trade_date = _today()
    account_equity = _get_equity()
    daily_state = get_or_create_daily_state(trade_date, account_equity)
    news_days = load_news_calendar()
    is_news_day = trade_date in news_days

    # Determine which window this is based on current ET time
    now_et = datetime.now(ET).strftime("%H:%M")
    logger.info("BIC scan triggered at %s ET", now_et)

    if is_paused():
        logger.info("Engine paused — skipping BIC scan")
        send("⏸️ BIC scan skipped — engine paused")
        return

    # Count prior entries today from DB
    from core.database import get_open_spreads, get_all_spreads_today
    prior_entries = len(get_all_spreads_today(trade_date))
    entry_num = prior_entries + 1

    if not dry_run:
        result = run_bic_scan(
            entry_num=entry_num,
            daily_state=daily_state,
            account_equity=account_equity,
            is_news_day=is_news_day,
        )
        if result.get("status") == "entered":
            logger.info("BIC #%d entered: order=%s credit=%.2f",
                        entry_num, result.get("order_id"), result.get("credit", 0))
        else:
            logger.info("BIC #%d — no entry: %s", entry_num, result.get("reason", "unknown"))
    else:
        logger.info("[DRY RUN] BIC scan #%d skipped", entry_num)

    logger.info("BIC scan complete — exiting")


def run_eod_close(dry_run: bool = False):
    """
    2:30 PM ET — close all open positions, send daily/weekly summary.
    Runtime: ~2-3 min.
    """
    logger.info("=== EOD CLOSE MODE ===")
    init_db()
    trade_date = _today()
    account_equity = _get_equity()
    daily_state = get_or_create_daily_state(trade_date, account_equity)

    from core.database import get_open_spreads, get_legs_for_spread, close_spread
    from core.tradier import place_close_order
    from core.risk_manager import update_daily_pnl

    open_spreads = get_open_spreads(trade_date)

    if open_spreads:
        logger.info("Closing %d open position(s)", len(open_spreads))
        closed = 0
        for spread in open_spreads:
            legs = get_legs_for_spread(spread["id"])
            close_legs = []
            for leg in legs:
                side = "buy_to_close" if leg["leg_type"].startswith("SHORT") else "sell_to_close"
                close_legs.append({
                    "symbol": leg["option_symbol"],
                    "side": side,
                    "quantity": spread["contracts"],
                })
            if not dry_run:
                order = place_close_order(close_legs, order_type="market")
                if order and order.get("id"):
                    pnl = close_spread(spread["id"], 0.0, "EOD_CLOSE",
                                       spread["credit_received"])
                    daily_state = update_daily_pnl(trade_date, daily_state, pnl)
                    closed += 1
            else:
                logger.info("[DRY RUN] Would close spread %s", spread["id"][:8])
                closed += 1
        send(f"🔒 EOD: Closed {closed}/{len(open_spreads)} positions")
    else:
        logger.info("No open positions to close")

    # Update equity and send summaries
    balance = get_account_balance()
    if balance and balance.get("total_equity"):
        account_equity = float(balance["total_equity"])
        update_daily_state(trade_date, account_equity=account_equity)

    send_daily_summary(trade_date, account_equity)

    # Weekly summary on Fridays
    now_dt = _now_pt()
    if now_dt.weekday() == WEEKLY_SUMMARY_DAY:
        send_weekly_summary(STARTING_ACCOUNT_EQUITY, account_equity)

    # Monthly summary on last trading day
    import calendar
    last_day = calendar.monthrange(now_dt.year, now_dt.month)[1]
    if now_dt.day == last_day:
        from modules.telegram_bot import send_monthly_summary
        send_monthly_summary(account_equity)

    logger.info("EOD close complete — exiting")


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY PERSISTENT MODE (kept for backward compat / manual use)
# ─────────────────────────────────────────────────────────────────────────────

class Engine:
    # ... (keep your existing Engine class exactly as-is for --dry-run testing)
    pass


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SPX 0DTE Paper Trading Engine")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate logic without placing orders")
    parser.add_argument("--mode", default=None,
                        choices=["startup", "bic_scan", "eod_close"],
                        help="Cron mode: run one phase and exit")
    args = parser.parse_args()

    if args.mode == "startup":
        run_startup(dry_run=args.dry_run)
    elif args.mode == "bic_scan":
        run_bic_scan_mode(dry_run=args.dry_run)
    elif args.mode == "eod_close":
        run_eod_close(dry_run=args.dry_run)
    else:
        # Legacy persistent mode — only for local testing
        engine = Engine(dry_run=args.dry_run)
        signal.signal(signal.SIGINT, engine.handle_signal)
        signal.signal(signal.SIGTERM, engine.handle_signal)
        engine.start()


if __name__ == "__main__":
    main()
