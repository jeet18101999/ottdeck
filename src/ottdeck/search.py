"""Search over the local catalogue.

FTS5 handles prefix and phrase matching; rapidfuzz covers the misspellings FTS
cannot ("intersteller"). Filters are plain SQL on indexed columns.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from rapidfuzz import fuzz, process

from .config import REGION

# Fuzzy matches below this similarity are noise rather than typos.
FUZZY_CUTOFF = 70

# FTS5 treats these as operators; a user typing them means them literally.
_FTS_SPECIALS = re.compile(r'["*():^\-]')


@dataclass
class Filters:
    media_type: str | None = None          # 'movie' | 'tv'
    genres: Sequence[str] = field(default_factory=tuple)
    year_min: int | None = None
    year_max: int | None = None
    runtime_max: int | None = None
    language: str | None = None            # ISO 639-1, e.g. 'ta', 'hi'
    provider_ids: Sequence[int] = field(default_factory=tuple)
    only_my_providers: bool = False
    only_local: bool = False               # only titles with a file on disk
    min_rating: float | None = None
    region: str = REGION


def _fts_query(text: str) -> str:
    """Turn free text into a safe prefix query: `dark knig` -> `dark* knig*`."""
    cleaned = _FTS_SPECIALS.sub(" ", text)
    tokens = [t for t in cleaned.split() if t]
    return " ".join(f'"{t}"*' for t in tokens)


def _filter_sql(f: Filters, params: list[Any]) -> str:
    clauses: list[str] = []

    if f.media_type:
        clauses.append("t.media_type = ?")
        params.append(f.media_type)
    if f.year_min is not None:
        clauses.append("t.year >= ?")
        params.append(f.year_min)
    if f.year_max is not None:
        clauses.append("t.year <= ?")
        params.append(f.year_max)
    if f.runtime_max is not None:
        clauses.append("t.runtime IS NOT NULL AND t.runtime <= ?")
        params.append(f.runtime_max)
    if f.language:
        clauses.append("t.original_language = ?")
        params.append(f.language)
    if f.min_rating is not None:
        clauses.append("t.vote_average >= ?")
        params.append(f.min_rating)

    for g in f.genres:
        # genres is a JSON array of names; match the quoted element.
        clauses.append("t.genres LIKE ?")
        params.append(f'%"{g}"%')

    if f.only_local:
        clauses.append(
            "EXISTS (SELECT 1 FROM local_media lm WHERE lm.title_id = t.id)"
        )

    if f.only_my_providers:
        # A subscription covers flatrate/free/ads only - paying for Netflix does
        # not make its rentals free, so a rent-only offer is not "on my plan".
        clauses.append(
            "EXISTS (SELECT 1 FROM availability a "
            "JOIN my_providers m ON m.provider_id = a.provider_id "
            "WHERE a.title_id = t.id AND a.region = ? "
            "AND a.offer_type IN ('flatrate', 'free', 'ads'))"
        )
        params.append(f.region)
    elif f.provider_ids:
        marks = ", ".join("?" for _ in f.provider_ids)
        clauses.append(
            f"EXISTS (SELECT 1 FROM availability a WHERE a.title_id = t.id "
            f"AND a.region = ? AND a.provider_id IN ({marks}))"
        )
        params.append(f.region)
        params.extend(f.provider_ids)

    return (" AND " + " AND ".join(clauses)) if clauses else ""


_SELECT = """
    SELECT t.id, t.tmdb_id, t.media_type, t.title, t.original_title, t.overview,
           t.year, t.runtime, t.vote_average, t.popularity, t.poster_path,
           t.original_language, t.genres, t.imdb_id
"""


def search(
    conn: sqlite3.Connection,
    query: str = "",
    filters: Filters | None = None,
    limit: int = 60,
    offset: int = 0,
) -> list[dict[str, Any]]:
    f = filters or Filters()
    params: list[Any] = []

    if query.strip():
        where = _filter_sql(f, params := [_fts_query(query)])
        sql = f"""
            {_SELECT}, bm25(titles_fts, 10.0, 8.0, 4.0, 1.0) AS rank
            FROM titles_fts
            JOIN titles t ON t.id = titles_fts.rowid
            WHERE titles_fts MATCH ?{where}
            ORDER BY rank, t.popularity DESC
            LIMIT ? OFFSET ?
        """
    else:
        where = _filter_sql(f, params)
        sql = f"""
            {_SELECT}, 0 AS rank
            FROM titles t
            WHERE 1 = 1{where}
            ORDER BY t.popularity DESC
            LIMIT ? OFFSET ?
        """

    params.extend([limit, offset])
    rows = [_row(r) for r in conn.execute(sql, params).fetchall()]

    # FTS found little and the query looks like a typo -> fuzzy fallback.
    if query.strip() and len(rows) < 5:
        rows.extend(_fuzzy(conn, query, f, limit - len(rows), exclude={r["id"] for r in rows}))

    return rows


def _fuzzy(
    conn: sqlite3.Connection,
    query: str,
    f: Filters,
    limit: int,
    exclude: set[int],
) -> list[dict[str, Any]]:
    """Typo tolerance: score every candidate title against the query."""
    if limit <= 0:
        return []

    params: list[Any] = []
    where = _filter_sql(f, params)
    # Cap the candidate pool; popularity order keeps the useful ones in it.
    candidates = conn.execute(
        f"{_SELECT} FROM titles t WHERE 1 = 1{where} ORDER BY t.popularity DESC LIMIT 20000",
        params,
    ).fetchall()

    pool = {r["id"]: (r["title"] or "") for r in candidates if r["id"] not in exclude}
    if not pool:
        return []

    # Without `processor` the scorer is case-sensitive, which costs short
    # queries enough points to fall under the cutoff ("vikrma" vs "Vikram").
    matches = process.extract(
        query,
        pool,
        scorer=fuzz.WRatio,
        processor=str.lower,
        limit=limit,
        score_cutoff=FUZZY_CUTOFF,
    )
    keep = {key: score for _, score, key in matches}
    by_id = {r["id"]: r for r in candidates}
    return [
        {**_row(by_id[i]), "rank": -score / 100.0, "fuzzy": True}
        for i, score in sorted(keep.items(), key=lambda kv: -kv[1])
    ]


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    if d.get("genres"):
        try:
            d["genres"] = json.loads(d["genres"])
        except (TypeError, json.JSONDecodeError):
            d["genres"] = []
    else:
        d["genres"] = []
    return d


def offers_for(
    conn: sqlite3.Connection, title_id: int, region: str = REGION
) -> list[dict[str, Any]]:
    """Where a title can be watched, best offer type first."""
    rows = conn.execute(
        """
        SELECT a.provider_id, a.offer_type, a.link, p.name, p.logo_path,
               CASE a.offer_type
                   WHEN 'flatrate' THEN 0 WHEN 'free' THEN 1 WHEN 'ads' THEN 2
                   WHEN 'rent' THEN 3 ELSE 4
               END AS pref
        FROM availability a
        JOIN providers p ON p.provider_id = a.provider_id
        WHERE a.title_id = ? AND a.region = ?
        ORDER BY pref, p.display_priority
        """,
        (title_id, region),
    ).fetchall()
    return [dict(r) for r in rows]


def elsewhere(
    conn: sqlite3.Connection,
    title_id: int,
    exclude_region: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Countries where a title streams, for when the chosen one has nothing.

    Every region is already stored locally, so this is a plain lookup. Only
    subscription-style offers count - a country where you could merely buy it
    is not an interesting answer to "where can I watch this?".
    """
    rows = conn.execute(
        """
        SELECT a.region,
               group_concat(DISTINCT p.name) AS providers,
               min(CASE a.offer_type
                     WHEN 'flatrate' THEN 0 WHEN 'free' THEN 1 ELSE 2 END) AS pref
        FROM availability a
        JOIN providers p ON p.provider_id = a.provider_id
        WHERE a.title_id = ?
          AND a.region <> ?
          AND a.offer_type IN ('flatrate', 'free', 'ads')
        GROUP BY a.region
        ORDER BY pref, a.region
        """,
        (title_id, exclude_region.upper()),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        names = sorted({n for n in (r["providers"] or "").split(",") if n})
        out.append({"region": r["region"], "providers": names})
    return out


def facets(conn: sqlite3.Connection, region: str = REGION) -> dict[str, list]:
    """Values worth offering as filters, derived from what is actually indexed."""
    genres: dict[str, int] = {}
    for (blob,) in conn.execute("SELECT genres FROM titles WHERE genres IS NOT NULL"):
        try:
            for g in json.loads(blob):
                genres[g] = genres.get(g, 0) + 1
        except (TypeError, json.JSONDecodeError):
            continue

    provs = conn.execute(
        """
        SELECT p.provider_id, p.name, count(DISTINCT a.title_id) AS n
        FROM providers p
        JOIN availability a ON a.provider_id = p.provider_id AND a.region = ?
        GROUP BY p.provider_id
        ORDER BY n DESC, p.name
        """,
        (region,),
    ).fetchall()

    langs = conn.execute(
        """
        SELECT original_language AS code, count(*) AS n
        FROM titles WHERE original_language IS NOT NULL
        GROUP BY original_language ORDER BY n DESC LIMIT 25
        """
    ).fetchall()

    return {
        "genres": sorted(genres.items(), key=lambda kv: -kv[1]),
        "providers": [dict(r) for r in provs],
        "languages": [dict(r) for r in langs],
    }
