"""SQLite storage for fills, candle cache, fund-flow ledger, and LP snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DATA_DIR = Path(os.environ.get("MMKIT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "mmkit.db"
CONFIG_PATH = DATA_DIR / "config.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    venue      TEXT    NOT NULL DEFAULT 'hyperliquid',
    tid        INTEGER NOT NULL,        -- venue trade id (unique per venue)
    oid        INTEGER,
    coin       TEXT    NOT NULL,
    side       TEXT    NOT NULL,        -- 'B' buy / 'A' sell
    px         REAL    NOT NULL,
    sz         REAL    NOT NULL,
    time_ms    INTEGER NOT NULL,
    crossed    INTEGER NOT NULL,        -- 1 = taker (crossed the book), 0 = maker
    fee        REAL    NOT NULL,        -- positive = paid, negative = rebate
    fee_token  TEXT,
    closed_pnl REAL,                    -- venue-reported realized pnl on this fill
    dir        TEXT,
    hash       TEXT,
    PRIMARY KEY (venue, tid)
);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(time_ms);
CREATE INDEX IF NOT EXISTS idx_fills_coin ON fills(coin, time_ms);

CREATE TABLE IF NOT EXISTS candles (
    venue TEXT    NOT NULL DEFAULT 'hyperliquid',
    coin  TEXT    NOT NULL,
    t_ms  INTEGER NOT NULL,             -- candle open time
    close REAL    NOT NULL,
    PRIMARY KEY (venue, coin, t_ms)
);

CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,           -- ISO-8601 UTC
    chain      TEXT NOT NULL,
    tx_hash    TEXT,
    amount_usd REAL NOT NULL,           -- negative = cost/outflow
    category   TEXT NOT NULL,           -- deposit|withdrawal|bridge_fee|gas|transfer|lp_add|lp_remove|other
    note       TEXT
);

CREATE TABLE IF NOT EXISTS lp_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    venue           TEXT NOT NULL,      -- e.g. aerodrome, uniswap-v4
    pool            TEXT NOT NULL,      -- e.g. WETH/USDC 0.05%
    value_usd       REAL NOT NULL,      -- current position value if withdrawn now
    fees_earned_usd REAL,               -- cumulative fees earned
    range_lo        REAL,
    range_hi        REAL,
    in_range        INTEGER,
    note            TEXT
);
"""

LEDGER_CATEGORIES = (
    "deposit", "withdrawal", "bridge_fee", "gas", "transfer", "lp_add", "lp_remove", "other",
)


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- config
def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# ---------------------------------------------------------------- fills
def insert_fills(conn: sqlite3.Connection, fills: List[Dict[str, Any]],
                 venue: str = "hyperliquid") -> int:
    """Insert fills, ignoring duplicates. Returns number of new rows."""
    before = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    for f in fills:
        conn.execute(
            """INSERT INTO fills (venue, tid, oid, coin, side, px, sz, time_ms, crossed,
                                  fee, fee_token, closed_pnl, dir, hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(venue, tid) DO NOTHING""",
            (
                venue,
                f.get("tid"),
                f.get("oid"),
                f.get("coin"),
                f.get("side"),
                float(f.get("px", 0)),
                float(f.get("sz", 0)),
                int(f.get("time", 0)),
                1 if f.get("crossed") else 0,
                float(f.get("fee", 0) or 0),
                f.get("feeToken"),
                float(f.get("closedPnl", 0) or 0),
                f.get("dir"),
                f.get("hash"),
            ),
        )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    return after - before


def get_fills(
    conn: sqlite3.Connection,
    coin: Optional[str] = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    venue: Optional[str] = None,
) -> List[sqlite3.Row]:
    q = "SELECT * FROM fills WHERE 1=1"
    args: List[Any] = []
    if venue:
        q += " AND venue = ?"
        args.append(venue)
    if coin:
        q += " AND coin = ?"
        args.append(coin)
    if since_ms is not None:
        q += " AND time_ms >= ?"
        args.append(since_ms)
    if until_ms is not None:
        q += " AND time_ms <= ?"
        args.append(until_ms)
    q += " ORDER BY time_ms, tid"
    return conn.execute(q, args).fetchall()


def latest_fill_time(conn: sqlite3.Connection, venue: str = "hyperliquid") -> Optional[int]:
    row = conn.execute("SELECT MAX(time_ms) FROM fills WHERE venue = ?", (venue,)).fetchone()
    return row[0]


def venues_present(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT venue FROM fills ORDER BY venue")]


# ---------------------------------------------------------------- candles
def upsert_candles(conn: sqlite3.Connection, coin: str, candles: Iterable[Dict[str, Any]],
                   venue: str = "hyperliquid") -> None:
    conn.executemany(
        "INSERT INTO candles (venue, coin, t_ms, close) VALUES (?,?,?,?) "
        "ON CONFLICT(venue, coin, t_ms) DO UPDATE SET close = excluded.close",
        [(venue, coin, int(c["t"]), float(c["c"])) for c in candles],
    )
    conn.commit()


def get_close_at(conn: sqlite3.Connection, coin: str, t_ms: int,
                 venue: str = "hyperliquid") -> Optional[float]:
    """Close of the 1m candle containing t_ms."""
    bucket = (t_ms // 60_000) * 60_000
    row = conn.execute(
        "SELECT close FROM candles WHERE venue = ? AND coin = ? AND t_ms = ?",
        (venue, coin, bucket),
    ).fetchone()
    return row[0] if row else None


def candle_coverage(conn: sqlite3.Connection, coin: str,
                    venue: str = "hyperliquid") -> Optional[tuple]:
    row = conn.execute(
        "SELECT MIN(t_ms), MAX(t_ms) FROM candles WHERE venue = ? AND coin = ?",
        (venue, coin),
    ).fetchone()
    return (row[0], row[1]) if row and row[0] is not None else None
