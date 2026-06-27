"""
Module: telegram_bot.py
All Telegram interactions:
  - Daily summary at 1:05 PM PT
  - Weekly summary every Friday
  - Manual commands: /status /pause /resume /enter /close_all
  - Async command handler runs in background thread
"""

import logging
import requests
import threading
import time
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional
import pytz

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from core.database import (
    get_daily_trades, get_weekly_trades, get_or_create_daily_state,
    get_open_spreads, insert_weekly_summary
)

logger = logging.getLogger(__name__)
PT = pytz.timezone("America/Los_Angeles")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Shared flag for pause/resume from Telegram commands
_paused = threading.Event()   # set = paused, clear = running


def is_paused() -> bool:
    return _paused.is_set()


def pause_trading(reason: str = "manual"):
    _paused.set()
    logger.warning("Trading PAUSED — reason: %s", reason)


def resume_trading():
    _paused.clear()
    logger.info("Trading RESUMED")


# ─────────────────────────────────────────────────────────────────────────────
# SEND
# ─────────────────────────────────────────────────────────────────────────────

def send(text: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — message suppressed: %s", text[:80])
        return False
    # Prepend Vajra header to every message
    text = "⚡ <b>Vajra Alert</b>" + chr(10) + text
    # Try with parse_mode first, fall back to plain text if formatting fails
    for mode in ([parse_mode, None] if parse_mode else [None]):
        try:
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
            if mode:
                payload["parse_mode"] = mode
            r = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json=payload,
                timeout=10,
            )
            if r.ok:
                return True
            elif r.status_code == 400 and mode:
                # Formatting error — retry as plain text
                logger.warning("Telegram parse_mode=%s failed (400) — retrying as plain text", mode)
                continue
            else:
                r.raise_for_status()
        except requests.RequestException as e:
            logger.error("Telegram send failed: %s", e)
            return False
    return False


# ─────────────────────────────────────────────────────────────────────────────
# DAILY SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def send_daily_summary(trade_date: str, account_equity: float) -> None:
    trades = get_daily_trades(trade_date)
    state  = get_or_create_daily_state(trade_date, account_equity)

    total     = len(trades)
    winners   = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    losers    = [t for t in trades if (t.get("realized_pnl") or 0) <= 0 and t["status"] != "OPEN"]
    open_t    = [t for t in trades if t["status"] == "OPEN"]
    gross_pnl = sum(t.get("realized_pnl") or 0 for t in trades)
    win_rate  = (len(winners) / total * 100) if total else 0

    breached  = [t for t in trades if t["status"] == "BREACHED"]

    cb_flag   = "⚡ YES" if state.get("circuit_breaker_hit") else "No"
    stopouts  = state.get("stopout_count", 0)

    # Trade detail rows
    rows = []
    for t in trades:
        pnl     = t.get("realized_pnl") or 0
        emoji   = "✅" if pnl > 0 else ("🛑" if t["status"] == "BREACHED" else "❌")
        reason  = t.get("close_reason") or t["status"]
        credit  = t.get("credit_received") or 0
        rows.append(
            f"{emoji} {t['setup_type']} | credit=${credit:.2f} | "
            f"P&L=${pnl:+.2f} | {reason}"
        )

    trades_block = "\n".join(rows) if rows else "_No trades today_"

    msg = (
        f"📊 <b>Daily Summary — {trade_date}</b>" + chr(10) +
        f"━━━━━━━━━━━━━━━━━━━━━━━━" + chr(10) +
        f"Trades:    {total} total | {len(winners)}W / {len(losers)}L" + chr(10) +
        f"Win Rate:  {win_rate:.0f}%" + chr(10) +
        f"Net P&L:   ${gross_pnl:+.2f}" + chr(10) +
        f"Account:   ${account_equity + gross_pnl:,.0f}" + chr(10) +
        f"━━━━━━━━━━━━━━━━━━━━━━━━" + chr(10) +
        f"Circuit Breaker: {cb_flag}" + chr(10) +
        f"Stop-outs:       {stopouts}" + chr(10) +
        f"Open positions:  {len(open_t)}" +
        (" ⚠️ carry risk!" if open_t else "") + chr(10) +
        f"━━━━━━━━━━━━━━━━━━━━━━━━" + chr(10) +
        f"<b>Trade Log:</b>" + chr(10) +
        trades_block
    )
    send(msg)
    logger.info("Daily summary sent for %s", trade_date)


# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def send_weekly_summary(equity_start: float, equity_end: float) -> None:
    today      = datetime.now(PT).date()
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    week_end   = today.strftime("%Y-%m-%d")

    trades     = get_weekly_trades(week_start, week_end)
    total      = len(trades)
    winners    = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    gross_pnl  = sum(t.get("realized_pnl") or 0 for t in trades)
    gross_cred = sum(t.get("credit_received") or 0 for t in trades)
    win_rate   = (len(winners) / total * 100) if total else 0
    growth_pct = ((equity_end - equity_start) / equity_start * 100) if equity_start else 0

    avg_credit = gross_cred / total if total else 0
    avg_kept   = gross_pnl  / total if total else 0

    # By setup type
    by_type: Dict[str, Dict] = {}
    for t in trades:
        st = t["setup_type"]
        if st not in by_type:
            by_type[st] = {"count": 0, "pnl": 0.0}
        by_type[st]["count"] += 1
        by_type[st]["pnl"]   += t.get("realized_pnl") or 0

    type_rows = "\n".join(
        f"  {st}: {v['count']} trades | ${v['pnl']:+.2f}"
        for st, v in by_type.items()
    ) or "  None"

    msg = (
        f"📈 <b>Weekly Summary — {week_start} to {week_end}</b>" + chr(10) +
        f"━━━━━━━━━━━━━━━━━━━━━━━━" + chr(10) +
        f"Trades:       {total} | {len(winners)}W / {total - len(winners)}L" + chr(10) +
        f"Win Rate:     {win_rate:.0f}%" + chr(10) +
        f"Net P&L:      ${gross_pnl:+.2f}" + chr(10) +
        f"Avg Credit:   ${avg_credit:.2f}" + chr(10) +
        f"Avg Kept:     ${avg_kept:.2f}" + chr(10) +
        f"Growth:       {growth_pct:+.2f}%" + chr(10) +
        f"Equity:       ${equity_start:,.0f} → ${equity_end:,.0f}" + chr(10) +
        f"━━━━━━━━━━━━━━━━━━━━━━━━" + chr(10) +
        f"<b>By Setup Type:</b>" + chr(10) +
        type_rows
    )
    send(msg)

    # Persist weekly summary
    insert_weekly_summary({
        "week_ending":           week_end,
        "total_trades":          total,
        "winning_trades":        len(winners),
        "win_rate":              round(win_rate, 2),
        "gross_credit":          round(gross_cred, 2),
        "gross_pnl":             round(gross_pnl, 2),
        "account_equity_start":  equity_start,
        "account_equity_end":    equity_end,
        "account_growth_pct":    round(growth_pct, 2),
        "created_at":            datetime.utcnow().isoformat(),
    })
    logger.info("Weekly summary sent for week ending %s", week_end)


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND HANDLER  (polls Telegram for /commands)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def send_monthly_summary(equity: float) -> None:
    from datetime import date, timedelta
    today     = datetime.now(PT).date()
    # First day of current month
    first_day = today.replace(day=1).strftime("%Y-%m-%d")
    last_day  = today.strftime("%Y-%m-%d")

    trades    = get_weekly_trades(first_day, last_day)
    total     = len(trades)
    winners   = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    gross_pnl = sum(t.get("realized_pnl") or 0 for t in trades)
    win_rate  = (len(winners) / total * 100) if total else 0
    avg_credit= sum(t.get("credit_received") or 0 for t in trades) / total if total else 0

    month_name = datetime.now(PT).strftime("%B %Y")

    msg = (
        f"📅 <b>Monthly Summary — {month_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades:      {total} | {len(winners)}W / {total-len(winners)}L\n"
        f"Win Rate:    {win_rate:.0f}%\n"
        f"Net P&L:     ${gross_pnl:+,.2f}\n"
        f"Avg Credit:  ${avg_credit:.2f}\n"
        f"Account:     ${equity:,.0f}"
    )
    send(msg)
    logger.info("Monthly summary sent for %s", month_name)


# ─────────────────────────────────────────────────────────────────────────────
# YEARLY SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def send_yearly_summary(equity_start: float, equity_end: float) -> None:
    year      = datetime.now(PT).year
    first_day = f"{year}-01-01"
    last_day  = datetime.now(PT).strftime("%Y-%m-%d")

    trades    = get_weekly_trades(first_day, last_day)
    total     = len(trades)
    winners   = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    gross_pnl = sum(t.get("realized_pnl") or 0 for t in trades)
    win_rate  = (len(winners) / total * 100) if total else 0
    growth    = ((equity_end - equity_start) / equity_start * 100) if equity_start else 0

    # Monthly breakdown
    from collections import defaultdict
    monthly: dict = defaultdict(float)
    for t in trades:
        m = t.get("trade_date", "")[:7]  # YYYY-MM
        monthly[m] += t.get("realized_pnl") or 0

    monthly_rows = "\n".join(
        f"  {m}: ${v:+,.0f}"
        for m, v in sorted(monthly.items())
    ) or "  No data"

    msg = (
        f"📆 <b>Yearly Summary — {year}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades:      {total} | {len(winners)}W / {total-len(winners)}L\n"
        f"Win Rate:    {win_rate:.0f}%\n"
        f"Net P&L:     ${gross_pnl:+,.2f}\n"
        f"Growth:      {growth:+.1f}%\n"
        f"Equity:      ${equity_start:,.0f} → ${equity_end:,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Monthly Breakdown:</b>\n{monthly_rows}"
    )
    send(msg)
    logger.info("Yearly summary sent for %s", year)


class CommandHandler:
    """
    Polls Telegram getUpdates every 5s and dispatches commands.
    Runs in a daemon thread — doesn't block main loop.

    Supported commands:
      /status      — show open positions + today's P&L
      /pause       — halt new entries (does NOT close existing positions)
      /resume      — re-enable entries
      /close_all   — emergency: close all open positions immediately
      /help        — list commands
    """

    def __init__(self, engine_ref):
        """engine_ref: the main Engine instance for calling close_all."""
        self.engine   = engine_ref
        self._offset  = 0
        self._running = False

    def start(self):
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info("Telegram command handler started")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        while self._running:
            try:
                self._check_updates()
            except Exception as e:
                logger.error("Command handler error: %s", e)
            time.sleep(5)

    def _check_updates(self):
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": self._offset, "timeout": 4},
            timeout=10,
        )
        if not r.ok:
            return
        updates = r.json().get("result", [])
        for update in updates:
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip()
            if text:
                self._dispatch(text)

    def _dispatch(self, text: str):
        cmd = text.lower().split()[0]
        logger.info("Telegram command received: %s", text)

        if cmd == "/enter":
            self._cmd_enter(text)
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/pause":
            pause_trading("telegram command")
            send("⏸ Trading <b>PAUSED</b> — no new entries until /resume")
        elif cmd == "/resume":
            resume_trading()
            send("▶️ Trading <b>RESUMED</b>")
        elif cmd == "/close_all":
            send("🚨 Closing all open positions now...")
            if hasattr(self.engine, "emergency_close_all"):
                self.engine.emergency_close_all()
        elif cmd == "/help":
            send(
            send(
                "📋 <b>Commands:</b>" + chr(10) +
                "/enter IC — enter Iron Condor" + chr(10) +
                "/enter IC A+ — enter IC grade A+" + chr(10) +
                "/status — open positions + P&L" + chr(10) +
                "/pause — halt new entries" + chr(10) +
                "/resume — re-enable entries" + chr(10) +
                "/close_all — emergency close all" + chr(10) +
                "/help — this menu"
            )
            )
        else:
            send(f"Unknown command: {text}" + chr(10) + "Try /help")

    def _cmd_enter(self, text: str):
        """
        Handle /enter IC [grade] command.
        Examples:
          /enter IC        → IC, grade A (default)
          /enter IC A+     → IC, grade A+
        """
        import pytz
        from core.database import get_open_spreads, get_or_create_daily_state
        from core.tradier import get_vix
        from modules.trade_entry import enter_trade

        PT = pytz.timezone("America/Los_Angeles")
        trade_date = __import__("datetime").datetime.now(PT).strftime("%Y-%m-%d")

        # Parse command — /enter IC or /enter IC A+
        parts = text.strip().split()
        if len(parts) < 2:
            send("Usage: `/enter IC` or `/enter IC A+`")
            return

        setup_type  = parts[1].upper()
        signal_grade = parts[2].upper() if len(parts) >= 3 else "A"

        if setup_type not in ("IC", "BEAR_CALL", "BULL_PUT"):
            send(f"❌ Unknown setup: `{setup_type}` — use IC, BEAR_CALL, or BULL_PUT")
            return

        if signal_grade not in ("A+", "A"):
            send(f"❌ Invalid grade: `{signal_grade}` — use A or A+")
            return

        if is_paused():
            send("⏸ Trading is paused — send /resume first")
            return

        # Load daily state
        from core.tradier import get_account_balance
        balance = get_account_balance()
        equity  = float(balance["total_equity"]) if balance and balance.get("total_equity") else 50000.0

        daily_state       = get_or_create_daily_state(trade_date, equity)
        open_positions    = len(get_open_spreads(trade_date))
        vix               = get_vix() or 0.0

        send(
            "🔍 Processing /enter " + setup_type + " " + signal_grade + chr(10) +
            "VIX: " + str(round(vix,1)) + " | Open positions: " + str(open_positions) +
            " | Equity: $" + f"{equity:,.0f}"
        )

        # Attempt entry
        success, msg = enter_trade(
            setup_type          = setup_type,
            signal_grade        = signal_grade,
            is_news_day         = False,
            daily_state         = daily_state,
            account_equity      = equity,
            total_open_positions = open_positions,
        )

        if success:
            send("✅ <b>Trade entered!</b>" + chr(10) + msg)
        else:
            send("🚫 <b>Entry blocked:</b>" + chr(10) + msg)

    def _cmd_status(self):
        from datetime import datetime
        import pytz
        PT = pytz.timezone("America/Los_Angeles")
        today = datetime.now(PT).strftime("%Y-%m-%d")
        open_spreads = get_open_spreads(today)

        if not open_spreads:
            send("📭 No open positions right now.")
            return

        lines = [f"📋 <b>Open Positions ({len(open_spreads)})</b>"]
        for s in open_spreads:
            lines.append(
                f"  • {s['setup_type']} | credit=${s['credit_received']:.2f} "
                f"| {s['contracts']}c | entered {s['entry_time'][11:16]} UTC"
            )
        paused_str = " | ⏸ PAUSED" if is_paused() else ""
        lines.append(f"\n_Status: running{paused_str}_")
        send("\n".join(lines))
