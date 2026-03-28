"""Debug UI for a2akit agents — Chat + Task Dashboard."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from fastapi import FastAPI

_STATIC_DIR = Path(__file__).parent / "_static"


def _load_html() -> str:
    """Load chat.html from package data, falling back to file-system path."""
    try:
        return resources.files("a2akit").joinpath("_static/chat.html").read_text("utf-8")
    except (TypeError, FileNotFoundError):
        return _STATIC_DIR.joinpath("chat.html").read_text("utf-8")


def mount_chat_ui(app: FastAPI) -> None:
    """Mount the debug chat UI at ``/chat``."""
    from importlib.metadata import version

    try:
        pkg_version = version("a2akit")
    except Exception:
        pkg_version = "dev"

    html = _load_html().replace("{{VERSION}}", pkg_version)

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    async def debug_chat() -> HTMLResponse:
        return HTMLResponse(html)
