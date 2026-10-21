"""Native desktop UI (Qt / PySide6).

No browser, no embedded web view. Search runs against local SQLite so results
appear as you type; only catalogue sync touches the network, and that happens
on a worker thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from . import db, ingest, links, localscan
from . import search as search_mod
from .config import POSTER_CACHE, REGION, has_credentials, poster_url
from .player import PlayerWindow
from .regions import country_label

POSTER_W, POSTER_H = 154, 231

STYLE = """
QWidget          { background: #14161a; color: #e6e8eb;
                   font-family: 'Segoe UI', sans-serif; font-size: 13px; }
QLineEdit        { background: #1e2127; border: 1px solid #2c313a; border-radius: 6px;
                   padding: 9px 12px; font-size: 15px; selection-background-color: #3b82f6; }
QLineEdit:focus  { border-color: #3b82f6; }
QComboBox        { background: #1e2127; border: 1px solid #2c313a; border-radius: 5px;
                   padding: 5px 8px; }
QComboBox QAbstractItemView { background: #1e2127; selection-background-color: #3b82f6; }
QPushButton      { background: #262b33; border: 1px solid #343b45; border-radius: 5px;
                   padding: 7px 13px; }
QPushButton:hover    { background: #2f353f; border-color: #3b82f6; }
QPushButton#play     { background: #2563eb; border: none; font-weight: 600; padding: 10px; }
QPushButton#play:hover { background: #3b82f6; }
QListWidget      { background: #14161a; border: none; outline: none; }
QListWidget::item          { color: #aeb4bd; padding: 6px; border-radius: 8px; }
QListWidget::item:selected { background: #1e2127; color: #ffffff; }
QListWidget::item:hover    { background: #1a1d23; }
QLabel#h1        { font-size: 21px; font-weight: 600; color: #ffffff; }
QLabel#meta      { color: #8b929c; }
QLabel#section   { color: #6b7280; font-size: 11px; font-weight: 700;
                   letter-spacing: 1px; }
QScrollArea      { border: none; }
QFrame#card      { background: #1a1d23; border: 1px solid #252a32; border-radius: 10px; }
QSlider::groove:horizontal { background: #2c313a; height: 4px; border-radius: 2px; }
QSlider::handle:horizontal { background: #3b82f6; width: 14px; margin: -5px 0;
                             border-radius: 7px; }
QProgressBar     { background: #1e2127; border: none; border-radius: 3px; height: 6px;
                   text-align: center; }
QProgressBar::chunk { background: #3b82f6; border-radius: 3px; }
"""


# ---------------------------------------------------------------- posters


class PosterLoader(QObject):
    """Fetches poster art once, then serves it from disk."""

    loaded = Signal(str, QPixmap)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._on_finished)
        self._inflight: set[str] = set()
        self._memory: dict[str, QPixmap] = {}

    def _cache_path(self, url: str) -> Path:
        return POSTER_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")

    def get(self, url: str | None) -> QPixmap | None:
        if not url:
            return None
        if url in self._memory:
            return self._memory[url]

        path = self._cache_path(url)
        if path.exists():
            pix = QPixmap(str(path))
            if not pix.isNull():
                self._memory[url] = pix
                return pix

        if url not in self._inflight:
            self._inflight.add(url)
            req = QNetworkRequest(QUrl(url))
            req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, True)
            self._net.get(req)
        return None

    def _on_finished(self, reply: QNetworkReply) -> None:
        url = reply.request().url().toString()
        self._inflight.discard(url)
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = reply.readAll()
            pix = QPixmap()
            if pix.loadFromData(data):
                self._memory[url] = pix
                try:
                    self._cache_path(url).write_bytes(bytes(data))
                except OSError:
                    pass
                self.loaded.emit(url, pix)
        reply.deleteLater()


def placeholder_poster(title: str) -> QPixmap:
    pix = QPixmap(POSTER_W, POSTER_H)
    pix.fill(Qt.GlobalColor.transparent)
    from PySide6.QtGui import QColor, QPainter

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#1e2127"))
    painter.setPen(QColor("#2c313a"))
    painter.drawRoundedRect(0, 0, POSTER_W - 1, POSTER_H - 1, 8, 8)
    painter.setPen(QColor("#4b5563"))
    painter.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, (title or "?")[:1].upper())
    painter.end()
    return pix


# ------------------------------------------------------------- sync thread


class SyncWorker(QThread):
    """Catalogue refresh. Network + asyncio, kept off the UI thread."""

    progress = Signal(str, int, int)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, pages: int = 5, query: str | None = None) -> None:
        super().__init__()
        self.pages = pages
        self.query = query

    def run(self) -> None:
        try:
            if self.query:
                n = asyncio.run(ingest.ingest_search(self.query))
                self.finished_ok.emit({"written": n, "query": self.query})
            else:
                result = asyncio.run(
                    ingest.refresh_catalogue(
                        pages=self.pages,
                        progress=lambda s, d, t: self.progress.emit(s, d, t),
                    )
                )
                self.finished_ok.emit(result)
        except Exception as exc:  # surfaced in the status bar, never a hard crash
            self.failed.emit(str(exc))


class ScanWorker(QThread):
    """Folder scan. Disk-bound, so it stays off the UI thread too."""

    progress = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, roots: list[str]) -> None:
        super().__init__()
        self.roots = roots

    def run(self) -> None:
        try:
            with db.session() as conn:
                result = localscan.scan(
                    self.roots,
                    conn,
                    progress=lambda i, t, name: self.progress.emit(i, t, name),
                )
            self.finished_ok.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


# ------------------------------------------------------------ detail panel


def _local_label(rec: dict) -> str:
    """`Play S01E02` for an episode, plain `Play` for a film."""
    if rec["season"] is not None and rec["episode"] is not None:
        return f"Play  S{rec['season']:02d}E{rec['episode']:02d}"
    return "Play from your library"


class DetailPanel(QScrollArea):
    """Everything about one title, including where to watch it."""

    play_here = Signal(str, str)   # (path, title)

    def __init__(self, posters: PosterLoader) -> None:
        super().__init__()
        self.posters = posters
        self.setWidgetResizable(True)
        self._row: dict | None = None
        self._poster_url: str | None = None
        self.region = REGION

        body = QWidget()
        self.box = QVBoxLayout(body)
        self.box.setContentsMargins(20, 20, 20, 20)
        self.box.setSpacing(12)

        self.art = QLabel()
        self.art.setFixedSize(POSTER_W * 2, POSTER_H * 2)
        self.art.setScaledContents(True)

        self.title = QLabel()
        self.title.setObjectName("h1")
        self.title.setWordWrap(True)

        self.meta = QLabel()
        self.meta.setObjectName("meta")
        self.meta.setWordWrap(True)

        self.overview = QLabel()
        self.overview.setWordWrap(True)
        self.overview.setStyleSheet("color: #c3c8cf; line-height: 150%;")

        self.where_label = QLabel("WHERE TO WATCH")
        self.where_label.setObjectName("section")

        self.buttons = QVBoxLayout()
        self.buttons.setSpacing(7)

        for w in (self.art, self.title, self.meta, self.overview, self.where_label):
            self.box.addWidget(w)
        self.box.addLayout(self.buttons)
        self.box.addStretch(1)

        self.setWidget(body)
        self.posters.loaded.connect(self._poster_arrived)
        self.clear()

    def clear(self) -> None:
        self._row = None
        self.art.setPixmap(placeholder_poster(""))
        self.title.setText("Select a title")
        self.meta.setText("")
        self.overview.setText("")
        self.where_label.hide()
        self._clear_buttons()

    def _clear_buttons(self) -> None:
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def show_title(self, row: dict) -> None:
        self._row = row
        self.title.setText(row["title"] or "")

        bits = []
        if row.get("year"):
            bits.append(str(row["year"]))
        bits.append("Series" if row["media_type"] == "tv" else "Film")
        if row.get("runtime"):
            bits.append(f"{row['runtime']} min")
        if row.get("vote_average"):
            bits.append(f"{row['vote_average']:.1f}/10")
        if row.get("original_language"):
            bits.append(row["original_language"].upper())
        if row.get("genres"):
            bits.append(" / ".join(row["genres"][:3]))
        self.meta.setText("   ".join(bits))
        self.overview.setText(row.get("overview") or "")

        self._poster_url = poster_url(row.get("poster_path"), "w500")
        pix = self.posters.get(self._poster_url)
        self.art.setPixmap(pix or placeholder_poster(row["title"]))

        self._clear_buttons()
        with db.session() as conn:
            offers = search_mod.offers_for(conn, row["id"], self.region)
            local = conn.execute(
                "SELECT path, season, episode FROM local_media WHERE title_id = ? "
                "ORDER BY season, episode, path",
                (row["id"],),
            ).fetchall()

        self.where_label.setText(
            "ON THIS PC" if local else f"WHERE TO WATCH  -  {country_label(self.region)}"
        )
        self.where_label.show()

        for rec in local:
            btn = QPushButton(_local_label(rec))
            btn.setObjectName("play")
            btn.setToolTip(rec["path"])
            btn.clicked.connect(lambda _, p=rec["path"], t=row["title"]: self.play_here.emit(p, t))
            self.buttons.addWidget(btn)

        if local:
            alt = QPushButton("Open in external player")
            alt.setToolTip(links.find_external_player() or "No VLC or mpv found")
            alt.clicked.connect(lambda _, p=local[0]["path"]: links.play_local(p))
            self.buttons.addWidget(alt)

        targets = links.build_targets(row["title"], offers)
        if not targets and not local:
            note = QLabel(f"Not streaming in {country_label(self.region)}.")
            note.setObjectName("meta")
            self.buttons.addWidget(note)

            # Every region is stored, so answer the obvious follow-up:
            # if not here, then where?
            with db.session() as conn:
                other = search_mod.elsewhere(conn, row["id"], self.region)
            if other:
                head = QLabel("AVAILABLE ELSEWHERE")
                head.setObjectName("section")
                head.setContentsMargins(0, 10, 0, 2)
                self.buttons.addWidget(head)
                for entry in other:
                    line = QLabel(
                        f"{country_label(entry['region'])}  -  "
                        f"{', '.join(entry['providers'][:3])}"
                    )
                    line.setWordWrap(True)
                    line.setStyleSheet("color: #9aa1ab;")
                    self.buttons.addWidget(line)
                hint = QLabel(
                    "Switch country in the sidebar to browse that catalogue."
                )
                hint.setObjectName("meta")
                hint.setWordWrap(True)
                hint.setContentsMargins(0, 6, 0, 0)
                self.buttons.addWidget(hint)

        for i, t in enumerate(targets):
            btn = QPushButton(t.label)
            if i == 0 and not local:
                btn.setObjectName("play")
            btn.clicked.connect(lambda _, tgt=t: links.open_target(tgt))
            self.buttons.addWidget(btn)

    def _poster_arrived(self, url: str, pix: QPixmap) -> None:
        if url == self._poster_url:
            self.art.setPixmap(pix)


# --------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OttDeck")
        self.resize(1440, 900)
        self.posters = PosterLoader(self)
        self.rows: list[dict] = []
        self.worker: SyncWorker | None = None
        self.scanner: ScanWorker | None = None
        self.players: list[PlayerWindow] = []
        self._gap_queue: list[str] = []
        self._pending_rematch = False
        self.region = REGION

        db.init_db()

        # Typing fires a search 180 ms after the last keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self.run_search)

        self._build_ui()
        self.posters.loaded.connect(self._poster_arrived)
        self.load_facets()
        self.run_search()

        if not has_credentials():
            self.status.showMessage(
                "No TMDB key found - add TMDB_API_KEY to .env, then use Library > Sync", 0
            )

    # -- layout ----------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(STYLE)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "Search titles, actors, directors...   (try a misspelling)"
        )
        self.search_box.textChanged.connect(lambda _: self._debounce.start())
        self.search_box.returnPressed.connect(self.run_search)

        self.fetch_btn = QPushButton("Fetch from TMDB")
        self.fetch_btn.setToolTip("Not in your library yet? Pull it in from TMDB.")
        self.fetch_btn.clicked.connect(self.fetch_current_query)

        top = QHBoxLayout()
        top.setContentsMargins(16, 14, 16, 8)
        top.setSpacing(10)
        top.addWidget(self.search_box, 1)
        top.addWidget(self.fetch_btn)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._filter_panel())
        splitter.addWidget(self._grid_panel())
        self.detail = DetailPanel(self.posters)
        self.detail.play_here.connect(self.play_in_app)
        splitter.addWidget(self.detail)
        splitter.setSizes([230, 800, 410])
        splitter.setStretchFactor(1, 1)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        self.status = QStatusBar()
        self.status.addPermanentWidget(self.progress)
        self.setStatusBar(self.status)

        self._build_menu()

    def _filter_panel(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 12, 12, 12)
        box.setSpacing(9)

        def section(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setObjectName("section")
            return lbl

        self.region_combo = QComboBox()
        self.region_combo.setToolTip(
            "Browse another country's catalogue.\n"
            "This changes what the app shows, not what your account can play."
        )

        self.type_combo = QComboBox()
        self.type_combo.addItem("Everything", None)
        self.type_combo.addItem("Films", "movie")
        self.type_combo.addItem("Series", "tv")

        self.genre_combo = QComboBox()
        self.lang_combo = QComboBox()
        self.provider_combo = QComboBox()

        self.local_check = QCheckBox("Only what is on this PC")
        self.local_check.setToolTip("Files indexed from your own folders.")

        self.mine_check = QCheckBox("Only what I subscribe to")
        self.mine_check.setToolTip(
            "Titles included with a service you pay for - rentals excluded."
        )

        self.runtime = QSlider(Qt.Orientation.Horizontal)
        self.runtime.setRange(0, 240)
        self.runtime.setValue(0)
        self.runtime_label = QLabel("Any length")
        self.runtime_label.setObjectName("meta")
        self.runtime.valueChanged.connect(self._runtime_changed)

        self.rating = QSlider(Qt.Orientation.Horizontal)
        self.rating.setRange(0, 90)
        self.rating.setValue(0)
        self.rating_label = QLabel("Any rating")
        self.rating_label.setObjectName("meta")
        self.rating.valueChanged.connect(self._rating_changed)

        reset = QPushButton("Reset filters")
        reset.clicked.connect(self.reset_filters)

        box.addWidget(section("COUNTRY"))
        box.addWidget(self.region_combo)
        box.addSpacing(6)
        box.addWidget(section("TYPE"))
        box.addWidget(self.type_combo)
        box.addWidget(section("GENRE"))
        box.addWidget(self.genre_combo)
        box.addWidget(section("LANGUAGE"))
        box.addWidget(self.lang_combo)
        box.addWidget(section("SERVICE"))
        box.addWidget(self.provider_combo)
        box.addWidget(self.mine_check)
        box.addWidget(self.local_check)
        box.addSpacing(6)
        box.addWidget(section("MAX RUNTIME"))
        box.addWidget(self.runtime)
        box.addWidget(self.runtime_label)
        box.addSpacing(6)
        box.addWidget(section("MIN RATING"))
        box.addWidget(self.rating)
        box.addWidget(self.rating_label)
        box.addStretch(1)
        box.addWidget(reset)

        for combo in (self.type_combo, self.genre_combo, self.lang_combo, self.provider_combo):
            combo.currentIndexChanged.connect(self.run_search)
        self.region_combo.currentIndexChanged.connect(self._region_changed)
        self.mine_check.toggled.connect(self.run_search)
        self.local_check.toggled.connect(self.run_search)
        self.runtime.sliderReleased.connect(self.run_search)
        self.rating.sliderReleased.connect(self.run_search)

        panel.setMinimumWidth(200)
        return panel

    def _grid_panel(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(6, 4, 6, 6)
        box.setSpacing(4)

        self.result_label = QLabel()
        self.result_label.setObjectName("meta")
        self.result_label.setContentsMargins(10, 0, 0, 4)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(QSize(POSTER_W, POSTER_H))
        self.grid.setGridSize(QSize(POSTER_W + 26, POSTER_H + 62))
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.currentItemChanged.connect(self._selection_changed)
        self.grid.itemActivated.connect(self._activate)

        box.addWidget(self.result_label)
        box.addWidget(self.grid, 1)
        return panel

    def _build_menu(self) -> None:
        library = self.menuBar().addMenu("&Library")

        sync = QAction("&Sync catalogue", self)
        sync.setShortcut("Ctrl+R")
        sync.triggered.connect(self.sync_catalogue)
        library.addAction(sync)

        focus = QAction("&Find", self)
        focus.setShortcut("Ctrl+F")
        focus.triggered.connect(self.search_box.setFocus)
        library.addAction(focus)

        library.addSeparator()

        add_folder = QAction("&Add media folder...", self)
        add_folder.setShortcut("Ctrl+O")
        add_folder.triggered.connect(self.add_media_folder)
        library.addAction(add_folder)

        rescan = QAction("Re&scan my folders", self)
        rescan.triggered.connect(self.rescan_folders)
        library.addAction(rescan)

        fill_gaps = QAction("Look up &unmatched files on TMDB", self)
        fill_gaps.triggered.connect(self.match_unmatched)
        library.addAction(fill_gaps)

        library.addSeparator()
        stats = QAction("Library &stats", self)
        stats.triggered.connect(self.show_stats)
        library.addAction(stats)

    # -- filters ---------------------------------------------------------

    def _runtime_changed(self, value: int) -> None:
        self.runtime_label.setText("Any length" if value == 0 else f"Under {value} min")

    def _rating_changed(self, value: int) -> None:
        self.rating_label.setText(
            "Any rating" if value == 0 else f"{value / 10:.1f}+ out of 10"
        )

    def _region_changed(self) -> None:
        """Switching country is a local filter - no network, no re-sync."""
        code = self.region_combo.currentData()
        if not code or code == self.region:
            return
        self.region = code
        self.detail.region = code
        # Provider and genre counts are per-country, so rebuild them too.
        self.load_facets()
        self.run_search()
        self.status.showMessage(f"Showing what streams in {country_label(code)}", 4000)

    def reset_filters(self) -> None:
        # The country is a viewing context, not a filter - leave it alone.
        for combo in (self.type_combo, self.genre_combo, self.lang_combo, self.provider_combo):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.mine_check.setChecked(False)
        self.local_check.setChecked(False)
        self.runtime.setValue(0)
        self.rating.setValue(0)
        self.run_search()

    def load_facets(self) -> None:
        with db.session() as conn:
            f = search_mod.facets(conn, self.region)
            available = db.known_regions(conn)

        def refill(combo: QComboBox, first: str, items: list[tuple[str, object]]) -> None:
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first, None)
            for label, data in items:
                combo.addItem(label, data)
            idx = combo.findData(current)
            combo.setCurrentIndex(max(idx, 0))
            combo.blockSignals(False)

        # Only offer countries the library actually holds data for.
        if available:
            current = self.region_combo.currentData() or self.region
            self.region_combo.blockSignals(True)
            self.region_combo.clear()
            for code, n in available:
                self.region_combo.addItem(f"{country_label(code)}  ({n})", code)
            idx = self.region_combo.findData(current)
            if idx < 0:
                idx = self.region_combo.findData(REGION)
            self.region_combo.setCurrentIndex(max(idx, 0))
            self.region = self.region_combo.currentData() or REGION
            self.detail.region = self.region
            self.region_combo.blockSignals(False)

        refill(self.genre_combo, "All genres",
               [(f"{g}  ({n})", g) for g, n in f["genres"][:30]])
        refill(self.lang_combo, "All languages",
               [(f"{r['code'].upper()}  ({r['n']})", r["code"]) for r in f["languages"]])
        refill(self.provider_combo, "All services",
               [(f"{r['name']}  ({r['n']})", r["provider_id"]) for r in f["providers"][:40]])

    def current_filters(self) -> search_mod.Filters:
        genre = self.genre_combo.currentData()
        provider = self.provider_combo.currentData()
        return search_mod.Filters(
            region=self.region,
            media_type=self.type_combo.currentData(),
            genres=(genre,) if genre else (),
            runtime_max=self.runtime.value() or None,
            language=self.lang_combo.currentData(),
            provider_ids=(provider,) if provider else (),
            only_my_providers=self.mine_check.isChecked(),
            only_local=self.local_check.isChecked(),
            min_rating=(self.rating.value() / 10) or None,
        )

    # -- search ----------------------------------------------------------

    def run_search(self) -> None:
        query = self.search_box.text().strip()
        with db.session() as conn:
            self.rows = search_mod.search(conn, query, self.current_filters(), limit=200)
            total = db.stats(conn)["titles"]

        self.grid.clear()
        for row in self.rows:
            year = f"  ({row['year']})" if row.get("year") else ""
            item = QListWidgetItem(f"{row['title']}{year}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            url = poster_url(row.get("poster_path"))
            item.setData(Qt.ItemDataRole.UserRole + 1, url)
            pix = self.posters.get(url)
            item.setIcon(QIcon(pix or placeholder_poster(row["title"])))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            item.setToolTip(row.get("overview") or row["title"])
            self.grid.addItem(item)

        if total == 0:
            self.result_label.setText(
                "Library is empty - use Library > Sync catalogue (Ctrl+R) to fill it."
            )
        elif not self.rows:
            self.result_label.setText(
                f"No match in {total} titles. Try 'Fetch from TMDB' to pull it in."
            )
        else:
            fuzzy = sum(1 for r in self.rows if r.get("fuzzy"))
            note = f"  ({fuzzy} close match{'es' if fuzzy != 1 else ''})" if fuzzy else ""
            self.result_label.setText(f"{len(self.rows)} of {total} titles{note}")

        if self.rows:
            self.grid.setCurrentRow(0)
        else:
            self.detail.clear()

    def _selection_changed(self, current: QListWidgetItem | None, _prev) -> None:
        if current is None:
            return
        tid = current.data(Qt.ItemDataRole.UserRole)
        row = next((r for r in self.rows if r["id"] == tid), None)
        if row:
            self.detail.show_title(row)

    def _activate(self, item: QListWidgetItem) -> None:
        """Enter / double-click opens the best place to watch."""
        tid = item.data(Qt.ItemDataRole.UserRole)
        row = next((r for r in self.rows if r["id"] == tid), None)
        if not row:
            return
        with db.session() as conn:
            offers = search_mod.offers_for(conn, row["id"], self.region)
        targets = links.build_targets(row["title"], offers)
        if targets:
            links.open_target(targets[0])
            self.status.showMessage(f"Opening {targets[0].label}", 4000)
        else:
            self.status.showMessage(
                f"{row['title']} is not streaming in {country_label(self.region)}", 4000)

    def _poster_arrived(self, url: str, pix: QPixmap) -> None:
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            if item.data(Qt.ItemDataRole.UserRole + 1) == url:
                item.setIcon(QIcon(pix))

    # -- network ---------------------------------------------------------

    def _start_worker(self, worker: SyncWorker, message: str) -> None:
        if self.worker and self.worker.isRunning():
            self.status.showMessage("A sync is already running", 3000)
            return
        if not has_credentials():
            QMessageBox.warning(
                self, "No TMDB key",
                "Add TMDB_API_KEY to a .env file in the project folder.\n\n"
                "Free key: https://www.themoviedb.org/settings/api",
            )
            return

        self.worker = worker
        worker.progress.connect(self._sync_progress)
        worker.finished_ok.connect(self._sync_done)
        worker.failed.connect(self._sync_failed)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.showMessage(message, 0)
        worker.start()

    def sync_catalogue(self) -> None:
        self._start_worker(SyncWorker(pages=5), "Syncing catalogue...")

    def fetch_current_query(self) -> None:
        query = self.search_box.text().strip()
        if not query:
            self.status.showMessage("Type something to fetch first", 3000)
            return
        self._start_worker(SyncWorker(query=query), f"Fetching '{query}' from TMDB...")

    def _sync_progress(self, stage: str, done: int, total: int) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.showMessage(f"{stage}: {done}/{total}", 0)

    def _sync_done(self, result: dict) -> None:
        self.progress.hide()
        # A lookup kicked off by unrecognised files: work through the queue,
        # then re-scan so the new catalogue entries get linked to the files.
        if self._pending_rematch:
            if self._gap_queue:
                nxt = self._gap_queue.pop(0)
                self._start_worker(SyncWorker(query=nxt), f"Looking up {nxt!r}...")
                return
            self._pending_rematch = False
            roots = self._media_roots()
            if roots:
                self._start_scan(roots)
                return
        written = result.get("written", 0)
        warnings = result.get("warnings") or []
        self.status.showMessage(f"Done - {written} titles added or refreshed", 6000)
        self.load_facets()
        self.run_search()
        # A sync that stores titles but no availability is the failure mode that
        # looks like success, so say so rather than leaving an empty catalogue.
        if warnings:
            QMessageBox.warning(self, "Sync finished with warnings", "\n\n".join(warnings))

    def _sync_failed(self, message: str) -> None:
        self.progress.hide()
        self.status.showMessage(f"Sync failed: {message}", 10000)

    # -- local media -----------------------------------------------------

    def _media_roots(self) -> list[str]:
        with db.session() as conn:
            raw = db.get_meta(conn, "media_roots", "")
        return [r for r in (raw or "").split("|") if r]

    def _save_media_roots(self, roots: list[str]) -> None:
        with db.session() as conn:
            db.set_meta(conn, "media_roots", "|".join(dict.fromkeys(roots)))

    def add_media_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder holding your films or series"
        )
        if not folder:
            return
        roots = self._media_roots()
        if folder not in roots:
            roots.append(folder)
            self._save_media_roots(roots)
        self._start_scan([folder])

    def rescan_folders(self) -> None:
        roots = self._media_roots()
        if not roots:
            self.status.showMessage(
                "No media folders yet - use Library > Add media folder", 5000
            )
            return
        self._start_scan(roots)

    def _start_scan(self, roots: list[str]) -> None:
        if self.scanner and self.scanner.isRunning():
            self.status.showMessage("A scan is already running", 3000)
            return
        self.scanner = ScanWorker(roots)
        self.scanner.progress.connect(self._scan_progress)
        self.scanner.finished_ok.connect(self._scan_done)
        self.scanner.failed.connect(
            lambda m: self.status.showMessage("Scan failed: " + m, 10000)
        )
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.showMessage("Scanning...", 0)
        self.scanner.start()

    def _scan_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.showMessage(f"Scanning {done}/{total}: {name}", 0)

    def _scan_done(self, result: dict) -> None:
        self.progress.hide()
        self.run_search()
        self.status.showMessage(
            f"Indexed {result['files']} files - {result['matched']} matched, "
            f"{result['unmatched']} unrecognised",
            8000,
        )
        if result["unmatched"]:
            answer = QMessageBox.question(
                self,
                "Unrecognised files",
                f"{result['unmatched']} file(s) did not match anything in the "
                "catalogue.\n\nLook them up on TMDB now?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.match_unmatched()

    def match_unmatched(self) -> None:
        """Fetch the guessed titles from TMDB, then re-link the files."""
        with db.session() as conn:
            gaps = localscan.unmatched_titles(conn, limit=25)
        if not gaps:
            self.status.showMessage("Every local file is already matched", 4000)
            return
        self._pending_rematch = True
        self._gap_queue = [g["guess_title"] for g in gaps[1:]]
        first = gaps[0]["guess_title"]
        self._start_worker(SyncWorker(query=first), f"Looking up {first!r}...")

    def play_in_app(self, path: str, title: str) -> None:
        """Open the built-in player. Qt's ffmpeg backend handles most formats."""
        if not Path(path).exists():
            self.status.showMessage(f"File is missing: {path}", 8000)
            return
        window = PlayerWindow(path, title)
        window.closed.connect(lambda w=window: self._player_closed(w))
        self.players.append(window)
        window.show()
        self.status.showMessage(f"Playing {title}", 4000)

    def _player_closed(self, window: PlayerWindow) -> None:
        if window in self.players:
            self.players.remove(window)

    def show_stats(self) -> None:
        with db.session() as conn:
            s = db.stats(conn)
            last = db.get_meta(conn, "last_ingest", "never")
        body = "\n".join(f"{k:<16}{v}" for k, v in s.items())
        QMessageBox.information(
            self, "Library", f"Region: {REGION}\nLast sync: {last}\n\n{body}"
        )

    def closeEvent(self, event) -> None:
        for window in list(self.players):
            window.close()
        for thread in (self.worker, self.scanner):
            if thread and thread.isRunning():
                thread.quit()
                thread.wait(3000)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OttDeck")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
