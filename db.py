"""SQLite schema and connection helpers for the job match engine."""

import argparse
import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "jobs.db"

# ATS boards carry the full JD; USAJOBS carries full text; Adzuna truncates.
# Higher wins when two sources produce the same dedupe_key.
SOURCE_PRIORITY = {"greenhouse": 3, "lever": 3, "usajobs": 2, "adzuna": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  dedupe_key    TEXT NOT NULL,
  source        TEXT NOT NULL,
  company       TEXT,
  title         TEXT,
  location      TEXT,
  remote        INTEGER,
  url           TEXT,
  description   TEXT,
  desc_is_full  INTEGER DEFAULT 0,
  salary_min    INTEGER,
  salary_max    INTEGER,
  posted_at     TEXT,
  first_seen    TEXT NOT NULL,
  raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);

CREATE TABLE IF NOT EXISTS scores (
  job_id           TEXT PRIMARY KEY REFERENCES jobs(id),
  score            INTEGER NOT NULL,
  title_score      INTEGER,
  keyword_score    INTEGER,
  location_score   INTEGER,
  freshness_score  INTEGER,
  penalty          INTEGER,
  matched_keywords TEXT,
  gap_flags        TEXT,
  scored_at        TEXT
);
-- The Daily Ten query orders by score on every queue rebuild.
CREATE INDEX IF NOT EXISTS idx_scores_score ON scores(score DESC);

CREATE TABLE IF NOT EXISTS applications (
  job_id     TEXT PRIMARY KEY REFERENCES jobs(id),
  status     TEXT NOT NULL,
  applied_at TEXT,
  notes      TEXT
);
"""


def connect(path=DB_PATH):
    """Open the database, creating its directory if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=DB_PATH):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_title(title):
    """Strip seniority noise and punctuation so the same role dedupes across boards."""
    t = (title or "").lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)  # drop "(Remote)", "[US]"
    t = re.sub(r"\b(sr|snr|senior|jr|junior|staff|lead|principal|i{1,3}|iv|v)\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def normalize_city(location):
    """First comma-segment of a location string, lowercased."""
    loc = (location or "").lower().strip()
    city = loc.split(",")[0]
    city = re.sub(r"[^a-z0-9]+", " ", city)
    return " ".join(city.split())


def dedupe_key(company, title, location):
    raw = f"{(company or '').lower().strip()}|{normalize_title(title)}|{normalize_city(location)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_yaml(name):
    """Load a YAML file from the project root; returns {} if absent."""
    path = ROOT / name if not Path(name).is_absolute() else Path(name)
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def save_yaml(name, data):
    path = ROOT / name if not Path(name).is_absolute() else Path(name)
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


def main():
    ap = argparse.ArgumentParser(description="Database maintenance.")
    ap.add_argument("--init", action="store_true", help="create tables")
    ap.add_argument("--stats", action="store_true", help="print row counts")
    args = ap.parse_args()

    if args.init or not args.stats:
        conn = init_db()
        tables = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        print(f"initialized {DB_PATH}")
        print("tables:", ", ".join(tables))

    if args.stats:
        conn = connect()
        for table in ("jobs", "scores", "applications"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:14} {n}")


if __name__ == "__main__":
    main()
