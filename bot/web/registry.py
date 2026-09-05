"""
Endpoint-Registry.

Jeder Route-Handler wird mit ``@route(...)`` registriert. Aus denselben
Metadaten wird später **automatisch** ``GET /api/v1/capabilities`` erzeugt —
die API dokumentiert sich also selbst und kann nie aus der Doku laufen.

Handler-Signatur::

    @route("POST", "/api/v1/roles", scope="write", summary="Rolle anlegen",
           body={"name": "str (Pflicht)", "color": "#RRGGBB", "permissions": "[str]"})
    async def create_role(ctx: Ctx) -> Any:
        return serialize_role(await ...)

Der Rückgabewert wird automatisch in ``{"ok": true, "data": …}`` verpackt.
Ein ``ApiError`` wird zu ``{"ok": false, "error": …}`` mit passendem Status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = ("Endpoint", "ENDPOINTS", "route", "sorted_endpoints", "aiohttp_path")

Handler = Callable[..., Any]


@dataclass(slots=True)
class Endpoint:
    method: str
    path: str
    handler: Handler
    scope: str = "read"
    summary: str = ""
    description: str = ""
    tags: Tuple[str, ...] = ()
    query: Dict[str, str] = field(default_factory=dict)
    body: Dict[str, str] = field(default_factory=dict)
    response: str = ""
    public: bool = False
    examples: List[Dict[str, Any]] = field(default_factory=list)

    # ── Helpers ─────────────────────────────────────────────────────────────
    @property
    def is_dynamic(self) -> bool:
        return "{" in self.path

    def to_dict(self, *, include_handler: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "method": self.method,
            "path": self.path,
            "scope": self.scope,
            "summary": self.summary,
            "tags": list(self.tags),
            "public": self.public,
        }
        if self.description:
            data["description"] = self.description
        if self.query:
            data["query"] = dict(self.query)
        if self.body:
            data["body"] = dict(self.body)
        if self.response:
            data["response"] = self.response
        if self.examples:
            data["examples"] = list(self.examples)
        return data


ENDPOINTS: List[Endpoint] = []


def route(
    method: str,
    path: str,
    *,
    scope: str = "read",
    summary: str = "",
    description: str = "",
    tags: Sequence[str] = (),
    query: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, str]] = None,
    response: str = "",
    public: bool = False,
    examples: Optional[List[Dict[str, Any]]] = None,
) -> Callable[[Handler], Handler]:
    """Registriert einen API-Endpoint (siehe Modul-Docstring)."""

    def decorator(handler: Handler) -> Handler:
        doc = (handler.__doc__ or "").strip()
        lines = doc.splitlines()
        endpoint = Endpoint(
            method=method.upper(),
            path=path,
            handler=handler,
            scope=scope,
            summary=summary or (lines[0] if lines else ""),
            description=description or "\n".join(lines[1:]).strip(),
            tags=tuple(tags),
            query=dict(query or {}),
            body=dict(body or {}),
            response=response,
            public=public,
            examples=list(examples or []),
        )
        ENDPOINTS.append(endpoint)
        handler.__endpoint__ = endpoint  # type: ignore[attr-defined]
        return handler

    return decorator


def sorted_endpoints() -> List[Endpoint]:
    """
    Reihenfolge für die Registrierung bei aiohttp.

    aiohttp matcht Ressourcen in Einfügereihenfolge — statische Pfade müssen
    deshalb **vor** dynamischen kommen, sonst frisst ``/channels/{id}`` das
    ``/channels/tree``. Innerhalb einer Gruppe: längere Pfade zuerst.
    """
    return sorted(ENDPOINTS, key=lambda e: (e.is_dynamic, -len(e.path), e.path, e.method))


def aiohttp_path(path: str) -> str:
    """Übersetzt ``{name}``-Platzhalter (identisch für aiohttp) und normalisiert."""
    return path if path.startswith("/") else "/" + path


def endpoints_by_tag() -> Dict[str, List[Endpoint]]:
    grouped: Dict[str, List[Endpoint]] = {}
    for endpoint in ENDPOINTS:
        for tag in endpoint.tags or ("sonstiges",):
            grouped.setdefault(tag, []).append(endpoint)
    return grouped
