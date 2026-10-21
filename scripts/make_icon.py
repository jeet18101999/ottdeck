"""Generate assets/ottdeck.ico - a play triangle on a rounded tile."""
import os, sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication


def draw(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#2563eb"))
    grad.setColorAt(1.0, QColor("#1e3a8a"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    radius = size * 0.22
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    p.setBrush(QColor("#ffffff"))
    w, h = size * 0.34, size * 0.38
    cx, cy = size * 0.54, size * 0.5
    p.drawPolygon(QPolygonF([
        QPointF(cx - w / 2, cy - h / 2),
        QPointF(cx + w / 2, cy),
        QPointF(cx - w / 2, cy + h / 2),
    ]))
    p.end()
    return pix


app = QApplication([])
out = Path(__file__).resolve().parent.parent / "assets" / "ottdeck.ico"
out.parent.mkdir(exist_ok=True)

# Qt writes a single-image ICO; 256px downscales cleanly for every shell size.
if not draw(256).save(str(out), "ICO"):
    print("ICO write failed", file=sys.stderr)
    raise SystemExit(1)
print(f"wrote {out}  ({out.stat().st_size} bytes)")
