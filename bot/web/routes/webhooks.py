"""
Webhook-Verwaltung: listen, ansehen, bearbeiten, löschen, senden.

Webhooks sind das wichtigste Gestaltungswerkzeug der KI: Jede Information,
jede Regel, jede Begrüßung kann als **eigene Persona** mit eigenem Namen und
eigenem Profilbild erscheinen, statt als nüchterne Bot-Nachricht.

* ``GET  /api/v1/webhooks``               — alle Webhooks des Servers
* ``PATCH /api/v1/webhooks/{id}``         — Name/Avatar/Kanal ändern
* ``POST /api/v1/webhooks/{id}/send``     — als Webhook senden (mit Override)
* ``DELETE /api/v1/webhooks/{id}``        — löschen

Anlegen pro Kanal: ``POST /api/v1/channels/{id}/webhooks``. Komfort-Abkürzung
mit automatischer Persona: ``"webhook": {"name": …, "avatar": …}`` direkt in
``POST /api/v1/channels/{id}/messages`` und in jedem Setup-``messages``-Eintrag.
"""

from __future__ import annotations

from typing import Any, Dict, List

import discord

from ...serializers import serialize_message, serialize_webhook
from ...util import ApiError, parse_str, sf
from ..channel_ops import resolve_channel_by_ref
from ..context import Ctx, guard
from ..registry import route
from ..webhook_ops import send_as_webhook


async def _fetch_guild_webhook(ctx: Ctx) -> discord.Webhook:
    """Lädt einen Webhook und stellt sicher, dass er zu DIESEM Server gehört."""
    webhook_id = ctx.path_id("webhook_id")
    hook = await guard(ctx.client.fetch_webhook(webhook_id), action="Webhook laden")
    if getattr(hook, "guild_id", None) and hook.guild_id != ctx.guild.id:
        raise ApiError.forbidden("Dieser Webhook gehört zu einem anderen Server.", code="CROSS_GUILD")
    return hook


@route(
    "GET", "/api/v1/webhooks", scope="read", tags=("webhooks",),
    summary="Alle Webhooks des Servers",
    query={"channel": "nur Webhooks dieses Kanals (ID oder Name)"},
    description="Der Bot braucht 'manage_webhooks'. Ideal, um vorhandene "
                "Personas wiederzuverwenden, statt neue anzulegen (Limit: "
                "10 Webhooks pro Kanal).",
)
async def list_guild_webhooks(ctx: Ctx) -> Dict[str, Any]:
    hooks: List[discord.Webhook] = await guard(ctx.guild.webhooks(), action="Server-Webhooks laden")
    channel_ref = ctx.q("channel")
    if channel_ref:
        channel = resolve_channel_by_ref(ctx.guild, channel_ref, field="?channel")
        hooks = [h for h in hooks if h.channel_id == channel.id]
    return {
        "count": len(hooks),
        "webhooks": [serialize_webhook(h) for h in hooks],
        "limit_per_channel": 10,
        "hint": "Persona-Pattern: ein Webhook pro Kanal, Name/Avatar pro Nachricht "
                'übersteuern — {"webhook": {"name": "📜 Regeln", "avatar": "https://…"}} '
                "in POST …/messages.",
    }


@route(
    "GET", "/api/v1/webhooks/{webhook_id}", scope="read", tags=("webhooks",),
    summary="Einzelnen Webhook abrufen",
)
async def get_webhook(ctx: Ctx) -> Dict[str, Any]:
    hook = await _fetch_guild_webhook(ctx)
    return serialize_webhook(hook)


@route(
    "PATCH", "/api/v1/webhooks/{webhook_id}", scope="manage", tags=("webhooks",),
    summary="Webhook bearbeiten (Name, Avatar, Kanal)",
    body={
        "name": "str (1–80)",
        "avatar": "URL | Data-URI | null (entfernen) — das Profilbild der Persona",
        "channel": "Ziel-Kanal (ID/#name/Name) — verschiebt den Webhook",
        "reason": "str",
    },
    description="Damit bekommt jede Persona ein eigenes Gesicht: "
                "PATCH mit {\"avatar\": \"https://…/regeln.png\"} setzt das "
                "Profilbild dauerhaft für alle künftigen Nachrichten dieses Webhooks.",
)
async def patch_webhook(ctx: Ctx) -> Dict[str, Any]:
    hook = await _fetch_guild_webhook(ctx)
    data = await ctx.body()
    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "name" in data:
        name = parse_str(data["name"], field="name", min_length=1, max_length=80, allow_empty=False)
        if not name:
            raise ApiError.bad_request("name muss 1–80 Zeichen haben.")
        kwargs["name"] = name
        changes.append(f"name → {name}")

    if "avatar" in data:
        from ..images import resolve_image

        kwargs["avatar"] = await resolve_image(data["avatar"], session=ctx.http_session(),
                                               field="avatar")
        changes.append(f"avatar → {'entfernt' if kwargs['avatar'] is None else 'gesetzt'}")

    if "channel" in data and data["channel"] is not None:
        channel = resolve_channel_by_ref(ctx.guild, data["channel"], field="channel")
        kwargs["channel"] = channel
        changes.append(f"channel → #{getattr(channel, 'name', channel.id)}")

    if not kwargs:
        raise ApiError.bad_request(
            "Nichts zu tun: 'name', 'avatar' und/oder 'channel' angeben.",
            code="NO_CHANGES",
        )
    kwargs["reason"] = ctx.reason(data, default="Webhook aktualisiert (Arena AI)")
    updated = await guard(hook.edit(**kwargs), action="Webhook bearbeiten")
    await ctx.settle(0.4)
    return {"changed": changes, "webhook": serialize_webhook(updated or hook)}


@route(
    "DELETE", "/api/v1/webhooks/{webhook_id}", scope="manage", tags=("webhooks",),
    summary="Webhook löschen",
    query={"reason": "str"},
)
async def delete_webhook(ctx: Ctx) -> Dict[str, Any]:
    hook = await _fetch_guild_webhook(ctx)
    await guard(hook.delete(reason=ctx.reason(default="Webhook gelöscht (Arena AI)")),
                action="Webhook löschen")
    await ctx.settle(0.3)
    return {"deleted": {"id": sf(hook.id), "name": hook.name}}


@route(
    "POST", "/api/v1/webhooks/{webhook_id}/send", scope="write", tags=("webhooks", "messages"),
    summary="Nachricht als Webhook senden — mit eigenem Namen & Avatar",
    body={
        "content": "str (max. 2000)",
        "username": "str — Anzeigename DIESER Nachricht (Override)",
        "avatar": "http(s)-Bild-URL — Profilbild DIESER Nachricht (Override)",
        "embeds": "[{…}] — wie beim normalen Senden",
        "components": "[[…]] — Link-Buttons",
        "attachments": '[{"url": "https://…", "filename": "…"}]',
        "poll": "{question, answers, duration_hours}",
        "allowed_mentions": "{…}",
        "tts": "bool", "silent": "bool", "suppress_embeds": "bool",
        "thread": "Thread-ID — in diesen Forum-Post/Thread senden",
        "thread_name": "str — neuen Thread im Forum-Kanal starten",
        "reason": "str",
    },
    examples=[
        {"body": {"content": "Neue Regeln sind da!", "username": "📜 Serverregeln"}},
        {"body": {"embeds": [{"title": "🎉 Willkommen", "color": "#5865F2"}],
                  "username": "Willkommens-Team", "avatar": "https://example.com/w.png"}},
    ],
    description="Ein Webhook, viele Gesichter: ``username`` und ``avatar`` "
                "gelten pro Nachricht — so entsteht eine echte Persona pro "
                "Thema, ohne neue Webhooks anzulegen.",
)
async def webhook_send(ctx: Ctx) -> Dict[str, Any]:
    hook = await _fetch_guild_webhook(ctx)
    data = await ctx.body()
    from ..message_ops import build_message_kwargs

    # Kanal-Objekt für Validierung/Kontext (Attachments, Guild-Emojis, …)
    channel = ctx.guild.get_channel(hook.channel_id) or ctx.guild.get_thread(hook.channel_id)
    if channel is None:
        raise ApiError.conflict(
            "Der Zielkanal dieses Webhooks ist nicht mehr erreichbar.",
            hint="Verschiebe ihn: PATCH /api/v1/webhooks/{id} {\"channel\": \"…\"}.",
            code="WEBHOOK_CHANNEL_GONE",
        )

    kwargs, _notes = await build_message_kwargs(data, ctx=ctx, channel=channel)

    spec = {
        "name": data.get("username") or hook.name,
        "avatar": data.get("avatar") or data.get("avatar_url"),
    }
    message, notes = await send_as_webhook(
        channel, spec, kwargs, reason=ctx.reason(data, default=None)
    )
    await ctx.settle(0.3)
    result: Dict[str, Any] = {
        "sent": serialize_message(message),
        "id": sf(message.id),
        "via": "webhook",
        "webhook": {"id": sf(hook.id), "name": spec["name"]},
    }
    if notes:
        result["notes"] = notes
    return result
