"""Command line entry point — the headless half of the app.

    python -m ottdeck init
    python -m ottdeck sync --pages 5
    python -m ottdeck add "Jawan"
    python -m ottdeck search "space" --type movie --max-runtime 150
    python -m ottdeck providers
    python -m ottdeck subscribe 8 119 2336
    python -m ottdeck stats
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import db, ingest, links, search as search_mod
from .config import DB_PATH, REGION, has_credentials


def _progress(stage: str, done: int, total: int) -> None:
    pct = (done / total * 100) if total else 0
    print(f"\r  {stage:<10} {done}/{total} ({pct:5.1f}%)", end="", file=sys.stderr)
    if done >= total:
        print(file=sys.stderr)


def cmd_init(args: argparse.Namespace) -> int:
    db.init_db()
    print(f"Database ready: {DB_PATH}")
    if not has_credentials():
        print("\nNo TMDB key yet. Create a .env file next to this project with:")
        print("  TMDB_API_KEY=your_v3_key")
        print("Get one free at https://www.themoviedb.org/settings/api")
        return 1
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if not has_credentials():
        print("No TMDB credentials. Run `init` for instructions.", file=sys.stderr)
        return 1
    print(f"Syncing catalogue for region {REGION} ({args.pages} pages per source)...")
    result = asyncio.run(ingest.refresh_catalogue(pages=args.pages, progress=_progress))
    print(f"\nDiscovered {result['discovered']} ids, stored {result['written']} titles.")
    print(f"Library: {result['titles']} titles, {result['availability']} availability rows.")
    for warning in result.get("warnings", []):
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    if not has_credentials():
        print("No TMDB credentials. Run `init` for instructions.", file=sys.stderr)
        return 1
    n = asyncio.run(ingest.ingest_search(args.query))
    print(f"Added/updated {n} titles matching {args.query!r}.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    filters = search_mod.Filters(
        region=(args.region or REGION).upper(),
        media_type=args.type,
        genres=tuple(args.genre or ()),
        year_min=args.year_min,
        year_max=args.year_max,
        runtime_max=args.max_runtime,
        language=args.language,
        only_my_providers=args.mine,
        only_local=args.local,
        min_rating=args.min_rating,
    )
    with db.session() as conn:
        rows = search_mod.search(conn, args.query, filters, limit=args.limit)
        if not rows:
            print("No matches. Try `sync` first, or `add <title>` to pull one in.")
            return 1

        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0

        for r in rows:
            offers = search_mod.offers_for(conn, r["id"], filters.region)
            where = ", ".join(
                dict.fromkeys(
                    f"{o['name']}"
                    + ("" if o["offer_type"] == "flatrate" else f" ({o['offer_type']})")
                    for o in offers
                )
            )
            if not where:
                other = search_mod.elsewhere(conn, r["id"], filters.region, limit=4)
                where = (
                    "not in "
                    + filters.region
                    + "; try "
                    + ", ".join(
                        f"{e['region']} ({e['providers'][0]})" for e in other
                    )
                ) if other else "not streaming anywhere"
            kind = "TV" if r["media_type"] == "tv" else "Film"
            rating = f"{r['vote_average']:.1f}" if r["vote_average"] else " -- "
            fuzzy = "  ~" if r.get("fuzzy") else ""
            genres = "/".join(r["genres"][:3])
            print(f"\n{r['title']} ({r['year'] or '?'})  [{kind}] {rating}{fuzzy}")
            print(f"    {genres}")
            print(f"    -> {where}")
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    region = (args.region or REGION).upper()
    with db.session() as conn:
        rows = search_mod.search(conn, args.query, limit=1)
        if not rows:
            print("Nothing matched.", file=sys.stderr)
            return 1
        row = rows[0]
        offers = search_mod.offers_for(conn, row["id"], region)
        targets = links.build_targets(row["title"], offers)

    if not targets:
        print(f"{row['title']} is not streaming in {region}.", file=sys.stderr)
        return 1

    print(f"{row['title']} ({row['year']})")
    for i, t in enumerate(targets, 1):
        print(f"  {i}. {t.label}")

    pick = targets[0] if args.first else None
    if pick is None:
        try:
            choice = input("\nOpen which? [1] ").strip() or "1"
            pick = targets[int(choice) - 1]
        except (ValueError, IndexError, EOFError, KeyboardInterrupt):
            return 1

    print(f"Opening {pick.label} ...")
    links.open_target(pick)
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    with db.session() as conn:
        region = (args.region or REGION).upper()
        f = search_mod.facets(conn, region)
        mine = {r[0] for r in conn.execute("SELECT provider_id FROM my_providers")}
        if not f["providers"]:
            print("No providers indexed yet. Run `sync` first.")
            return 1
        for p in f["providers"][: args.limit]:
            mark = "*" if p["provider_id"] in mine else " "
            print(f" {mark} {p['provider_id']:>5}  {p['name']:<28} {p['n']:>6} titles")
        print("\n(* = subscribed; set with `subscribe <id> <id> ...`)")
    return 0


def cmd_subscribe(args: argparse.Namespace) -> int:
    with db.session() as conn:
        if args.clear:
            conn.execute("DELETE FROM my_providers")
        conn.executemany(
            "INSERT OR IGNORE INTO my_providers (provider_id) VALUES (?)",
            [(int(p),) for p in args.provider_ids],
        )
        names = conn.execute(
            "SELECT name FROM providers WHERE provider_id IN "
            "(SELECT provider_id FROM my_providers)"
        ).fetchall()
    print("Subscriptions: " + (", ".join(n[0] for n in names) or "(none)"))
    return 0


def cmd_regions(args: argparse.Namespace) -> int:
    from .regions import country_label

    with db.session() as conn:
        rows = db.known_regions(conn)
    if not rows:
        print("No availability data yet. Run `sync` first.")
        return 1
    print(f"{len(rows)} countries in the library (current: {REGION})\n")
    for code, n in rows[: args.limit]:
        mark = "*" if code == REGION else " "
        print(f" {mark} {code}  {country_label(code):<28} {n:>5} titles")
    print('\nBrowse another with:  ottdeck search "" --region US')
    return 0


def _media_roots(conn) -> list[str]:
    raw = db.get_meta(conn, "media_roots", "")
    return [r for r in (raw or "").split("|") if r]


def cmd_scan(args: argparse.Namespace) -> int:
    from . import localscan

    with db.session() as conn:
        roots = [] if args.forget else _media_roots(conn)
        for folder in args.folders:
            path = str(Path(folder).expanduser().resolve())
            if not Path(path).exists():
                print(f"No such folder: {folder}", file=sys.stderr)
                return 1
            if path not in roots:
                roots.append(path)
        if not roots:
            print("Nothing to scan. Pass a folder: ottdeck scan \"D:/Movies\"")
            return 1
        db.set_meta(conn, "media_roots", "|".join(roots))

    print("Scanning:")
    for r in roots:
        print(f"  {r}")

    def show(done: int, total: int, name: str) -> None:
        print(f"\r  {done}/{total}  {name[:48]:<48}", end="", file=sys.stderr)

    with db.session() as conn:
        result = localscan.scan(roots, conn, progress=show)
    print(file=sys.stderr)
    print(
        f"\n{result['files']} files - {result['matched']} matched, "
        f"{result['unmatched']} unrecognised."
    )
    if result["unmatched"]:
        print("Pull the missing ones in with:  ottdeck local --unmatched")
    return 0


def cmd_local(args: argparse.Namespace) -> int:
    from . import localscan

    with db.session() as conn:
        if args.unmatched:
            gaps = localscan.unmatched_titles(conn, limit=args.limit)
            if not gaps:
                print("Every local file is matched to the catalogue.")
                return 0
            print("Not in the catalogue yet:\n")
            for g in gaps:
                year = f" ({g['guess_year']})" if g["guess_year"] else ""
                print(f"  {g['guess_title']}{year}   [{g['kind']}, {g['files']} file(s)]")
            print("\nFetch one with:  ottdeck add \"<title>\"  then  ottdeck scan")
            return 0

        rows = localscan.library_rows(conn)
        if not rows:
            print("No local media indexed. Add a folder:  ottdeck scan \"D:/Movies\"")
            return 1
        for r in rows[: args.limit]:
            name = r["title"] or f"{r['guess_title']} (unmatched)"
            ep = (
                f"  S{r['season']:02d}E{r['episode']:02d}"
                if r["season"] is not None and r["episode"] is not None
                else ""
            )
            size = f"{(r['size_bytes'] or 0) / 1e9:.1f}G"
            print(f"  {name}{ep:<10} {size:>7}  {r['path']}")
        print(f"\n{len(rows)} file(s) indexed.")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    # Imported lazily so the CLI still works on a machine without Qt installed.
    from .gui import main as gui_main

    return gui_main()


def cmd_stats(args: argparse.Namespace) -> int:
    with db.session() as conn:
        s = db.stats(conn)
        last = db.get_meta(conn, "last_ingest", "never")
    print(f"Database : {DB_PATH}")
    print(f"Region   : {REGION}")
    print(f"Synced   : {last}")
    for k, v in s.items():
        print(f"  {k:<14} {v:>8}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ottdeck", description="Unified OTT catalogue")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    s = sub.add_parser("sync", help="pull the catalogue from TMDB")
    s.add_argument("--pages", type=int, default=5, help="pages per source (20 titles each)")
    s.set_defaults(func=cmd_sync)

    a = sub.add_parser("add", help="pull one specific title in")
    a.add_argument("query")
    a.set_defaults(func=cmd_add)

    q = sub.add_parser("search", help="search the local catalogue")
    q.add_argument("query", nargs="?", default="")
    q.add_argument("--type", choices=["movie", "tv"])
    q.add_argument("--genre", action="append")
    q.add_argument("--year-min", type=int)
    q.add_argument("--year-max", type=int)
    q.add_argument("--max-runtime", type=int, metavar="MIN")
    q.add_argument("--language", help="ISO code, e.g. ta, hi, en")
    q.add_argument("--min-rating", type=float)
    q.add_argument("--mine", action="store_true", help="only services I subscribe to")
    q.add_argument("--region", help="browse another country's catalogue, e.g. US, GB, JP")
    q.add_argument("--local", action="store_true", help="only titles I have a file for")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_search)

    pl = sub.add_parser("play", help="open the best match on its service")
    pl.add_argument("query")
    pl.add_argument("--first", action="store_true", help="skip the prompt")
    pl.add_argument("--region", help="resolve in another country, e.g. US")
    pl.set_defaults(func=cmd_play)

    pr = sub.add_parser("providers", help="list indexed services")
    pr.add_argument("--limit", type=int, default=40)
    pr.add_argument("--region", help="services in another country, e.g. US, GB")
    pr.set_defaults(func=cmd_providers)

    sb = sub.add_parser("subscribe", help="record which services you pay for")
    sb.add_argument("provider_ids", nargs="+")
    sb.add_argument("--clear", action="store_true", help="replace instead of append")
    sb.set_defaults(func=cmd_subscribe)

    rg = sub.add_parser("regions", help="countries the library has data for")
    rg.add_argument("--limit", type=int, default=30)
    rg.set_defaults(func=cmd_regions)

    sc = sub.add_parser("scan", help="index your own media folders")
    sc.add_argument("folders", nargs="*", help="folders to add (omit to rescan saved ones)")
    sc.add_argument("--forget", action="store_true", help="clear saved folders first")
    sc.set_defaults(func=cmd_scan)

    lb = sub.add_parser("local", help="list the files indexed from your folders")
    lb.add_argument("--limit", type=int, default=40)
    lb.add_argument("--unmatched", action="store_true", help="only unrecognised files")
    lb.set_defaults(func=cmd_local)

    sub.add_parser("gui", help="open the desktop app").set_defaults(func=cmd_gui)
    sub.add_parser("stats", help="library summary").set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
