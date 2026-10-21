"""TMDB client.

`append_to_response` is what makes ingestion cheap: details, credits, external
ids and per-region watch providers all arrive in a single request per title.
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable

import httpx

from ..config import (
    LANGUAGE,
    MAX_CONCURRENCY,
    REGION,
    TMDB_API_KEY,
    TMDB_BEARER,
)

BASE = "https://api.themoviedb.org/3"

# TMDB labels offers by monetisation type; we keep its vocabulary as-is.
OFFER_TYPES = ("flatrate", "free", "ads", "rent", "buy")


class TmdbError(RuntimeError):
    pass


class TmdbClient:
    def __init__(self, *, region: str = REGION, language: str = LANGUAGE) -> None:
        if not (TMDB_API_KEY or TMDB_BEARER):
            raise TmdbError(
                "No TMDB credentials. Put TMDB_API_KEY (v3) or TMDB_BEARER (v4) in .env"
            )
        self.region = region
        self.language = language
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)

        headers = {"accept": "application/json"}
        if TMDB_BEARER:
            headers["Authorization"] = f"Bearer {TMDB_BEARER}"

        self._client = httpx.AsyncClient(
            base_url=BASE,
            headers=headers,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "TmdbClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("language", self.language)
        if TMDB_API_KEY and not TMDB_BEARER:
            params["api_key"] = TMDB_API_KEY

        async with self._sem:
            for attempt in range(4):
                resp = await self._client.get(path, params=params)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 1)) + attempt
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
        raise TmdbError(f"TMDB kept failing for {path}")

    # ---- discovery: what to ingest -------------------------------------

    async def trending(self, media_type: str = "all", window: str = "week") -> list[dict]:
        data = await self._get(f"/trending/{media_type}/{window}")
        return data.get("results", [])

    async def popular(self, media_type: str = "movie", page: int = 1) -> list[dict]:
        data = await self._get(f"/{media_type}/popular", page=page, region=self.region)
        return data.get("results", [])

    async def discover(
        self,
        media_type: str = "movie",
        page: int = 1,
        *,
        provider_ids: Iterable[int] | None = None,
        language: str | None = None,
        year: int | None = None,
        sort_by: str = "popularity.desc",
    ) -> list[dict]:
        """Catalogue sweep. `provider_ids` pulls what a given service carries here."""
        params: dict[str, Any] = {
            "page": page,
            "sort_by": sort_by,
            "watch_region": self.region,
            "with_original_language": language,
        }
        if provider_ids:
            params["with_watch_providers"] = "|".join(str(p) for p in provider_ids)
        if year:
            key = "primary_release_year" if media_type == "movie" else "first_air_date_year"
            params[key] = year

        data = await self._get(f"/discover/{media_type}", **params)
        return data.get("results", [])

    async def search_multi(self, query: str, page: int = 1) -> list[dict]:
        data = await self._get("/search/multi", query=query, page=page)
        return [
            r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")
        ]

    # ---- the per-title fetch -------------------------------------------

    async def details(self, tmdb_id: int, media_type: str) -> dict[str, Any]:
        return await self._get(
            f"/{media_type}/{tmdb_id}",
            append_to_response="credits,watch/providers,external_ids",
        )

    async def provider_list(self, media_type: str = "movie") -> list[dict]:
        data = await self._get(
            f"/watch/providers/{media_type}", watch_region=self.region
        )
        return [normalise_provider(p) for p in data.get("results", [])]


# ---- normalisation: TMDB payload -> our row shape -----------------------


def normalise_provider(entry: dict[str, Any]) -> dict[str, Any]:
    """TMDB calls it `provider_name` everywhere; our `providers` row calls it `name`.

    Both the provider directory and the per-title offer blocks come through
    here so the two can never drift apart again.
    """
    pid = entry.get("provider_id")
    return {
        "provider_id": pid,
        "name": entry.get("provider_name") or entry.get("name") or f"Provider {pid}",
        "logo_path": entry.get("logo_path"),
        "display_priority": entry.get("display_priority"),
    }


def _people_blob(credits: dict[str, Any]) -> str:
    """Top cast plus directors, flattened into one searchable string."""
    cast = [c.get("name", "") for c in (credits.get("cast") or [])[:12]]
    crew = [
        c.get("name", "")
        for c in (credits.get("crew") or [])
        if c.get("job") in ("Director", "Creator") or c.get("department") == "Directing"
    ][:5]
    return ", ".join(n for n in [*cast, *crew] if n)


def normalise(payload: dict[str, Any], media_type: str) -> dict[str, Any]:
    """Flatten a TMDB details payload into a `titles` row."""
    is_movie = media_type == "movie"
    date = payload.get("release_date") if is_movie else payload.get("first_air_date")
    year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None

    runtime = payload.get("runtime")
    if not is_movie:
        eps = payload.get("episode_run_time") or []
        runtime = eps[0] if eps else None

    external = payload.get("external_ids") or {}

    return {
        "tmdb_id": payload["id"],
        "media_type": media_type,
        "title": payload.get("title") if is_movie else payload.get("name"),
        "original_title": (
            payload.get("original_title") if is_movie else payload.get("original_name")
        ),
        "overview": payload.get("overview"),
        "people": _people_blob(payload.get("credits") or {}),
        "release_date": date,
        "year": year,
        "runtime": runtime,
        "vote_average": payload.get("vote_average"),
        "vote_count": payload.get("vote_count"),
        "popularity": payload.get("popularity"),
        "poster_path": payload.get("poster_path"),
        "backdrop_path": payload.get("backdrop_path"),
        "original_language": payload.get("original_language"),
        "genres": [g["name"] for g in (payload.get("genres") or [])],
        "imdb_id": payload.get("imdb_id") or external.get("imdb_id"),
        "homepage": payload.get("homepage"),
    }


def _offers_in_block(block: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    offers: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    seen: set[int] = set()

    for offer_type in OFFER_TYPES:
        for entry in block.get(offer_type) or []:
            pid = entry.get("provider_id")
            if pid is None:
                continue
            offers.append(
                {
                    "provider_id": pid,
                    "offer_type": offer_type,
                    # TMDB gives one JustWatch link for the whole region, not per offer.
                    "link": block.get("link"),
                }
            )
            if pid not in seen:
                seen.add(pid)
                providers.append(normalise_provider(entry))

    return offers, providers


def extract_offers(payload: dict[str, Any], region: str) -> tuple[list[dict], list[dict]]:
    """Pull (offers, providers) for one region out of a details payload.

    TMDB nests this under the literal key "watch/providers".
    """
    block = (payload.get("watch/providers") or {}).get("results", {}).get(region)
    if not block:
        return [], []
    return _offers_in_block(block)


def extract_all_offers(
    payload: dict[str, Any],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Every region in one go: ({region: offers}, providers).

    A details call already carries all ~115 regions, so storing them costs no
    extra requests and makes switching country instant and offline.
    """
    results = (payload.get("watch/providers") or {}).get("results") or {}
    by_region: dict[str, list[dict]] = {}
    providers: dict[int, dict] = {}

    for region, block in results.items():
        if not isinstance(block, dict):
            continue
        offers, provs = _offers_in_block(block)
        if offers:
            by_region[region.upper()] = offers
        for p in provs:
            providers.setdefault(p["provider_id"], p)

    return by_region, list(providers.values())
