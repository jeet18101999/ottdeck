"""Configuration and on-disk locations."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_data_dir

load_dotenv()

APP_NAME = "OttDeck"

DATA_DIR = Path(os.getenv("OTTDECK_DATA_DIR") or user_data_dir(APP_NAME, appauthor=False))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "library.db"
POSTER_CACHE = DATA_DIR / "posters"
POSTER_CACHE.mkdir(parents=True, exist_ok=True)

# TMDB accepts either a v3 api key or a v4 read-access bearer token.
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_BEARER = os.getenv("TMDB_BEARER", "").strip()

# Watch-provider availability is region specific; there is no global catalog.
# TMDB wants an ISO 3166-1 alpha-2 code (IN, US, GB) - not a dialling code.
_RAW_REGION = os.getenv("OTTDECK_REGION", "IN").strip().upper()

# Phone dialling codes are an easy thing to type here by mistake, and TMDB
# answers an invalid region with an empty catalogue rather than an error.
_DIALLING_CODES = {
    "+91": "IN", "91": "IN", "+1": "US", "+44": "GB", "+61": "AU",
    "+971": "AE", "+65": "SG", "+27": "ZA", "+49": "DE", "+33": "FR",
}


def _clean_region(value: str) -> tuple[str, str | None]:
    if len(value) == 2 and value.isalpha():
        return value, None
    mapped = _DIALLING_CODES.get(value)
    if mapped:
        return mapped, (
            f"OTTDECK_REGION={value!r} looks like a dialling code; using {mapped!r}. "
            f"Set OTTDECK_REGION={mapped} in .env to silence this."
        )
    return "IN", (
        f"OTTDECK_REGION={value!r} is not a two-letter country code; falling back "
        "to 'IN'. Valid examples: IN, US, GB, AE."
    )


REGION, REGION_WARNING = _clean_region(_RAW_REGION)
LANGUAGE = os.getenv("OTTDECK_LANGUAGE", "en-US")

IMAGE_BASE = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w342"
BACKDROP_SIZE = "w780"

# TMDB tolerates ~50 req/s; stay well under it so ingestion never gets throttled.
MAX_CONCURRENCY = int(os.getenv("OTTDECK_CONCURRENCY", "8"))


def has_credentials() -> bool:
    return bool(TMDB_API_KEY or TMDB_BEARER)


def poster_url(path: str | None, size: str = POSTER_SIZE) -> str | None:
    if not path:
        return None
    return f"{IMAGE_BASE}/{size}{path}"
