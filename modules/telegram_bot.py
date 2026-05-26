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

def send(text: str, parse_mode: str = "Markdown") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — message suppressed: %s", text[:80])
        return False
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Telegram send failed: %s", e)
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

    msg = f"""📊 *Daily Summary — {trade_date}*
━━━━━━━━━━━━━━━━━━━━━━━━
*Trades:*       {total} total | {len(winners)}W / {len(losers)}L
*Win Rate:*     {win_rate:.0f}%
*Net P&L:*      ${gross_pnl:+.2f}
*Account:*      ${account_equity + gross_pnl:,.0f}
━━━━━━━━━━━━━━━━━━━━━━━━
*Circuit Breaker:* {cb_flag}
*Stop-outs:*       {stopouts}
*Open positions:*  {len(open_t)} (carry risk — check!)
━━━━━━━━━━━━━━━━━━━━━━━━
*Trade Log:*
{trades_block}"""

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

    msg = f"""📈 *Weekly Summary — {week_start} to {week_end}*
━━━━━━━━━━━━━━━━━━━━━━━━
*Trades:*         {total} | {len(winners)}W / {total - len(winners)}L
*Win Rate:*       {win_rate:.0f}%
*Net P&L:*        ${gross_pnl:+.2f}
*Avg Credit:*     ${avg_credit:.2f}
*Avg Kept:*       ${avg_kept:.2f}
*Account Growth:* {growth_pct:+.2f}%
*Equity:*         ${equity_start:,.0f} → ${equity_end:,.0f}
━━━━━━━━━━━━━━━━━━━━━━━━
*By Setup Type:*
{type_rows}"""

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

        if cmd == "/status":
            self._cmd_status()
        elif cmd == "/pause":
            pause_trading("telegram command")
            send("⏸ Trading *PAUSED* — no new entries until /resume")
        elif cmd == "/resume":
            resume_trading()
            send("▶️ Trading *RESUMED*")
        elif cmd == "/close_all":
            send("🚨 Closing all open positions now...")
            if hasattr(self.engine, "emergency_close_all"):
                self.engine.emergency_close_all()
        elif cmd == "/help":
            send(
                "📋 *Commands:*\n"
                "/status — open positions + P&L\n"
                "/pause — halt new entries\n"
                "/resume — re-enable entries\n"
                "/close\\_all — emergency close all\n"
                "/help — this menu"
            )
        else:
            send(f"Unknown command: `{text}`\nTry /help")

    def _cmd_status(self):
        from datetime import datetime
        import pytz
        PT = pytz.timezone("America/Los_Angeles")
        today = datetime.now(PT).strftime("%Y-%m-%d")
        open_spreads = get_open_spreads(today)

        if not open_spreads:
            send("📭 No open positions right now.")
            return

        lines = [f"📋 *Open Positions ({len(open_spreads)})*"]
        for s in open_spreads:
            lines.append(
                f"  • {s['setup_type']} | credit=${s['credit_received']:.2f} "
                f"| {s['contracts']}c | entered {s['entry_time'][11:16]} UTC"
            )
        paused_str = " | ⏸ PAUSED" if is_paused() else ""
        lines.append(f"\n_Status: running{paused_str}_")
        send("\n".join(lines))
