"""In-app video player for your own files.

PySide6 ships an ffmpeg multimedia backend, so MKV/H.264/H.265 play without
any extra install. Anything it still refuses falls back to VLC or mpv.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import links

PLAYER_STYLE = """
QWidget#playerRoot { background: #000000; }
QWidget#controls   { background: #14161a; }
QLabel#time        { color: #aeb4bd; font-size: 12px; }
QLabel#nowPlaying  { color: #e6e8eb; font-size: 13px; font-weight: 600; }
QPushButton        { background: #262b33; border: 1px solid #343b45; color: #e6e8eb;
                     border-radius: 5px; padding: 6px 12px; min-width: 40px; }
QPushButton:hover  { background: #2f353f; border-color: #3b82f6; }
QSlider::groove:horizontal { background: #2c313a; height: 4px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 12px; margin: -5px 0;
                             border-radius: 6px; }
"""


def _fmt(ms: int) -> str:
    if ms <= 0:
        return "0:00"
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class PlayerWindow(QWidget):
    """A standalone playback window. Closing it releases the file."""

    closed = Signal()

    def __init__(self, path: str, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(f"{title or Path(path).name} - OttDeck")
        self.setObjectName("playerRoot")
        self.setStyleSheet(PLAYER_STYLE)
        self.resize(1100, 660)
        self._seeking = False

        self.video = QVideoWidget()
        self.video.setStyleSheet("background: #000000;")

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.audio.setVolume(0.9)

        self.now = QLabel(title or Path(path).name)
        self.now.setObjectName("nowPlaying")

        self.play_btn = QPushButton("Pause")
        self.play_btn.clicked.connect(self.toggle)

        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.position.sliderPressed.connect(self._seek_start)
        self.position.sliderReleased.connect(self._seek_end)

        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("time")

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(90)
        self.volume.setMaximumWidth(110)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))

        self.full_btn = QPushButton("Fullscreen")
        self.full_btn.clicked.connect(self.toggle_fullscreen)

        self.external = QPushButton("Open in VLC")
        self.external.setToolTip("Use an external player instead")
        self.external.clicked.connect(self._open_external)

        bar = QHBoxLayout()
        bar.setContentsMargins(12, 8, 12, 10)
        bar.setSpacing(10)
        bar.addWidget(self.play_btn)
        bar.addWidget(self.position, 1)
        bar.addWidget(self.time)
        bar.addWidget(QLabel("Vol"))
        bar.addWidget(self.volume)
        bar.addWidget(self.full_btn)
        bar.addWidget(self.external)

        controls = QWidget()
        controls.setObjectName("controls")
        controls.setLayout(bar)

        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 12, 4)
        header.addWidget(self.now)
        header.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("time")
        header.addWidget(self.status)
        head = QWidget()
        head.setObjectName("controls")
        head.setLayout(header)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(head)
        layout.addWidget(self.video, 1)
        layout.addWidget(controls)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)

        for key, slot in (
            ("Space", self.toggle),
            ("F", self.toggle_fullscreen),
            ("Esc", self._leave_fullscreen),
            ("Right", lambda: self.nudge(10_000)),
            ("Left", lambda: self.nudge(-10_000)),
            ("Up", lambda: self.volume.setValue(min(100, self.volume.value() + 5))),
            ("Down", lambda: self.volume.setValue(max(0, self.volume.value() - 5))),
        ):
            QShortcut(QKeySequence(key), self, activated=slot)

        self.player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        QTimer.singleShot(0, self.player.play)

    # -- controls --------------------------------------------------------

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def nudge(self, ms: int) -> None:
        self.player.setPosition(max(0, self.player.position() + ms))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._leave_fullscreen()
        else:
            self.showFullScreen()
            self.full_btn.setText("Exit full")

    def _leave_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.full_btn.setText("Fullscreen")

    def _seek_start(self) -> None:
        self._seeking = True

    def _seek_end(self) -> None:
        self._seeking = False
        self.player.setPosition(self.position.value())

    def _open_external(self) -> None:
        self.player.pause()
        links.play_local(self.path)

    # -- player signals --------------------------------------------------

    def _on_position(self, pos: int) -> None:
        if not self._seeking:
            self.position.setValue(pos)
        self.time.setText(f"{_fmt(pos)} / {_fmt(self.player.duration())}")

    def _on_duration(self, dur: int) -> None:
        self.position.setRange(0, dur)
        self.time.setText(f"{_fmt(self.player.position())} / {_fmt(dur)}")

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("Pause" if playing else "Play")

    def _on_error(self, error, message: str = "") -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self.status.setText(
            f"Cannot play this file here ({message or error}). Try 'Open in VLC'."
        )

    def closeEvent(self, event) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.closed.emit()
        super().closeEvent(event)
