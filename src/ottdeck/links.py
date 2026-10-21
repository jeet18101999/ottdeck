"""Turning an availability row into something clickable.

TMDB gives one JustWatch link per title per region, not a per-provider deep
link. So: prefer a native app URI when the provider has one, fall back to the
provider's own search page, and keep the JustWatch link as a last resort.

Exact per-title deep links need Watchmode (`/title/{id}/details/`), which
returns a `source.web_url` and `source.ios_url` per platform. Wire that in as a
second source and store it in `availability.link` to upgrade every button here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import quote_plus

# TMDB/JustWatch provider ids -> that service's own search URL, which the OS
# hands to the installed desktop app when it has registered the domain.
# A bare scheme like `netflix://` opens the app but cannot carry a title, so a
# search URL beats it on every provider here.
PROVIDERS: dict[int, dict[str, str]] = {
    8:    {"name": "Netflix",        "search": "https://www.netflix.com/search?q={q}"},
    119:  {"name": "Amazon Prime Video",
           "search": "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}"},
    9:    {"name": "Amazon Prime Video",
           "search": "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}"},
    122:  {"name": "JioHotstar",
           "search": "https://www.hotstar.com/in/search?q={q}"},
    2336: {"name": "JioHotstar",
           "search": "https://www.hotstar.com/in/search?q={q}"},
    232:  {"name": "ZEE5",          "search": "https://www.zee5.com/search?q={q}"},
    237:  {"name": "SonyLIV",       "search": "https://www.sonyliv.com/search?searchTerm={q}"},
    121:  {"name": "Voot",          "search": "https://www.voot.com/search?q={q}"},
    350:  {"name": "Apple TV+",     "search": "https://tv.apple.com/search?term={q}"},
    2:    {"name": "Apple TV",      "search": "https://tv.apple.com/search?term={q}"},
    3:    {"name": "Google Play Movies",
           "search": "https://play.google.com/store/search?q={q}&c=movies"},
    192:  {"name": "YouTube",       "search": "https://www.youtube.com/results?search_query={q}"},
    11:   {"name": "MUBI",          "search": "https://mubi.com/search/{q}"},
    515:  {"name": "MUBI",          "search": "https://mubi.com/search/{q}"},
    220:  {"name": "JioCinema",     "search": "https://www.jiocinema.com/search/{q}"},
    503:  {"name": "MX Player",     "search": "https://www.mxplayer.in/search?q={q}"},
    73:   {"name": "Tubi",          "search": "https://tubitv.com/search/{q}"},
    300:  {"name": "Pluto TV",      "search": "https://pluto.tv/en/search?query={q}"},
    1898: {"name": "Crunchyroll",   "search": "https://www.crunchyroll.com/search?q={q}"},
    283:  {"name": "Crunchyroll",   "search": "https://www.crunchyroll.com/search?q={q}"},
    337:  {"name": "Disney+",       "search": "https://www.disneyplus.com/search?q={q}"},
    1968: {"name": "Lionsgate Play","search": "https://www.lionsgateplay.com/search?q={q}"},
}

OFFER_LABEL = {
    "flatrate": "Included",
    "free": "Free",
    "ads": "Free with ads",
    "rent": "Rent",
    "buy": "Buy",
}


@dataclass
class PlayTarget:
    label: str          # "Netflix - Included"
    url: str            # what to hand the OS
    provider_id: int
    offer_type: str


def build_targets(title: str, offers: list[dict]) -> list[PlayTarget]:
    """One button per place the title can be watched."""
    q = quote_plus(title)
    targets: list[PlayTarget] = []

    for o in offers:
        pid = o["provider_id"]
        spec = PROVIDERS.get(pid)
        name = (spec or {}).get("name") or o.get("name") or f"Provider {pid}"
        offer = o.get("offer_type", "flatrate")

        # Prefer the provider's own search page; fall back to TMDB's
        # JustWatch link for services not in the table above.
        if spec and spec.get("search"):
            url = spec["search"].format(q=q)
        elif o.get("link"):
            url = o["link"]
        else:
            continue

        targets.append(
            PlayTarget(
                label=f"{name} - {OFFER_LABEL.get(offer, offer)}",
                url=url,
                provider_id=pid,
                offer_type=offer,
            )
        )

    # De-duplicate: one button per provider, best offer wins (offers arrive sorted).
    seen: set[int] = set()
    unique = []
    for t in targets:
        if t.provider_id in seen:
            continue
        seen.add(t.provider_id)
        unique.append(t)
    return unique


def open_target(target: PlayTarget) -> None:
    """Hand the URL to the OS; the installed app takes it if it is registered."""
    _open_url(target.url)


def _open_url(url: str) -> None:
    if sys.platform == "win32":
        os.startfile(url)  # noqa: S606 - the documented Windows shell-open call
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


# ---- local files --------------------------------------------------------

PLAYERS = ("mpv", "vlc")

# Neither installer puts itself on PATH by default on Windows.
_WINDOWS_PLAYER_PATHS = (
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files\mpv\mpv.exe",
    r"C:\Program Files\mpv-x86_64\mpv.exe",
)


def find_external_player() -> str | None:
    for exe in PLAYERS:
        found = shutil.which(exe)
        if found:
            return found
    if sys.platform == "win32":
        for candidate in _WINDOWS_PLAYER_PATHS:
            if os.path.isfile(candidate):
                return candidate
    return None


def play_local(path: str) -> None:
    """Play a personal file in mpv/VLC if present, else the system default."""
    player = find_external_player()
    if player:
        subprocess.Popen([player, path])
        return
    _open_url(path)
