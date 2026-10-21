"""Finding your own media on disk and matching it to the catalogue.

Release filenames are noisy (`Movie.Name.2014.1080p.BluRay.x264-GROUP.mkv`),
so the job is to recover a title, a year, and for episodes a season/episode
number, then line that up against a TMDB title already in the library.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from rapidfuzz import fuzz

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".divx", ".ogv", ".3gp",
}

# Everything a release group bolts on after the actual title.
_JUNK = r"""
    2160p|1080p|720p|480p|4k|uhd|hdr10|hdr|sdr|dolby ?vision|dv
  | bluray|blu ray|brrip|bdrip|webrip|web ?dl|web|hdrip|dvdrip|dvdscr|hdtv|pdtv
  | remux|proper|repack|extended|uncut|unrated|limited|internal|readnfo
  | x264|x265|h ?264|h ?265|hevc|avc|xvid|divx
  | aac|ac3|eac3|dts ?hd|dts|truehd|atmos|flac|mp3|opus|ddp?5 ?1|5 ?1|7 ?1|2 ?0
  | dual ?audio|multi|subbed|dubbed|esub|msub|hindi|tamil|telugu|english|korean
  | amzn|nf|hulu|dsnp|hmax|atvp|mx|sonyliv|hotstar|zee5
  | complete|season|series|part
"""
_JUNK_RE = re.compile(rf"^(?:{_JUNK})$", re.IGNORECASE | re.VERBOSE)

_YEAR_RE = re.compile(r"\b(19[0-9]{2}|20[0-4][0-9])\b")

# Ordered: the most explicit episode notation wins.
_EPISODE_PATTERNS = (
    re.compile(r"\bs(?P<s>\d{1,2})[\s._-]*e(?P<e>\d{1,3})\b", re.I),
    re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{1,3})\b", re.I),
    re.compile(r"\bseason[\s._-]*(?P<s>\d{1,2})[\s._-]*episode[\s._-]*(?P<e>\d{1,3})\b", re.I),
    re.compile(r"\bseries[\s._-]*(?P<s>\d{1,2})[\s._-]*(?:ep|episode)[\s._-]*(?P<e>\d{1,3})\b", re.I),
)
_SEASON_DIR_RE = re.compile(r"\b(?:season|series|s)[\s._-]*(\d{1,2})\b", re.I)
# `episode` must come first or `ep`/`e` would match its prefix and then fail.
_EPISODE_ONLY_RE = re.compile(r"\b(?:episode|ep|e)[\s._-]*(\d{1,3})\b", re.I)


@dataclass
class ParsedFile:
    path: str
    kind: str                  # 'movie' | 'tv'
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def media_type(self) -> str:
        return self.kind


def _normalise(stem: str) -> str:
    """Dots and underscores are word separators in release names."""
    # Collapse dotted initialisms (S.W.A.T. -> SWAT) but keep the trailing dot
    # working as a separator, or the next token would be glued on.
    stem = re.sub(
        r"\b(?:[A-Za-z]\.){2,}[A-Za-z]?\b",
        lambda m: m.group(0).replace(".", "") + (" " if m.group(0).endswith(".") else ""),
        stem,
    )
    stem = re.sub(r"[._]+", " ", stem)
    stem = re.sub(r"\s*-\s*", " - ", stem)
    return re.sub(r"\s{2,}", " ", stem).strip()


def _strip_junk(text: str) -> str:
    """Drop release cruft from the tail, stopping at the first real word."""
    # Bracketed groups are never part of a title.
    text = re.sub(r"[\[(){}]([^\[\](){}]*)[\])}]", " ", text)
    words = [w for w in re.split(r"[\s-]+", text) if w]
    while words and _JUNK_RE.match(words[-1]):
        words.pop()
    cleaned = " ".join(words)
    # Slicing a title off at a year can leave an orphaned bracket behind.
    cleaned = re.sub(r"[\s._\-([{]+$", "", cleaned)
    return re.sub(r"^[\s._\-)\]}]+", "", cleaned).strip()


def parse_filename(path: str | os.PathLike[str]) -> ParsedFile:
    """Recover title/year/season/episode from a path."""
    p = Path(path)
    stem = _normalise(p.stem)

    season = episode = None
    kind = "movie"
    title_part = stem

    for pattern in _EPISODE_PATTERNS:
        m = pattern.search(stem)
        if m:
            season, episode = int(m.group("s")), int(m.group("e"))
            kind = "tv"
            title_part = stem[: m.start()]
            break

    if kind == "movie":
        # `Show/Season 2/Episode 5.mkv` carries the season in the folder name.
        parent = p.parent.name
        dir_season = _SEASON_DIR_RE.search(_normalise(parent))
        ep_only = _EPISODE_ONLY_RE.search(stem)
        if dir_season and ep_only:
            season, episode = int(dir_season.group(1)), int(ep_only.group(1))
            kind = "tv"
            title_part = stem[: ep_only.start()]
            if not _strip_junk(title_part):
                # Filename was just "Episode 5" - the show name is further up.
                grand = p.parent.parent.name
                title_part = _normalise(grand or parent)

    # Pull the year for episodes too: `S.W.A.T.2017.S01E01` is a disambiguating
    # year, and it helps matching rather than polluting the title.
    year = None
    years = _YEAR_RE.findall(title_part)
    if years:
        year = int(years[-1])
        idx = title_part.rfind(years[-1])
        title_part = title_part[:idx]

    title = _strip_junk(title_part)
    if not title:
        # Nothing usable in the filename; fall back to the containing folder.
        title = _strip_junk(_normalise(p.parent.name)) or p.stem

    return ParsedFile(str(p), kind, title, year, season, episode)


def walk_media(roots: Iterable[str | os.PathLike[str]]) -> Iterator[Path]:
    """Every video file under the given folders."""
    for root in roots:
        base = Path(root)
        if base.is_file():
            if base.suffix.lower() in VIDEO_EXT:
                yield base
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in VIDEO_EXT:
                    yield Path(dirpath) / name


# ---- matching against the catalogue ------------------------------------


def match_local(
    conn: sqlite3.Connection, parsed: ParsedFile, min_score: float = 78.0
) -> int | None:
    """Best matching title already in the library, or None."""
    candidates = conn.execute(
        "SELECT id, title, original_title, year, media_type FROM titles "
        "WHERE media_type = ?",
        (parsed.kind,),
    ).fetchall()
    if not candidates:
        return None

    want = parsed.title.lower()
    best_id, best = None, 0.0
    for row in candidates:
        for field in (row["title"], row["original_title"]):
            if not field:
                continue
            score = fuzz.WRatio(want, field.lower())
            # A matching year is strong evidence; a clashing one is not fatal
            # because release names get it wrong often enough.
            if parsed.year and row["year"]:
                score += 8 if abs(parsed.year - row["year"]) <= 1 else -12
            if score > best:
                best_id, best = row["id"], score

    return best_id if best >= min_score else None


def record(
    conn: sqlite3.Connection, parsed: ParsedFile, title_id: int | None, size: int
) -> None:
    conn.execute(
        """
        INSERT INTO local_media (path, title_id, size_bytes, added_at,
                                 kind, season, episode, guess_title, guess_year)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (path) DO UPDATE SET
            title_id    = excluded.title_id,
            size_bytes  = excluded.size_bytes,
            kind        = excluded.kind,
            season      = excluded.season,
            episode     = excluded.episode,
            guess_title = excluded.guess_title,
            guess_year  = excluded.guess_year
        """,
        (
            parsed.path,
            title_id,
            size,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            parsed.kind,
            parsed.season,
            parsed.episode,
            parsed.title,
            parsed.year,
        ),
    )


def scan(
    roots: Iterable[str | os.PathLike[str]],
    conn: sqlite3.Connection,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    """Index every video under `roots`, matching what we can to the catalogue."""
    files = list(walk_media(roots))
    total = len(files)
    matched = unmatched = 0

    for i, path in enumerate(files, 1):
        parsed = parse_filename(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        title_id = match_local(conn, parsed)
        record(conn, parsed, title_id, size)
        if title_id:
            matched += 1
        else:
            unmatched += 1
        if progress:
            progress(i, total, parsed.title)

    return {"files": total, "matched": matched, "unmatched": unmatched}


def unmatched_titles(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Guessed names for files nothing in the catalogue matched.

    These are the queries worth sending to TMDB to fill the gaps.
    """
    rows = conn.execute(
        """
        SELECT guess_title, guess_year, kind, count(*) AS files
        FROM local_media
        WHERE title_id IS NULL AND guess_title IS NOT NULL
        GROUP BY lower(guess_title), kind
        ORDER BY files DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def library_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT lm.path, lm.kind, lm.season, lm.episode, lm.size_bytes,
               lm.guess_title, lm.guess_year, t.title, t.id AS title_id
        FROM local_media lm
        LEFT JOIN titles t ON t.id = lm.title_id
        ORDER BY coalesce(t.title, lm.guess_title), lm.season, lm.episode
        """
    ).fetchall()
    return [dict(r) for r in rows]
