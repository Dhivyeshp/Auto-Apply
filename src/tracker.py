"""SQLite tracker: one `jobs` row per listing, from discovery through application."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = ("queued", "drafted", "submitted", "skipped", "interview", "rejected", "offer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id    TEXT UNIQUE,
    source        TEXT,
    company       TEXT NOT NULL,
    role          TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    ats           TEXT,
    category      TEXT,
    terms         TEXT,          -- JSON list
    locations     TEXT,          -- JSON list
    sponsorship   TEXT,
    degrees       TEXT,          -- JSON list
    preferred     INTEGER NOT NULL DEFAULT 0,
    date_posted   TEXT,
    discovered_at TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    date_applied  TEXT,
    resume_path   TEXT,
    coverage_pct  REAL,
    notes         TEXT,
    raw_json      TEXT,          -- original listing, kept in case the schema changes
    tailor_dir    TEXT,          -- output folder of the latest tailoring run
    tailored_at   TEXT,
    rating        INTEGER        -- Claude's 1-10 fit rating of that resume
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# columns added after the first release; init_db adds any an older database lacks
_MIGRATIONS = {"tailor_dir": "TEXT", "tailored_at": "TEXT", "rating": "INTEGER"}

_JSON_COLS = ("terms", "locations", "degrees")
_UPDATABLE = {"date_applied", "resume_path", "coverage_pct", "notes"}


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col, typ in _MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    conn.commit()


def insert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> bool:
    """Insert a new job; returns False if its listing_id or url is already tracked."""
    row = dict(job)
    for col in _JSON_COLS:
        if col in row and not isinstance(row[col], str):
            row[col] = json.dumps(row[col])
    if "raw_json" in row and not isinstance(row["raw_json"], str):
        row["raw_json"] = json.dumps(row["raw_json"])
    row.setdefault("discovered_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    row.setdefault("status", "queued")
    cols = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    cur = conn.execute(f"INSERT OR IGNORE INTO jobs ({cols}) VALUES ({marks})", row)
    conn.commit()
    return cur.rowcount == 1


def list_jobs(conn: sqlite3.Connection, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM jobs"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY preferred DESC, date_posted DESC"
    return [_decode(r) for r in conn.execute(sql, args)]


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for col in _JSON_COLS:
        if d.get(col):
            d[col] = json.loads(d[col])
    return d


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def get_job_by_listing(conn: sqlite3.Connection, listing_id: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT * FROM jobs WHERE listing_id = ?", (listing_id,)).fetchone())


def set_tailored(conn: sqlite3.Connection, job_id: int, tailor_dir: str, rating: int | None) -> None:
    conn.execute(
        "UPDATE jobs SET tailor_dir = ?, tailored_at = ?, rating = ? WHERE id = ?",
        (tailor_dir, datetime.now(timezone.utc).isoformat(timespec="seconds"), rating, job_id),
    )
    conn.commit()


def count_tailored_since(conn: sqlite3.Connection, since_iso: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_at >= ?", (since_iso,)).fetchone()[0]


def update_status(conn: sqlite3.Connection, job_id: int, status: str, **fields: Any) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    sets = ["status = :status"] + [f"{k} = :{k}" for k in fields]
    conn.execute(
        f"UPDATE jobs SET {', '.join(sets)} WHERE id = :id",
        {"status": status, "id": job_id, **fields},
    )
    conn.commit()
