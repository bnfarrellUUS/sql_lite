"""
SQLite Browser - desktop entry point.

Wraps the existing Flask app in a native window via pywebview (Windows
WebView2). pywebview serves the WSGI app on a free localhost port itself, so
all routes and the UI are reused unchanged. This is the entry point PyInstaller
freezes into ``SQLiteBrowser.exe``.

Run in dev:
    pip install pywebview
    python desktop.py
"""

from __future__ import annotations

import webview

from server import app


def main() -> None:
    webview.create_window("SQLite Browser", app, width=1200, height=800)
    webview.start()


if __name__ == "__main__":
    main()
