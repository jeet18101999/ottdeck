"""End-to-end: scan a folder, link it to the catalogue, drive the UI and player."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["OTTDECK_DATA_DIR"] = tempfile.mkdtemp(prefix="ottdeck-local-")

from ottdeck import db

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# ---- a small media tree -------------------------------------------------
root = Path(tempfile.mkdtemp(prefix="media-"))
(root / "Movies").mkdir()
(root / "Shows" / "Reacher" / "Season 1").mkdir(parents=True)
files = {
    "movie": root / "Movies" / "Interstellar.2014.1080p.BluRay.mkv",
    "ep1": root / "Shows" / "Reacher" / "Season 1" / "Reacher.S01E01.1080p.mkv",
    "ep2": root / "Shows" / "Reacher" / "Season 1" / "Reacher.S01E02.1080p.mkv",
}
for f in files.values():
    f.write_bytes(b"\x00" * 4096)

db.init_db()
with db.session() as conn:
    db.upsert_provider(conn, dict(provider_id=8, name="Netflix", display_priority=1))
    film = db.upsert_title(conn, dict(
        tmdb_id=157336, media_type="movie", title="Interstellar",
        original_title="Interstellar", overview="Wormhole.", people="Nolan",
        year=2014, runtime=169, vote_average=8.4, popularity=180.0,
        original_language="en", genres=["Science Fiction"]))
    show = db.upsert_title(conn, dict(
        tmdb_id=2734, media_type="tv", title="Reacher", original_title="Reacher",
        overview="Drifter.", people="Alan Ritchson", year=2022, runtime=50,
        vote_average=8.1, popularity=160.0, original_language="en",
        genres=["Crime"]))
    # Reacher streams in IN; Interstellar deliberately does not.
    db.replace_all_availability(conn, show, {
        "IN": [dict(provider_id=8, offer_type="flatrate", link="https://x")]})
    db.replace_all_availability(conn, film, {
        "US": [dict(provider_id=8, offer_type="flatrate", link="https://x")]})

from ottdeck.localscan import scan

with db.session() as conn:
    result = scan([root], conn)
check("scan found the files", result["files"], 3)
check("all linked to catalogue", result["matched"], 3)

# ---- drive the UI -------------------------------------------------------
from PySide6.QtWidgets import QApplication
from ottdeck.gui import MainWindow

app = QApplication([])
w = MainWindow()
w.show()
app.processEvents()

print("\n-- a film on disk but not streaming here")
w.search_box.setText("interstellar")
w.run_search()
app.processEvents()
check("found", w.detail.title.text(), "Interstellar")
check("section says it is local", w.detail.where_label.text(), "ON THIS PC")
btns = [w.detail.buttons.itemAt(i).widget().text()
        for i in range(w.detail.buttons.count())]
check("offers in-app playback", btns[0], "Play from your library")
check("external fallback offered", "Open in external player" in btns, True)

print("\n-- episodes are labelled individually")
w.search_box.setText("reacher")
w.run_search()
app.processEvents()
btns = [w.detail.buttons.itemAt(i).widget().text()
        for i in range(w.detail.buttons.count())]
check("one button per episode", [b for b in btns if b.startswith("Play  S")],
      ["Play  S01E01", "Play  S01E02"])

print("\n-- 'only what is on this PC' filter")
w.search_box.setText("")
w.local_check.setChecked(True)
app.processEvents()
check("only local titles", sorted(r["title"] for r in w.rows),
      ["Interstellar", "Reacher"])
w.local_check.setChecked(False)
app.processEvents()

print("\n-- 'not here, but elsewhere' still works")
w.search_box.setText("interstellar")
w.run_search()
app.processEvents()
with db.session() as conn:
    from ottdeck import search as S
    other = S.elsewhere(conn, film, "IN")
check("knows it streams in the US", [e["region"] for e in other], ["US"])

print("\n-- the in-app player opens and loads the file")
from ottdeck.player import PlayerWindow

w.play_in_app(str(files["movie"]), "Interstellar")
app.processEvents()
check("player window registered", len(w.players), 1)
player = w.players[0]
check("window titled", "Interstellar" in player.windowTitle(), True)
check("source points at the file",
      Path(player.player.source().toLocalFile()).name, files["movie"].name)
check("has video output", player.player.videoOutput() is not None, True)
check("has audio output", player.player.audioOutput() is not None, True)

print("\n-- closing the player releases it")
player.close()
app.processEvents()
check("unregistered on close", len(w.players), 0)

print("\n-- media folders are remembered")
w._save_media_roots([str(root)])
check("root persisted", w._media_roots(), [str(root)])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All local-media checks passed.")
