"""
Nachrichten: lesen, senden, bearbeiten, löschen, Reaktionen, Pins, Purge.

Der Sende-Endpoint unterstützt das komplette Discord-Feature-Set über
:func:`~bot.web.message_ops.build_message_kwargs` — Embeds, Buttons, Anhänge
per URL, Umfragen, Antworten und feingranulare Mention-Kontrolle.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List
from urllib.parse import unquote

import discord

from ...serializers import serialize_message
from ...util import ApiError, as_id, parse_bool, parse_int, parse_str, sf
from ..context import Ctx, guard
from ..message_ops import build_embeds, build_message_kwargs
from ..registry import route


def _resolve_message(channel: Any, message_id: int) -> discord.PartialMessage:
    if hasattr(channel, "get_partial_message"):
        return channel.get_partial_message(message_id)
    return discord.PartialMessageable(state=channel._state, id=channel.id).get_partial_message(message_id)  # noqa: SLF001


# ─────────────────────────────────────────────────────────────────────────────
#  Lesen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/channels/{channel_id}/messages", scope="read", tags=("messages",),
    summary="Nachrichten eines Kanals (Historie)",
    query={
        "limit": "int, Standard 25 (max. 100)",
        "before": "Nachrichten-ID — älter als diese",
        "after": "Nachrichten-ID — neuer als diese",
        "around": "Nachrichten-ID — rundherum",
        "oldest_first": "true — chronologisch aufsteigend",
        "author": "User-ID — filtert auf diesen Autor",
        "contains": "Textfilter (Groß-/Kleinschreibung egal)",
        "detailed": "true/false — Components mitsenden (Standard true)",
    },
)
async def list_messages(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    if not hasattr(channel, "history"):
        raise ApiError.bad_request(
            f"'{getattr(channel, 'name', channel.id)}' hat keine Nachrichten-Historie.",
            hint="Nur Text-Kanäle, Threads und Foren-Posts unterstützen das.",
            code="CHANNEL_TYPE_MISMATCH",
        )

    limit = ctx.q_int("limit", 25, minimum=1, maximum=ctx.config.max_message_history) or 25
    kwargs: Dict[str, Any] = {"limit": limit, "oldest_first": ctx.q_bool("oldest_first")}
    for key in ("before", "after", "around"):
        value = ctx.q_id(key)
        if value:
            kwargs[key] = discord.Object(id=value)

    author_filter = ctx.q_id("author")
    contains = (ctx.q("contains") or "").lower()
    detailed = ctx.q_bool("detailed", True)

    messages: List[Dict[str, Any]] = []
    scanned = 0
    try:
        async for message in channel.history(**kwargs):
            scanned += 1
            if author_filter and message.author.id != author_filter:
                continue
            if contains and contains not in (message.content or "").lower():
                continue
            messages.append(serialize_message(message, detailed=bool(detailed)))
    except discord.Forbidden as exc:
        raise ApiError.forbidden(
            "Nachrichten nicht lesbar: dem Bot fehlt 'read_message_history' oder "
            "'view_channel' in diesem Kanal.",
            code="MESSAGE_HISTORY_FORBIDDEN",
        ) from exc

    return {
        "channel": {"id": sf(channel.id), "name": getattr(channel, "name", None)},
        "count": len(messages),
        "scanned": scanned,
        "filter": {"author": sf(author_filter) if author_filter else None, "contains": contains or None},
        "messages": messages,
        "note": "Neueste zuerst, außer ?oldest_first=true.",
    }


@route(
    "GET", "/api/v1/channels/{channel_id}/messages/{message_id}", scope="read", tags=("messages",),
    summary="Einzelne Nachricht",
)
async def get_message(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    message = await guard(
        channel.fetch_message(message_id), action=f"Nachricht {message_id} laden"
    )
    return serialize_message(message)


@route(
    "GET", "/api/v1/channels/{channel_id}/pins", scope="read", tags=("messages",),
    summary="Angepinnte Nachrichten",
)
async def list_pins(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    if not hasattr(channel, "pins"):
        raise ApiError.bad_request(f"'{getattr(channel, 'name', channel.id)}' unterstützt keine Pins.",
                                   code="CHANNEL_TYPE_MISMATCH")
    pinned = await guard(channel.pins(), action="Pins laden")
    return {"count": len(pinned), "messages": [serialize_message(m, detailed=False) for m in pinned]}


# ─────────────────────────────────────────────────────────────────────────────
#  Senden
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "POST", "/api/v1/channels/{channel_id}/messages", scope="write", tags=("messages", "webhooks"),
    summary="Nachricht senden (Text, Embeds, Buttons, Dateien, Umfrage — oder als Webhook-Persona)",
    body={
        "content": "str (max. 2000 Zeichen)",
        "webhook": '{ "name": "📜 Serverregeln", "avatar": "https://…/bild.png" } | true | "Name" '
                   "— sendet als Webhook-Persona mit eigenem Namen & Profilbild",
        "webhook_name": "str — Kurzform für webhook.name",
        "webhook_avatar": "URL — Kurzform für webhook.avatar",
        "embeds": '[{"title","description","color","url","footer":{"text","icon_url"},'
                  '"thumbnail":"url","image":"url","author":{"name","url","icon_url"},'
                  '"fields":[{"name","value","inline"}],"timestamp":"ISO"}]',
        "components": '[[{"label":"Zur Website","style":"link","url":"https://…"}]] — Buttons',
        "attachments": '[{"url":"https://…/bild.png","filename":"bild.png","spoiler":false}]',
        "poll": '{"question":"…","answers":["A","B"],"duration_hours":24,"multiple":false}',
        "stickers": '["Sticker-ID"] (max. 3)',
        "allowed_mentions": '{"parse":["users","roles","everyone"],"replied_user":false}',
        "reply_to": "Nachrichten-ID",
        "mention_reply": "bool",
        "tts": "bool", "silent": "bool", "suppress_embeds": "bool",
        "pin": "bool — Nachricht nach dem Senden anpinnen (perfekt für Regeln/Infos)",
        "delete_after": "Sekunden — Selbstlöschung",
    },
    examples=[
        {"body": {"content": "**Willkommen!** Schön, dass du da bist 👋"}},
        {"body": {"webhook": {"name": "📜 Serverregeln",
                              "avatar": "https://cdn.discordapp.com/embed/avatars/0.png"},
                  "embeds": [{"title": "Regeln", "color": "#5865F2",
                              "fields": [{"name": "1. Respekt", "value": "Keine Beleidigungen."}],
                              "footer": {"text": "Team"}}],
                  "pin": True}},
        {"body": {"content": "Mehr Infos:",
                  "components": [[{"label": "Website", "style": "link", "url": "https://example.com"}]]}},
    ],
    description="Mit ``webhook``-Feld erscheint die Nachricht als eigene Persona "
                "(Name + Avatar) statt als Bot — für Regeln, News & Willkommen "
                "immer vorziehen. Der Webhook wird bei Bedarf automatisch angelegt. "
                "Antwort enthält die fertige Nachricht inklusive ID und jump_url.",
)
async def send_message(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    if not hasattr(channel, "send"):
        raise ApiError.bad_request(
            f"In '{getattr(channel, 'name', channel.id)}' kann nicht gesendet werden.",
            hint="Kategorien haben keine Nachrichten — nutze einen Text-/Forum-Kanal.",
            code="CHANNEL_TYPE_MISMATCH",
        )
    data = await ctx.body()

    from ..webhook_ops import normalize_webhook_spec, send_as_webhook

    webhook_spec = normalize_webhook_spec(data)
    kwargs, notes = await build_message_kwargs(data, ctx=ctx, channel=channel)

    if webhook_spec is not None:
        message, webhook_notes = await send_as_webhook(
            channel, webhook_spec, kwargs,
            reason=ctx.reason(data, default=None),
        )
        notes = notes + webhook_notes
        via = "webhook"
    else:
        message = await guard(channel.send(**kwargs),
                              action=f"Nachricht in '{getattr(channel, 'name', channel.id)}' senden")
        via = "bot"

    pinned = False
    if parse_bool(data.get("pin"), field="pin", default=False) and hasattr(message, "pin"):
        await guard(message.pin(), action="Nachricht anpinnen")
        pinned = True
        notes.append("Nachricht angepinnt.")

    await ctx.settle(0.3)
    result: Dict[str, Any] = {"sent": serialize_message(message), "id": sf(message.id),
                              "jump_url": message.jump_url, "via": via}
    if pinned:
        result["pinned"] = True
    if notes:
        result["notes"] = notes
    return result


@route(
    "PATCH", "/api/v1/channels/{channel_id}/messages/{message_id}", scope="write", tags=("messages",),
    summary="Nachricht bearbeiten",
    body={
        "content": "str | null",
        "embeds": "[…]",
        "components": "[…]",
        "suppress_embeds": "bool — Embeds nachträglich ausblenden",
        "allowed_mentions": "{…}",
    },
    description="Fremde Nachrichten lassen sich nur bei Embeds/Flags ändern, "
                "nicht beim Text.",
)
async def edit_message(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    message = await guard(channel.fetch_message(message_id), action="Nachricht laden")
    data = await ctx.body()

    guild = getattr(channel, "guild", None)
    kwargs: Dict[str, Any] = {}
    if "content" in data:
        text = parse_str(data["content"], field="content", max_length=2000)
        kwargs["content"] = text
    if "embeds" in data or "embed" in data:
        kwargs["embeds"] = build_embeds(data.get("embeds", data.get("embed")))
    if "components" in data:
        from ..message_ops import build_view

        kwargs["view"] = build_view(data["components"], guild=guild)
    if "suppress_embeds" in data:
        kwargs["suppress"] = bool(parse_bool(data["suppress_embeds"], field="suppress_embeds"))
    if "allowed_mentions" in data:
        from ..message_ops import build_allowed_mentions

        kwargs["allowed_mentions"] = build_allowed_mentions(data["allowed_mentions"])

    if not kwargs:
        raise ApiError.bad_request(
            "Keine änderbaren Felder.",
            hint="Unterstützt: content, embeds, components, suppress_embeds, allowed_mentions.",
            code="NO_CHANGES",
        )

    bot_id = ctx.client.user.id if ctx.client.user else 0
    if message.author.id != bot_id and "content" in kwargs:
        raise ApiError.forbidden(
            "Fremde Nachrichten können nicht umgeschrieben werden.",
            hint="Nur die eigene Nachricht darf 'content' ändern. Bei fremden "
                 "Nachrichten geht höchstens 'suppress_embeds'.",
            code="NOT_OWN_MESSAGE",
        )

    updated = await guard(message.edit(**kwargs), action="Nachricht bearbeiten")
    await ctx.settle(0.3)
    return {"edited": serialize_message(updated or message), "id": sf(message_id)}


@route(
    "DELETE", "/api/v1/channels/{channel_id}/messages/{message_id}", scope="manage", tags=("messages",),
    summary="Nachricht löschen",
    query={"reason": "str (erscheint nicht im Audit-Log, nur im Relay-Log)"},
)
async def delete_message(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    message = await guard(channel.fetch_message(message_id), action="Nachricht laden")
    info = {"id": sf(message.id), "author": message.author.name,
            "content_preview": (message.content or "")[:120]}
    await guard(message.delete(), action="Nachricht löschen")
    await ctx.settle(0.3)
    return {"deleted": info}


@route(
    "POST", "/api/v1/channels/{channel_id}/messages/bulk-delete", scope="danger", tags=("messages",),
    summary="Mehrere Nachrichten löschen",
    body={
        "message_ids": '["ID", …] — max. 100, nur Nachrichten jünger als 14 Tage',
        "reason": "str", "confirm": "true",
    },
)
async def bulk_delete_messages(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    if not parse_bool(data.get("confirm"), field="confirm", default=False):
        raise ApiError.bad_request(
            "Massenlöschung von Nachrichten ist destruktiv.",
            hint='Sende {"message_ids": ["…"], "confirm": true}.',
            code="CONFIRM_REQUIRED",
        )
    raw = data.get("message_ids")
    if not isinstance(raw, list) or not raw:
        raise ApiError.bad_request("'message_ids' muss eine nicht-leere Liste sein.")
    if len(raw) > 100:
        raise ApiError.bad_request("Discord erlaubt maximal 100 Nachrichten pro Bulk-Delete.")

    messages = [_resolve_message(channel, as_id(item, field=f"message_ids[{i}]"))
                for i, item in enumerate(raw)]
    deleted = 0
    errors: List[Dict[str, Any]] = []
    if hasattr(channel, "delete_messages") and len(messages) > 1:
        try:
            await guard(channel.delete_messages(messages), action="Nachrichten en bloc löschen")
            deleted = len(messages)
        except ApiError as exc:
            errors.append({"bulk": True, "code": exc.code, "message": exc.message,
                           "hint": "Bulk-Delete klappt nur für Nachrichten < 14 Tage alt. "
                                   "Ältere müssen einzeln gelöscht werden."})
            deleted = 0
    if deleted == 0:
        for message in messages:
            try:
                await guard(message.delete(), action="Nachricht löschen")
                deleted += 1
            except ApiError as exc:
                errors.append({"id": sf(message.id), "code": exc.code, "message": exc.message})
            await asyncio.sleep(0.3)
    await ctx.settle(0.5)
    return {"deleted_count": deleted, "failed_count": len(errors), "errors": errors or None}


@route(
    "POST", "/api/v1/channels/{channel_id}/purge", scope="danger", tags=("messages",),
    summary="Nachrichten nach Kriterien löschen (Purge)",
    body={
        "limit": "int, Standard 50 (max. 500 — prüft diese vielen)",
        "author_id": "nur Nachrichten dieses Nutzers",
        "contains": "nur Nachrichten mit diesem Text",
        "bots_only": "bool — nur Bot-Nachrichten",
        "webhooks_only": "bool — nur Webhook-Nachrichten (z. B. alte Regeln)",
        "before": "Nachrichten-ID",
        "after": "Nachrichten-ID",
        "bulk": "bool, Standard true — schnell, aber nur Nachrichten < 14 Tage. "
                "false = einzeln löschen, erfasst auch ältere (langsamer, aber vollständig)",
        "confirm": "true — Pflicht",
        "reason": "str",
    },
    description="Das Standardwerkzeug beim Erneuern von Regel-/Info-/Willkommens-"
                "Kanälen: erst Purge (bots_only/webhooks_only), dann die neue "
                "Nachricht posten. Alte Versionen dürfen nie doppelt herumstehen. "
                "Nachrichten älter als 14 Tage werden im Bulk-Modus still "
                "übersprungen — für die brauche es bulk: false.",
)
async def purge_messages(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    if not parse_bool(data.get("confirm"), field="confirm", default=False):
        raise ApiError.bad_request(
            "Purge löscht unwiderruflich Nachrichten.",
            hint='Sende {"limit": 50, "bots_only": true, "confirm": true}.',
            code="CONFIRM_REQUIRED",
        )
    if not hasattr(channel, "purge"):
        raise ApiError.bad_request(f"'{getattr(channel, 'name', channel.id)}' unterstützt kein Purge.",
                                   code="CHANNEL_TYPE_MISMATCH")

    limit = parse_int(data.get("limit"), field="limit", default=50, minimum=1, maximum=500) or 50
    author_id = data.get("author_id") and as_id(data["author_id"], field="author_id")
    contains = (data.get("contains") or "").lower()
    bots_only = bool(parse_bool(data.get("bots_only"), field="bots_only", default=False))
    webhooks_only = bool(parse_bool(data.get("webhooks_only"), field="webhooks_only", default=False))

    def check(message: discord.Message) -> bool:
        if author_id and message.author.id != author_id:
            return False
        if webhooks_only and getattr(message, "webhook_id", None) is None:
            return False
        if bots_only and not message.author.bot:
            return False
        if contains and contains not in (message.content or "").lower():
            return False
        return True

    bulk = bool(parse_bool(data.get("bulk"), field="bulk", default=True))
    kwargs: Dict[str, Any] = {"limit": limit, "check": check, "bulk": bulk,
                              "reason": ctx.reason(data, default="Purge (Arena AI)")}
    for key in ("before", "after"):
        if data.get(key):
            kwargs[key] = discord.Object(id=as_id(data[key], field=key))

    removed = await guard(channel.purge(**kwargs), action="Purge ausführen")
    await ctx.settle(0.8)
    return {
        "deleted_count": len(removed),
        "deleted": [{"id": sf(m.id), "author": m.author.name} for m in removed[:50]],
        "channel_id": sf(channel.id),
        "mode": "bulk" if bulk else "einzeln (erfasst auch Nachrichten > 14 Tage)",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Reaktionen & Pins
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "PUT", "/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
    scope="write", tags=("messages",),
    summary="Reaktion hinzufügen",
    description="``{emoji}`` = Unicode (``%F0%9F%91%8D`` für 👍), ``name:id`` für "
                "Server-Emojis oder einfach ``:name:``. Der Bot reagiert als er selbst.",
)
async def add_reaction(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    emoji = _resolve_reaction_emoji(ctx, unquote(ctx.path("emoji") or ""))
    message = await guard(channel.fetch_message(message_id), action="Nachricht laden")
    await guard(message.add_reaction(emoji), action=f"Reaktion {emoji} hinzufügen")
    return {"reacted": True, "message_id": sf(message_id), "emoji": str(emoji)}


@route(
    "DELETE", "/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
    scope="manage", tags=("messages",),
    summary="Reaktion entfernen (alle oder nur die des Bots)",
    query={"all": "true — auch Reaktionen anderer Nutzer entfernen"},
)
async def remove_reaction(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    raw = unquote(ctx.path("emoji") or "")
    message = await guard(channel.fetch_message(message_id), action="Nachricht laden")

    if raw.strip().lower() in {"all", "alle", "*"}:
        await guard(message.clear_reactions(), action="Alle Reaktionen entfernen")
        return {"cleared": True, "message_id": sf(message_id)}

    emoji = _resolve_reaction_emoji(ctx, raw)
    if ctx.q_bool("all", False):
        await guard(message.clear_reaction(emoji), action=f"Reaktionen {emoji} entfernen")
    else:
        await guard(message.remove_reaction(emoji, ctx.client.user), action=f"Eigene Reaktion {emoji} entfernen")
    return {"removed": True, "message_id": sf(message_id), "emoji": str(emoji)}


def _resolve_reaction_emoji(ctx: Ctx, raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise ApiError.bad_request("Emoji fehlt im Pfad.", code="EMOJI_MISSING")
    guild = ctx.guild

    match = discord.PartialEmoji.from_str(text) if re.match(r"<a?:[^:>]+:\d+>", text) else None
    if match is not None:
        return match

    if text.startswith(":") and text.endswith(":"):
        name = text[1:-1].lower()
        for emoji in guild.emojis:
            if emoji.name and emoji.name.lower() == name:
                return emoji
        raise ApiError.not_found(f"Server-Emoji '{text}' nicht gefunden.", code="EMOJI_NOT_FOUND")

    if text.isdigit():
        found = guild.get_emoji(int(text))
        if found is None:
            raise ApiError.not_found(f"Emoji {text} nicht gefunden.", code="EMOJI_NOT_FOUND")
        return found
    return text


@route(
    "PUT", "/api/v1/channels/{channel_id}/pins/{message_id}", scope="write", tags=("messages",),
    summary="Nachricht anpinnen",
)
async def pin_message(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    message = await guard(channel.fetch_message(message_id), action="Nachricht laden")
    await guard(message.pin(), action="Nachricht anpinnen")
    await ctx.settle(0.3)
    return {"pinned": True, "message_id": sf(message_id)}


@route(
    "DELETE", "/api/v1/channels/{channel_id}/pins/{message_id}", scope="write", tags=("messages",),
    summary="Pin entfernen",
)
async def unpin_message(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    message_id = ctx.path_id("message_id")
    message = await guard(channel.fetch_message(message_id), action="Nachricht laden")
    await guard(message.unpin(), action="Pin entfernen")
    await ctx.settle(0.3)
    return {"pinned": False, "message_id": sf(message_id)}
