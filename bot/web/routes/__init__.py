"""Importiert alle Route-Module, damit sich die Endpoints registrieren."""

from __future__ import annotations

from . import (  # noqa: F401  (Import erzeugt die Registrierung)
    channels,
    events,
    expressions,
    guild,
    invites,
    members,
    messages,
    meta,
    moderation,
    roles,
    setup,
)

__all__ = [
    "meta",
    "guild",
    "channels",
    "roles",
    "members",
    "moderation",
    "messages",
    "invites",
    "expressions",
    "events",
    "setup",
]
