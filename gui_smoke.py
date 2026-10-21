"""Headless check that the Qt UI builds, populates and reacts to input."""
import os, sys, tempfile
sys.path.insert(0, "src")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["OTTDECK_DATA_DIR"] = tempfile.mkdtemp(prefix="ottdeck-gui-")

from ottdeck import db
db.init_db()
with db.session() as conn:
    db.upsert_provider(conn, dict(provider_id=8, name="Netflix", display_priority=1))
    db.upsert_provider(conn, dict(provider_id=2336, name="JioHotstar", display_priority=3))
    a = db.upsert_title(conn, dict(tmdb_id=155, media_type="movie", title="The Dark Knight",
        original_title="The Dark Knight", overview="Batman faces the Joker.",
        people="Christian Bale, Christopher Nolan", year=2008, runtime=152,
        vote_average=8.5, popularity=160.0, original_language="en",
        genres=["Action", "Crime"]))
    b = db.upsert_title(conn, dict(tmdb_id=9999, media_type="movie", title="Vikram",
        original_title="Vikram", overview="A special agent investigates.",
        people="Kamal Haasan", year=2022, runtime=175, vote_average=8.2,
        popularity=90.0, original_language="ta", genres=["Action", "Thriller"]))
    db.replace_availability(conn, a, "IN", [dict(provider_id=8, offer_type="flatrate", link="https://x")])
    db.replace_availability(conn, b, "IN", [dict(provider_id=2336, offer_type="flatrate", link="https://x")])

from PySide6.QtWidgets import QApplication
from ottdeck.gui import MainWindow

app = QApplication([])
w = MainWindow()
w.show()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok: fails.append(label)

print("\n-- window boots")
check("grid populated on launch", w.grid.count(), 2)
check("result label", w.result_label.text(), "2 of 2 titles")
check("detail auto-selected", w.detail.title.text(), "Breaking" if False else "The Dark Knight")

print("\n-- detail panel content")
check("meta line", "2008" in w.detail.meta.text() and "152 min" in w.detail.meta.text(), True)
btns = [w.detail.buttons.itemAt(i).widget().text()
        for i in range(w.detail.buttons.count())]
check("play buttons", btns, ["Netflix - Included"])

print("\n-- typing filters live (debounce bypassed)")
w.search_box.setText("vikram")
w.run_search()
check("search 'vikram'", [r["title"] for r in w.rows], ["Vikram"])
check("detail followed selection", w.detail.title.text(), "Vikram")

w.search_box.setText("vikrma")   # transposed
w.run_search()
check("typo 'vikrma' still finds it", [r["title"] for r in w.rows], ["Vikram"])
check("flagged as close match", "close match" in w.result_label.text(), True)

print("\n-- filter widgets")
w.search_box.setText("")
w.lang_combo.setCurrentIndex(w.lang_combo.findData("ta"))
check("language filter -> Tamil only", [r["title"] for r in w.rows], ["Vikram"])
w.reset_filters()
check("reset restores all", w.grid.count(), 2)

w.runtime.setValue(160); w.run_search()
check("runtime under 160", [r["title"] for r in w.rows], ["The Dark Knight"])
w.reset_filters()

print("\n-- facets loaded into combos")
check("genre combo has entries", w.genre_combo.count() > 1, True)
check("provider combo has services", w.provider_combo.count() > 1, True)

print("\n" + "="*60)
if fails:
    print(f"{len(fails)} FAILURE(S): {fails}")
    sys.exit(1)
print("GUI checks passed.")
