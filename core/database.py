"""
Module: database.py
Handles all SQLite persistence — trades, legs, daily snapshots, system state.
Single source of truth for everything the engine needs to survive a restart.
"""

import sqlite3
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from config import DB_PATH

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    with get_conn() as conn:
        conn.executescript("""
        -- ── SPREADS ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS spreads (
            id              TEXT PRIMARY KEY,          -- uuid4
            trade_date      TEXT NOT NULL,             -- YYYY-MM-DD
            setup_type      TEXT NOT NULL,             -- IC | BEAR_CALL | BULL_PUT
            signal_grade    TEXT NOT NULL,             -- A+ | A
            entry_time      TEXT NOT NULL,             -- ISO datetime
            credit_received REAL NOT NULL,             -- total credit per spread ($)
            spread_width    INTEGER NOT NULL,          -- points
            contracts       INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | BREACHED
            close_time      TEXT,
            close_debit     REAL,                      -- cost to close per spread
            realized_pnl    REAL,                      -- positive = profit
            close_reason    TEXT,                      -- TIER1|TIER2|FREE_RUNNER|BREACH_DELTA|BREACH_PNL|HARD_CLOSE|CIRCUIT_BREAKER
            tier_assignment TEXT,                      -- JSON: {tier: contracts}
            notes           TEXT
        );

        -- ── LEGS ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS legs (
            id              TEXT PRIMARY KEY,
            spread_id       TEXT NOT NULL REFERENCES spreads(id),
            leg_type        TEXT NOT NULL,   -- SHORT_CALL | LONG_CALL | SHORT_PUT | LONG_PUT
            strike          REAL NOT NULL,
            expiry          TEXT NOT NULL,   -- YYYY-MM-DD
            option_symbol   TEXT NOT NULL,
            entry_price     REAL NOT NULL,
            entry_delta     REAL,
            entry_iv        REAL,
            entry_theta     REAL,
            last_price      REAL,
            last_delta      REAL,
            last_iv         REAL,
            tradier_order_id TEXT
        );

        -- ── POSITION SNAPSHOTS (every poll cycle for open positions) ──────────
        CREATE TABLE IF NOT EXISTS position_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            spread_id       TEXT NOT NULL REFERENCES spreads(id),
            snapshot_time   TEXT NOT NULL,
            net_debit       REAL,           -- current cost to close
            pnl_pct         REAL,           -- % of credit captured so far
            short_leg_delta REAL,
            vix             REAL,
            raw_json        TEXT            -- full Tradier response
        );

        -- ── DAILY STATE ───────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS daily_state (
            trade_date          TEXT PRIMARY KEY,   -- YYYY-MM-DD
            account_equity      REAL NOT NULL,
            daily_pnl           REAL DEFAULT 0,
            circuit_breaker_hit INTEGER DEFAULT 0,  -- 0 | 1
            stopout_count       INTEGER DEFAULT 0,
            cooloff_until       TEXT,               -- ISO datetime or NULL
            entries_halted      INTEGER DEFAULT 0,  -- 0 | 1
            consecutive_win_days INTEGER DEFAULT 0,
            notes               TEXT
        );

        -- ── WEEKLY SUMMARY ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS weekly_summaries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            week_ending     TEXT NOT NULL,          -- YYYY-MM-DD (Friday)
            total_trades    INTEGER,
            winning_trades  INTEGER,
            win_rate        REAL,
            gross_credit    REAL,
            gross_pnl       REAL,
            account_equity_start REAL,
            account_equity_end   REAL,
            account_growth_pct   REAL,
            created_at      TEXT
        );
        """)
    logger.info("Database initialized: %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# DAILY STATE
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_daily_state(trade_date: str, account_equity: float) -> Dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_state WHERE trade_date = ?", (trade_date,)
        ).fetchone()
        if row:
            return dict(row)
        conn.execute(
            """INSERT INTO daily_state (trade_date, account_equity)
               VALUES (?, ?)""",
            (trade_date, account_equity)
        )
        return {
            "trade_date": trade_date,
            "account_equity": account_equity,
            "daily_pnl": 0.0,
            "circuit_breaker_hit": 0,
            "stopout_count": 0,
            "cooloff_until": None,
            "entries_halted": 0,
            "consecutive_win_days": 0,
        }


def update_daily_state(trade_date: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [trade_date]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE daily_state SET {cols} WHERE trade_date = ?", vals
        )


# ─────────────────────────────────────────────────────────────────────────────
# SPREADS
# ─────────────────────────────────────────────────────────────────────────────

def insert_spread(spread: Dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO spreads
            (id, trade_date, setup_type, signal_grade, entry_time,
             credit_received, spread_width, contracts, status, tier_assignment)
            VALUES (:id, :trade_date, :setup_type, :signal_grade, :entry_time,
                    :credit_received, :spread_width, :contracts, 'OPEN',
                    :tier_assignment)
        """, spread)


def insert_leg(leg: Dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO legs
            (id, spread_id, leg_type, strike, expiry, option_symbol,
             entry_price, entry_delta, entry_iv, entry_theta, tradier_order_id)
            VALUES (:id, :spread_id, :leg_type, :strike, :expiry, :option_symbol,
                    :entry_price, :entry_delta, :entry_iv, :entry_theta,
                    :tradier_order_id)
        """, leg)


def get_open_spreads(trade_date: Optional[str] = None) -> List[Dict]:
    query = "SELECT * FROM spreads WHERE status = 'OPEN'"
    params = []
    if trade_date:
        query += " AND trade_date = ?"
        params.append(trade_date)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_legs_for_spread(spread_id: str) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM legs WHERE spread_id = ?", (spread_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def close_spread(spread_id: str, close_debit: float, close_reason: str,
                 credit_received: float) -> float:
    """Marks spread closed, calculates realized P&L per contract, returns total P&L."""
    realized_pnl = (credit_received - close_debit)   # per spread
    with get_conn() as conn:
        conn.execute("""
            UPDATE spreads
            SET status = CASE WHEN ? LIKE 'BREACH%' THEN 'BREACHED' ELSE 'CLOSED' END,
                close_time  = ?,
                close_debit = ?,
                realized_pnl = ?,
                close_reason = ?
            WHERE id = ?
        """, (close_reason, datetime.utcnow().isoformat(),
              close_debit, realized_pnl, close_reason, spread_id))
    return realized_pnl


def update_leg_last_quote(spread_id: str, leg_type: str,
                          last_price: float, last_delta: float, last_iv: float) -> None:
    with get_conn() as conn:
        conn.execute("""
            UPDATE legs SET last_price = ?, last_delta = ?, last_iv = ?
            WHERE spread_id = ? AND leg_type = ?
        """, (last_price, last_delta, last_iv, spread_id, leg_type))


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOTS
# ─────────────────────────────────────────────────────────────────────────────

def insert_snapshot(spread_id: str, net_debit: float, pnl_pct: float,
                    short_leg_delta: float, vix: float, raw: dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO position_snapshots
            (spread_id, snapshot_time, net_debit, pnl_pct, short_leg_delta, vix, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (spread_id, datetime.utcnow().isoformat(),
              net_debit, pnl_pct, short_leg_delta, vix, json.dumps(raw)))


# ─────────────────────────────────────────────────────────────────────────────
# REPORTING QUERIES
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_trades(trade_date: str) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM spreads WHERE trade_date = ? ORDER BY entry_time",
            (trade_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_weekly_trades(week_start: str, week_end: str) -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM spreads WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, entry_time",
            (week_start, week_end)
        ).fetchall()
        return [dict(r) for r in rows]


def insert_weekly_summary(summary: Dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO weekly_summaries
            (week_ending, total_trades, winning_trades, win_rate,
             gross_credit, gross_pnl, account_equity_start,
             account_equity_end, account_growth_pct, created_at)
            VALUES (:week_ending, :total_trades, :winning_trades, :win_rate,
                    :gross_credit, :gross_pnl, :account_equity_start,
                    :account_equity_end, :account_growth_pct, :created_at)
        """, summary)
