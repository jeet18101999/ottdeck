"""Offline test for filename parsing and local-library matching."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
os.environ["OTTDECK_DATA_DIR"] = tempfile.mkdtemp(prefix="ottdeck-scan-")

from ottdeck import db
from ottdeck.localscan import parse_filename, scan, unmatched_titles, walk_media

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


print("-- movie filenames")
CASES = [
    ("Interstellar.2014.1080p.BluRay.x264-SPARKS.mkv", "Interstellar", 2014),
    ("The Dark Knight (2008) [1080p] [BluRay].mp4", "The Dark Knight", 2008),
    ("Vikram.2022.1080p.AMZN.WEB-DL.Tamil.DDP5.1.x264.mkv", "Vikram", 2022),
    ("Dune_Part_Two_2024_2160p_HDR_x265.mkv", "Dune Part Two", 2024),
    ("3 Idiots 2009 Hindi 720p BluRay.mkv", "3 Idiots", 2009),
    ("Spirited.Away.2001.JAPANESE.1080p.BluRay.mkv", "Spirited Away", 2001),
    ("Oppenheimer.mkv", "Oppenheimer", None),
]
for name, want_title, want_year in CASES:
    p = parse_filename(name)
    check(f"{name[:42]:<42}", (p.kind, p.title, p.year), ("movie", want_title, want_year))

print("\n-- episode filenames")
TV_CASES = [
    ("Breaking.Bad.S01E07.1080p.BluRay.x265.mkv", "Breaking Bad", 1, 7),
    ("Reacher.S02E03.WEBRip.x264-ION10.mp4", "Reacher", 2, 3),
    ("The Office (US) - 3x12 - Back from Vacation.avi", "The Office", 3, 12),
    ("Sacred.Games.Season.1.Episode.4.1080p.mkv", "Sacred Games", 1, 4),
    ("Dark.s02e05.GERMAN.1080p.WEB.h264.mkv", "Dark", 2, 5),
]
for name, want_title, want_s, want_e in TV_CASES:
    p = parse_filename(name)
    check(f"{name[:42]:<42}", (p.kind, p.title, p.season, p.episode),
          ("tv", want_title, want_s, want_e))

print("\n-- season folder supplies the season number")
p = parse_filename(Path("Mirzapur") / "Season 2" / "Episode 05.mkv")
check("Mirzapur/Season 2/Episode 05.mkv",
      (p.kind, p.title, p.season, p.episode), ("tv", "Mirzapur", 2, 5))

print("\n-- dotted initialisms survive")
p = parse_filename("S.W.A.T.2017.S01E01.1080p.mkv")
check("S.W.A.T. keeps its letters", (p.kind, p.title, p.year, p.season, p.episode),
      ("tv", "SWAT", 2017, 1, 1))

print("\n-- walking a real folder tree")
root = Path(tempfile.mkdtemp(prefix="media-"))
(root / "Movies").mkdir()
(root / "Shows" / "Reacher" / "Season 1").mkdir(parents=True)
made = [
    root / "Movies" / "Interstellar.2014.1080p.BluRay.mkv",
    root / "Movies" / "Vikram.2022.WEB-DL.mkv",
    root / "Shows" / "Reacher" / "Season 1" / "Reacher.S01E01.1080p.mkv",
    root / "Shows" / "Reacher" / "Season 1" / "Reacher.S01E02.1080p.mkv",
    root / "Movies" / "notes.txt",
    root / "Movies" / "cover.jpg",
]
for f in made:
    f.write_bytes(b"x" * 2048)
found = sorted(p.name for p in walk_media([root]))
check("only video files picked up", found,
      ["Interstellar.2014.1080p.BluRay.mkv", "Reacher.S01E01.1080p.mkv",
       "Reacher.S01E02.1080p.mkv", "Vikram.2022.WEB-DL.mkv"])

print("\n-- matching against the catalogue")
db.init_db()
with db.session() as conn:
    ids = {}
    for tmdb_id, mt, title, year in [
        (157336, "movie", "Interstellar", 2014),
        (9999, "movie", "Vikram", 2022),
        (2734, "tv", "Reacher", 2022),
        (155, "movie", "The Dark Knight", 2008),
    ]:
        ids[title] = db.upsert_title(conn, dict(
            tmdb_id=tmdb_id, media_type=mt, title=title, original_title=title,
            overview="x", people="", year=year, runtime=120, vote_average=8.0,
            popularity=50.0, original_language="en", genres=["Drama"]))

    result = scan([root], conn)
    check("files scanned", result["files"], 4)
    check("all matched to catalogue", result["matched"], 4)
    check("none left over", result["unmatched"], 0)

    rows = conn.execute(
        "SELECT lm.season, lm.episode, t.title FROM local_media lm "
        "JOIN titles t ON t.id = lm.title_id ORDER BY t.title, lm.episode"
    ).fetchall()
    check("linked correctly",
          [(r["title"], r["season"], r["episode"]) for r in rows],
          [("Interstellar", None, None), ("Reacher", 1, 1),
           ("Reacher", 1, 2), ("Vikram", None, None)])

print("\n-- an unknown film is reported, not silently dropped")
(root / "Movies" / "Some.Obscure.Film.1997.DVDRip.mkv").write_bytes(b"x" * 1024)
with db.session() as conn:
    result = scan([root], conn)
    check("now one unmatched", result["unmatched"], 1)
    gaps = unmatched_titles(conn)
    check("gap is named", [(g["guess_title"], g["guess_year"]) for g in gaps],
          [("Some Obscure Film", 1997)])

print("\n-- rescanning is idempotent")
with db.session() as conn:
    again = scan([root], conn)
    total = conn.execute("SELECT count(*) FROM local_media").fetchone()[0]
    check("no duplicate rows", total, again["files"])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All scan checks passed.")
