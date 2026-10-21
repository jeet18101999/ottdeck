# OttDeck

One searchable catalogue across every streaming service, as a native desktop
app. Search by title, actor or director — with typos — filter by anything, and
click through to whichever service actually carries it.

Not a browser app: Qt widgets, a local SQLite file, no server, no web view.

## What it does and does not do

It **indexes and organises** what is available, and **launches** playback in the
service that holds the rights. It does not host, proxy or re-stream anything
from Netflix, Prime, JioHotstar and the like — those streams are DRM-protected
and extracting them is not something this project attempts.

Three playback paths:

| Content | How it plays |
|---|---|
| Subscription catalogues | Opens the service (its installed app if registered, else its site) at the title |
| Free / ad-supported | Same, via the provider's own player |
| Your own files | Plays **inside OttDeck**, in its own player |

## Setup

**Double-click `setup.bat`.** That is the whole thing. It:

1. finds a Python 3.10+ install (via the `py` launcher or `PATH`)
2. creates `.venv` and installs everything
3. opens the TMDB key page, takes your key, and **verifies it against the API
   before saving** — a typo is caught now, not at first sync
4. asks for your country code, since streaming rights are per-country
5. creates Desktop and Start-menu shortcuts
6. offers to run the first catalogue sync
7. offers to launch the app

Re-running it is safe: each step detects what is already done, so it doubles as
a repair tool.

If PowerShell script execution is locked down on the machine, `setup.bat`
already passes `-ExecutionPolicy Bypass` for its own process only — nothing
system-wide changes.

<details>
<summary>Manual setup, or on macOS / Linux</summary>

```bash
python -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env     # then add your TMDB key
python app.py
```
</details>

### Unattended

```
setup.bat -NoPrompt              # no questions; skips the key step
setup.bat -NoPrompt -NoShortcuts
```

## Run

Use the **OttDeck** desktop shortcut, or:

```
run.bat        # the app
ottdeck.bat    # the command line, e.g. ottdeck.bat search "nolan"
```

Both launch without a console window. First launch is empty unless setup ran
the sync — use **Library → Sync catalogue** (`Ctrl+R`) to populate it; roughly
400 titles per sync at the default 5 pages, with cast, genres and per-service
availability.

The CLI does the same work headlessly:

```bash
python -m ottdeck sync --pages 10
python -m ottdeck search "nolan" --type movie --max-runtime 150
python -m ottdeck search "" --language ta --genre Action --mine
python -m ottdeck play "interstellar"
python -m ottdeck providers
python -m ottdeck subscribe 8 119 2336
```

`subscribe` records which services you pay for, which powers the **Only what I
subscribe to** filter. That filter counts `flatrate`/`free`/`ads` offers only —
a Netflix subscription does not make Netflix rentals free.

## How it is put together

```
setup.bat            one-click install (wraps scripts/setup.ps1)
run.bat              launch the app, no console window
ottdeck.bat          the CLI
app.py               launcher the shortcuts point at

src/ottdeck/
  config.py          paths, region, credentials
  db.py              SQLite schema + FTS5 index, kept in sync by triggers
  sources/tmdb.py    TMDB client and payload normalisation
  ingest.py          concurrent discovery -> details -> batched writes
  search.py          FTS5 + filters, with a rapidfuzz fallback for typos
  links.py           availability row -> clickable target per provider
  localscan.py       filename parsing, folder walk, catalogue matching
  player.py          in-app video player (Qt ffmpeg backend)
  regions.py         country codes -> readable names
  gui.py             Qt desktop UI
  cli.py             headless equivalent

scripts/
  setup.ps1          the installer proper
  make_icon.py       draws assets/ottdeck.ico
```

**Search** runs entirely against the local database, so results land as you
type. FTS5 handles prefixes (`dark knig`) and weights the title field above
cast and overview via `bm25`. When FTS returns almost nothing, rapidfuzz scores
the candidate pool so `intersteller` still finds *Interstellar*; those results
are marked as close matches.

**Ingestion** leans on TMDB's `append_to_response`, so one request per title
returns details, credits, external ids and regional watch providers together.
Titles are fetched concurrently and written in batches of 40.

**TMDB IDs are the join key.** Provider availability, your own files and the
watchlist all hang off one canonical id, which is what turns several disjoint
catalogues into a single library.

## Tests

```bash
python smoke_test.py      # schema, search, filters, links, regressions
python gui_smoke.py       # builds the Qt window offscreen and drives it
python scan_test.py       # filename parsing and catalogue matching
python local_gui_test.py  # scan -> link -> play, end to end
```

None of them need the network or a TMDB key.

## Countries

A single TMDB details call returns availability for **~115 countries at once**,
so the library stores all of them. Switching country in the sidebar is a local
query — instant, offline, and it costs no extra API calls.

```bash
ottdeck.bat regions                        # what the library holds
ottdeck.bat search "" --region US          # what streams in the US
ottdeck.bat search "dune" --region JP
```

This changes **what the app shows you**, which is a catalogue question. It does
not change what your accounts can actually play — that is decided by the
service at playback time, from where you connect and what you subscribe to.
Treat the country picker as "what's on in Japan?", not as an unlock.

`OTTDECK_REGION` sets the default and must be an **ISO 3166-1 alpha-2 code**
(`IN`, `US`, `GB`) — not a phone dialling code. TMDB answers an invalid region
with an empty catalogue rather than an error, so the symptom is every title
reading "not streaming" while the Service filter stays empty. Setup rejects
dialling codes at the prompt, and `config.py` coerces a recognised one
(`+91` → `IN`) with a warning rather than failing quietly.

## Your own files

Point OttDeck at folders holding your own media and they appear in the same
grid as everything else, with an in-app player.

```bash
ottdeck.bat scan "D:/Movies" "E:/Series"   # add folders and index them
ottdeck.bat scan                           # rescan the saved ones
ottdeck.bat local                          # what is indexed
ottdeck.bat local --unmatched              # what it could not identify
ottdeck.bat search "" --local              # titles you have a file for
```

In the app: **Library → Add media folder** (`Ctrl+O`), then **Only what is on
this PC** in the sidebar.

Release filenames are parsed for title, year, season and episode —
`Reacher.S02E03.WEBRip.x264-ION10.mp4` becomes *Reacher, season 2, episode 3* —
and matched against the catalogue by fuzzy title plus year. A show with a
`Season 2/Episode 05.mkv` layout takes the season from the folder. Files that
match nothing are listed rather than dropped, and the app offers to look them
up on TMDB and re-link them.

Playback uses PySide6's **ffmpeg backend**, which handles MKV/H.264/H.265
without installing anything. Space toggles play, arrows seek and adjust volume,
`F` is fullscreen. If a file still will not play, the window says so and offers
**Open in VLC** (found at its install path even when it is not on `PATH`).

This covers files you already have. It does not apply to subscription
catalogues — those streams are DRM-protected and OttDeck links out to the
service rather than attempting to play them.

## Worth knowing

TMDB returns one JustWatch link per title per region, not a deep link per
service, so `links.py` maps provider ids to each service's own search URL. To
get exact per-title deep links, add [Watchmode](https://api.watchmode.com) as a
second source and store its `source.web_url` in `availability.link` — every
button then upgrades with no UI change.

Personal files use the `local_media` table, which is wired through the UI but
has no scanner yet; point it at a folder or a Jellyfin instance and match on
title + year to fill it.

## Licence

MIT. See [LICENSE](LICENSE).
