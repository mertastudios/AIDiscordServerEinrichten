"""Web-Schicht: REST-API, Console und Healthcheck (aiohttp)."""

from __future__ import annotations

from .app import build_app, start_web_server
from .registry import ENDPOINTS, Endpoint, route

__all__ = ("build_app", "start_web_server", "ENDPOINTS", "Endpoint", "route")
