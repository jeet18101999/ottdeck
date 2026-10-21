"""Offline smoke test: proves schema, FTS, fuzzy fallback and filters work
without needing a TMDB key."""
import sys, os, tempfile
sys.path.insert(0, "src")
os.environ["OTTDECK_DATA_DIR"] = tempfile.mkdtemp(prefix="ottdeck-test-")

from ottdeck import db, search as S
from ottdeck.links import build_targets

db.init_db()

SAMPLE = [
    dict(tmdb_id=157336, media_type="movie", title="Interstellar",
         original_title="Interstellar", overview="A team travels through a wormhole.",
         people="Matthew McConaughey, Anne Hathaway, Christopher Nolan",
         year=2014, runtime=169, vote_average=8.4, popularity=180.0,
         original_language="en", genres=["Adventure", "Drama", "Science Fiction"]),
    dict(tmdb_id=155, media_type="movie", title="The Dark Knight",
         original_title="The Dark Knight", overview="Batman faces the Joker.",
         people="Christian Bale, Heath Ledger, Christopher Nolan",
         year=2008, runtime=152, vote_average=8.5, popularity=160.0,
         original_language="en", genres=["Action", "Crime", "Drama"]),
    dict(tmdb_id=1396, media_type="tv", title="Breaking Bad",
         original_title="Breaking Bad", overview="A chemistry teacher turns to meth.",
         people="Bryan Cranston, Aaron Paul", year=2008, runtime=49,
         vote_average=8.9, popularity=300.0, original_language="en",
         genres=["Drama", "Crime"]),
    dict(tmdb_id=9999, media_type="movie", title="Vikram",
         original_title="Vikram", overview="A special agent investigates murders.",
         people="Kamal Haasan, Vijay Sethupathi, Lokesh Kanagaraj",
         year=2022, runtime=175, vote_average=8.2, popularity=90.0,
         original_language="ta", genres=["Action", "Thriller"]),
]

PROVIDERS = [
    dict(provider_id=8, name="Netflix", display_priority=1),
    dict(provider_id=119, name="Amazon Prime Video", display_priority=2),
    dict(provider_id=2336, name="JioHotstar", display_priority=3),
]

with db.session() as conn:
    for p in PROVIDERS:
        db.upsert_provider(conn, p)
    ids = {}
    for rec in SAMPLE:
        ids[rec["title"]] = db.upsert_title(conn, rec)
    db.replace_availability(conn, ids["Interstellar"], "IN",
        [dict(provider_id=119, offer_type="flatrate", link="https://x"),
         dict(provider_id=8, offer_type="rent", link="https://x")])
    db.replace_availability(conn, ids["The Dark Knight"], "IN",
        [dict(provider_id=8, offer_type="flatrate", link="https://x")])
    db.replace_availability(conn, ids["Breaking Bad"], "IN",
        [dict(provider_id=8, offer_type="flatrate", link="https://x")])
    db.replace_availability(conn, ids["Vikram"], "IN",
        [dict(provider_id=2336, offer_type="flatrate", link="https://x")])
    conn.execute("INSERT OR IGNORE INTO my_providers (provider_id) VALUES (8)")

failures = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")

with db.session() as conn:
    print("\n-- exact + prefix search (FTS5)")
    check("'inter' -> Interstellar",
          [r["title"] for r in S.search(conn, "inter")], ["Interstellar"])
    check("'dark knig' -> The Dark Knight",
          [r["title"] for r in S.search(conn, "dark knig")], ["The Dark Knight"])

    print("\n-- search by cast/director, not just title")
    check("'nolan' -> both Nolan films",
          sorted(r["title"] for r in S.search(conn, "nolan")),
          ["Interstellar", "The Dark Knight"])
    check("'kamal haasan' -> Vikram",
          [r["title"] for r in S.search(conn, "kamal haasan")], ["Vikram"])

    print("\n-- typo tolerance (rapidfuzz fallback)")
    r = S.search(conn, "intersteller")
    check("'intersteller' -> Interstellar", [x["title"] for x in r], ["Interstellar"])
    check("  flagged as fuzzy", r[0].get("fuzzy") if r else None, True)

    print("\n-- filters")
    check("type=tv", [r["title"] for r in S.search(conn, "", S.Filters(media_type="tv"))],
          ["Breaking Bad"])
    check("Tamil only",
          [r["title"] for r in S.search(conn, "", S.Filters(language="ta"))], ["Vikram"])
    check("runtime <= 160",
          sorted(r["title"] for r in S.search(conn, "", S.Filters(runtime_max=160))),
          ["Breaking Bad", "The Dark Knight"])
    check("genre=Crime",
          sorted(r["title"] for r in S.search(conn, "", S.Filters(genres=("Crime",)))),
          ["Breaking Bad", "The Dark Knight"])
    check("only my subscriptions (Netflix)",
          sorted(r["title"] for r in S.search(conn, "", S.Filters(only_my_providers=True))),
          ["Breaking Bad", "The Dark Knight"])
    check("Tamil + action, the combined query",
          [r["title"] for r in S.search(conn, "",
              S.Filters(language="ta", genres=("Action",), runtime_max=180))], ["Vikram"])

    print("\n-- availability + play targets")
    offers = S.offers_for(conn, ids["Interstellar"])
    check("Interstellar offers ordered (flatrate first)",
          [(o["name"], o["offer_type"]) for o in offers],
          [("Amazon Prime Video", "flatrate"), ("Netflix", "rent")])
    targets = build_targets("Interstellar", offers)
    check("play buttons", [t.label for t in targets],
          ["Amazon Prime Video - Included", "Netflix - Rent"])
    print(f"        url: {targets[0].url}")

    print("\n-- facets")
    f = S.facets(conn)
    check("provider facet counts",
          [(p["name"], p["n"]) for p in f["providers"]],
          [("Netflix", 3), ("Amazon Prime Video", 1), ("JioHotstar", 1)])

    print("\n-- provider normalisation (regression: KeyError 'name')")
    from ottdeck.sources.tmdb import extract_offers, normalise_provider
    # /watch/providers/{type} spells it `provider_name`; upsert_provider wants `name`.
    # These two shapes drifting apart is what broke every full sync.
    directory_entry = {"provider_id": 8, "provider_name": "Netflix",
                       "logo_path": "/x.jpg", "display_priority": 1}
    check("directory entry keeps its name",
          normalise_provider(directory_entry)["name"], "Netflix")
    db.upsert_provider(conn, normalise_provider(directory_entry))
    check("upsert accepts the directory shape", True, True)
    check("unnamed provider gets a fallback",
          normalise_provider({"provider_id": 999})["name"], "Provider 999")

    payload = {"watch/providers": {"results": {"IN": {"link": "https://jw",
        "flatrate": [{"provider_id": 119, "provider_name": "Amazon Prime Video",
                      "display_priority": 2}]}}}}
    offers, provs = extract_offers(payload, "IN")
    check("offer block normalises too", provs[0]["name"], "Amazon Prime Video")
    check("offer parsed",
          (offers[0]["provider_id"], offers[0]["offer_type"]), (119, "flatrate"))

    print("\n-- region coercion (regression: OTTDECK_REGION=+91)")
    from ottdeck.config import _clean_region
    check("+91 -> IN", _clean_region("+91")[0], "IN")
    check("+91 warns the user", bool(_clean_region("+91")[1]), True)
    check("IN passes through clean", _clean_region("IN"), ("IN", None))
    check("garbage falls back", _clean_region("XYZ")[0], "IN")

    print("\n-- stats")
    print("       ", db.stats(conn))

print("\n" + ("=" * 60))
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
