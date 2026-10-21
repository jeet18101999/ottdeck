"""Launcher: double-click this, or run `python app.py`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ottdeck.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
