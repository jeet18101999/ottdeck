"""SQLite storage. One file, no server, full-text search built in."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS titles (
    id               INTEGER PRIMARY KEY,
    tmdb_id          INTEGER NOT NULL,
    media_type       TEXT    NOT NULL CHECK (media_type IN ('movie', 'tv')),
    title            TEXT    NOT NULL,
    original_title   TEXT,
    overview         TEXT,
    people           TEXT,            -- denormalised cast + director, for search
    release_date     TEXT,
    year             INTEGER,
    runtime          INTEGER,
    vote_average     REAL,
    vote_count       INTEGER,
    popularity       REAL,
    poster_path      TEXT,
    backdrop_path    TEXT,
    original_language TEXT,
    genres           TEXT,            -- JSON array of names
    imdb_id          TEXT,
    homepage         TEXT,
    updated_at       TEXT    NOT NULL,
    UNIQUE (tmdb_id, media_type)
);

CREATE INDEX IF NOT EXISTS idx_titles_year       ON titles (year);
CREATE INDEX IF NOT EXISTS idx_titles_popularity ON titles (popularity DESC);
CREATE INDEX IF NOT EXISTS idx_titles_lang       ON titles (original_language);

CREATE TABLE IF NOT EXISTS providers (
    provider_id      INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    logo_path        TEXT,
    display_priority INTEGER
);

-- Which service carries which title, in which region, on what terms.
CREATE TABLE IF NOT EXISTS availability (
    title_id     INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    region       TEXT    NOT NULL,
    provider_id  INTEGER NOT NULL REFERENCES providers (provider_id),
    offer_type   TEXT    NOT NULL,   -- flatrate | free | ads | rent | buy
    link         TEXT,
    PRIMARY KEY (title_id, region, provider_id, offer_type)
);

CREATE INDEX IF NOT EXISTS idx_avail_provider ON availability (region, provider_id);

-- Services the user actually pays for, so results can be filtered to them.
CREATE TABLE IF NOT EXISTS my_providers (
    provider_id INTEGER PRIMARY KEY REFERENCES providers (provider_id)
);

CREATE TABLE IF NOT EXISTS watchlist (
    title_id  INTEGER PRIMARY KEY REFERENCES titles (id) ON DELETE CASCADE,
    added_at  TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'planned'  -- planned | watching | done
);

-- Personal files (a folder scan or Jellyfin), matched to a TMDB title.
CREATE TABLE IF NOT EXISTS local_media (
    path       TEXT PRIMARY KEY,
    title_id   INTEGER REFERENCES titles (id) ON DELETE SET NULL,
    size_bytes INTEGER,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- External-content FTS: the index stores no copy of the rows, it points at `titles`.
CREATE VIRTUAL TABLE IF NOT EXISTS titles_fts USING fts5 (
    title,
    original_title,
    people,
    overview,
    content       = 'titles',
    content_rowid = 'id',
    tokenize      = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS titles_ai AFTER INSERT ON titles BEGIN
    INSERT INTO titles_fts (rowid, title, original_title, people, overview)
    VALUES (new.id, new.title, new.original_title, new.people, new.overview);
END;

CREATE TRIGGER IF NOT EXISTS titles_ad AFTER DELETE ON titles BEGIN
    INSERT INTO titles_fts (titles_fts, rowid, title, original_title, people, overview)
    VALUES ('delete', old.id, old.title, old.original_title, old.people, old.overview);
END;

CREATE TRIGGER IF NOT EXISTS titles_au AFTER UPDATE ON titles BEGIN
    INSERT INTO titles_fts (titles_fts, rowid, title, original_title, people, overview)
    VALUES ('delete', old.id, old.title, old.original_title, old.people, old.overview);
    INSERT INTO titles_fts (rowid, title, original_title, people, overview)
    VALUES (new.id, new.title, new.original_title, new.people, new.overview);
END;
"""


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will not
# add them to a database that already exists, so they are applied by hand.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "local_media": [
        ("kind", "TEXT"),
        ("season", "INTEGER"),
        ("episode", "INTEGER"),
        ("guess_title", "TEXT"),
        ("guess_year", "INTEGER"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue  # table not created yet; SCHEMA handles it
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db(path=None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(path=None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


TITLE_COLS = (
    "tmdb_id", "media_type", "title", "original_title", "overview", "people",
    "release_date", "year", "runtime", "vote_average", "vote_count",
    "popularity", "poster_path", "backdrop_path", "original_language",
    "genres", "imdb_id", "homepage",
)


def upsert_title(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    """Insert or refresh a title; returns its internal row id."""
    values = [rec.get(c) for c in TITLE_COLS]
    gi = TITLE_COLS.index("genres")
    if isinstance(values[gi], list):
        values[gi] = json.dumps(values[gi])

    placeholders = ", ".join("?" for _ in TITLE_COLS)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in TITLE_COLS if c not in ("tmdb_id", "media_type")
    )

    conn.execute(
        f"""
        INSERT INTO titles ({', '.join(TITLE_COLS)}, updated_at)
        VALUES ({placeholders}, ?)
        ON CONFLICT (tmdb_id, media_type)
        DO UPDATE SET {updates}, updated_at = excluded.updated_at
        """,
        (*values, _now()),
    )
    row = conn.execute(
        "SELECT id FROM titles WHERE tmdb_id = ? AND media_type = ?",
        (rec["tmdb_id"], rec["media_type"]),
    ).fetchone()
    return int(row["id"])


def upsert_provider(conn: sqlite3.Connection, p: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO providers (provider_id, name, logo_path, display_priority)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (provider_id) DO UPDATE SET
            name             = excluded.name,
            logo_path        = excluded.logo_path,
            display_priority = excluded.display_priority
        """,
        (p["provider_id"], p["name"], p.get("logo_path"), p.get("display_priority")),
    )


def replace_availability(
    conn: sqlite3.Connection, title_id: int, region: str, offers: list[dict[str, Any]]
) -> None:
    """Availability churns constantly, so each refresh replaces the region wholesale."""
    conn.execute(
        "DELETE FROM availability WHERE title_id = ? AND region = ?", (title_id, region)
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO availability (title_id, region, provider_id, offer_type, link)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(title_id, region, o["provider_id"], o["offer_type"], o.get("link")) for o in offers],
    )


def replace_all_availability(
    conn: sqlite3.Connection, title_id: int, offers_by_region: dict[str, list[dict[str, Any]]]
) -> None:
    """Replace every region's availability for one title in a single pass."""
    conn.execute("DELETE FROM availability WHERE title_id = ?", (title_id,))
    rows = [
        (title_id, region, o["provider_id"], o["offer_type"], o.get("link"))
        for region, offers in offers_by_region.items()
        for o in offers
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO availability (title_id, region, provider_id, offer_type, link)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )


def known_regions(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Regions that actually have offers stored, most-covered first."""
    return [
        (r["region"], r["n"])
        for r in conn.execute(
            "SELECT region, count(DISTINCT title_id) AS n FROM availability "
            "GROUP BY region ORDER BY n DESC, region"
        ).fetchall()
    ]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    def q(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "titles": q("SELECT count(*) FROM titles"),
        "movies": q("SELECT count(*) FROM titles WHERE media_type = 'movie'"),
        "shows": q("SELECT count(*) FROM titles WHERE media_type = 'tv'"),
        "providers": q("SELECT count(*) FROM providers"),
        "availability": q("SELECT count(*) FROM availability"),
        "watchlist": q("SELECT count(*) FROM watchlist"),
    }
