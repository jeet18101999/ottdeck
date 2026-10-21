"""Catalogue ingestion.

Discovery endpoints give us ids cheaply; the expensive part is one details call
per title. Those run concurrently and write to SQLite in batches.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import db
from .config import REGION
from .sources.tmdb import TmdbClient, extract_all_offers, normalise

Progress = Callable[[str, int, int], None]


def _noop(stage: str, done: int, total: int) -> None:
    pass


async def _fetch_one(
    client: TmdbClient, tmdb_id: int, media_type: str
) -> tuple[dict, list, list] | None:
    try:
        payload = await client.details(tmdb_id, media_type)
    except Exception:
        return None
    rec = normalise(payload, media_type)
    if not rec.get("title"):
        return None
    # One payload carries every region, so store them all - switching country
    # later then costs nothing and works offline.
    offers_by_region, providers = extract_all_offers(payload)
    return rec, offers_by_region, providers


async def ingest_ids(
    client: TmdbClient,
    ids: Iterable[tuple[int, str]],
    *,
    region: str = REGION,
    progress: Progress = _noop,
    batch_size: int = 40,
) -> int:
    """Fetch details for (tmdb_id, media_type) pairs and store them."""
    pairs = list(dict.fromkeys(ids))  # de-dupe, keep order
    total = len(pairs)
    written = 0

    for start in range(0, total, batch_size):
        chunk = pairs[start : start + batch_size]
        results = await asyncio.gather(
            *(_fetch_one(client, tid, mt) for tid, mt in chunk)
        )

        with db.session() as conn:
            for result in results:
                if result is None:
                    continue
                rec, offers_by_region, providers = result
                title_id = db.upsert_title(conn, rec)
                for p in providers:
                    db.upsert_provider(conn, p)
                db.replace_all_availability(conn, title_id, offers_by_region)
                written += 1
            db.set_meta(
                conn, "last_ingest", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )

        progress("titles", min(start + batch_size, total), total)

    return written


async def _collect(
    client: TmdbClient, pages: int, progress: Progress
) -> list[tuple[int, str]]:
    """Build the id list from trending + popular + per-provider discovery."""
    found: list[tuple[int, str]] = []

    for item in await client.trending("all", "week"):
        mt = item.get("media_type")
        if mt in ("movie", "tv"):
            found.append((item["id"], mt))
    progress("discover", 1, pages * 2 + 1)

    tasks = []
    for media_type in ("movie", "tv"):
        for page in range(1, pages + 1):
            tasks.append(client.popular(media_type, page))
            tasks.append(client.discover(media_type, page))

    done = 1
    for coro in asyncio.as_completed(tasks):
        try:
            batch = await coro
        except Exception:
            continue
        for item in batch:
            mt = item.get("media_type") or ("tv" if "name" in item else "movie")
            if mt in ("movie", "tv"):
                found.append((item["id"], mt))
        done += 1
        progress("discover", done, len(tasks) + 1)

    return list(dict.fromkeys(found))


async def refresh_catalogue(
    pages: int = 5, *, region: str = REGION, progress: Progress = _noop
) -> dict[str, Any]:
    """Full sync: discover ids, fetch details, store providers + availability."""
    db.init_db()

    warnings: list[str] = []

    async with TmdbClient(region=region) as client:
        # Seed the provider table so filters work even before any title lands.
        # A failure here is reported, not swallowed: when this silently skipped,
        # a bad region looked like "nothing is streaming" instead of an error.
        for media_type in ("movie", "tv"):
            try:
                provs = await client.provider_list(media_type)
            except Exception as exc:
                warnings.append(f"provider list ({media_type}) failed: {exc}")
                continue
            if not provs:
                warnings.append(
                    f"TMDB returned no {media_type} providers for region {region!r} "
                    "- check OTTDECK_REGION is a two-letter country code."
                )
            with db.session() as conn:
                for p in provs:
                    db.upsert_provider(conn, p)

        ids = await _collect(client, pages, progress)
        written = await ingest_ids(client, ids, region=region, progress=progress)

    with db.session() as conn:
        regions = dict(db.known_regions(conn))
        result = {
            "discovered": len(ids),
            "written": written,
            "regions": len(regions),
            **db.stats(conn),
        }

    local = regions.get(region, 0)
    if result["availability"] == 0 and result["titles"]:
        warnings.append(
            f"Stored {result['titles']} titles but no availability anywhere - "
            "check the TMDB key and network."
        )
    elif local == 0 and result["titles"]:
        warnings.append(
            f"No titles are streaming in region {region!r}. "
            "Check OTTDECK_REGION is a two-letter country code."
        )
    result["warnings"] = warnings
    return result


async def ingest_search(query: str, *, region: str = REGION, limit: int = 20) -> int:
    """Pull a specific title into the library on demand."""
    db.init_db()
    async with TmdbClient(region=region) as client:
        results = await client.search_multi(query)
        ids = [(r["id"], r["media_type"]) for r in results[:limit]]
        return await ingest_ids(client, ids, region=region)
