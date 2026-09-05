"""Geplante Server-Events (Voice, Bühne, extern)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import discord
from discord.utils import MISSING

from ...serializers import serialize_event, serialize_user
from ...util import ApiError, parse_datetime, parse_enum, parse_str, sf
from ..channel_ops import resolve_channel_by_ref
from ..context import Ctx, guard
from ..images import resolve_image
from ..registry import route

_STATUS_ALIASES = {
    "geplant": "scheduled", "starten": "active", "aktiv": "active", "live": "active",
    "beendet": "completed", "fertig": "completed", "abgesagt": "canceled",
    "abgebrochen": "canceled", "cancel": "canceled", "complete": "completed",
}

_ENTITY_ALIASES = {
    "sprache": "voice", "sprachkanal": "voice", "bühne": "stage_instance",
    "buehne": "stage_instance", "stage": "stage_instance", "extern": "external",
    "externe": "external", "ort": "external",
}


async def _get_event(ctx: Ctx, event_id: int) -> discord.ScheduledEvent:
    guild = ctx.guild
    event = guild.get_scheduled_event(event_id)
    if event is None:
        events = await guard(guild.fetch_scheduled_events(), action="Events laden")
        event = next((e for e in events if e.id == event_id), None)
    if event is None:
        raise ApiError.not_found(
            f"Event {event_id} existiert nicht auf diesem Server.",
            hint="GET /api/v1/events listet alle geplanten Events.",
            code="EVENT_NOT_FOUND",
        )
    return event


@route(
    "GET", "/api/v1/events", scope="read", tags=("events",),
    summary="Alle geplanten Events",
    query={"with_counts": "true — Teilnehmerzahlen mitsenden (Standard true)"},
)
async def list_events(ctx: Ctx) -> Dict[str, Any]:
    with_counts = ctx.q_bool("with_counts", True)
    events = await guard(
        ctx.guild.fetch_scheduled_events(with_counts=bool(with_counts)), action="Events laden"
    )
    return {"count": len(events), "events": [serialize_event(e) for e in events]}


@route(
    "POST", "/api/v1/events", scope="write", tags=("events",),
    summary="Event anlegen",
    body={
        "name": "str (Pflicht, max. 100)",
        "start_time": "ISO-8601 (Pflicht)",
        "end_time": "ISO-8601 (optional)",
        "description": "str (max. 1000)",
        "channel": "Voice-/Bühnen-Kanal (ID oder Name)",
        "location": "Text für externe Events (z. B. 'https://twitch.tv/…')",
        "entity_type": "'voice'|'stage_instance'|'external' — wird sonst abgeleitet",
        "image": "URL/Data-URI — Coverbild",
        "privacy_level": "'guild_only' (einziger gültiger Wert)",
        "reason": "str",
    },
    examples=[
        {"body": {"name": "Community-Abend", "start_time": "2026-09-20T19:00:00Z",
                  "channel": "Lounge", "description": "Games & Quatschen"}},
        {"body": {"name": "Stream", "start_time": "2026-09-21T18:00:00Z",
                  "entity_type": "external", "location": "https://twitch.tv/example"}},
    ],
)
async def create_event(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    guild = ctx.guild
    name = parse_str(data.get("name"), field="name", min_length=1, max_length=100, allow_empty=False)
    if not name:
        raise ApiError.bad_request("'name' fehlt.")
    start_time = parse_datetime(data.get("start_time"), field="start_time")
    if start_time is None:
        raise ApiError.bad_request(
            "'start_time' fehlt.",
            hint='Format ISO-8601, z. B. "2026-09-20T19:00:00Z".',
        )

    location = parse_str(data.get("location"), field="location", max_length=100)
    channel: Optional[Any] = None
    if data.get("channel"):
        channel = resolve_channel_by_ref(
            guild, data["channel"], field="channel",
            kinds=(discord.VoiceChannel, discord.StageChannel),
        )

    entity_type = parse_enum(
        discord.EntityType, data.get("entity_type"), field="entity_type", extra=_ENTITY_ALIASES
    )
    if entity_type is None:
        if channel is not None:
            entity_type = (discord.EntityType.stage_instance
                           if isinstance(channel, discord.StageChannel) else discord.EntityType.voice)
        elif location:
            entity_type = discord.EntityType.external
        else:
            raise ApiError.bad_request(
                "Weder 'channel' noch 'location' angegeben.",
                hint="Voice-/Bühnen-Event: 'channel' setzen. Externes Event: 'location' setzen "
                     "und entity_type 'external'.",
                code="EVENT_TARGET_MISSING",
            )
    if entity_type is discord.EntityType.external and not location:
        raise ApiError.bad_request("Externe Events brauchen 'location'.", code="EVENT_LOCATION_MISSING")
    if entity_type in (discord.EntityType.voice, discord.EntityType.stage_instance) and channel is None:
        raise ApiError.bad_request(
            f"entity_type '{entity_type.name}' braucht einen 'channel'.",
            code="EVENT_CHANNEL_MISSING",
        )

    kwargs: Dict[str, Any] = {"name": name, "start_time": start_time, "entity_type": entity_type}
    if channel is not None:
        kwargs["channel"] = channel
    if location:
        kwargs["location"] = location
    description = parse_str(data.get("description"), field="description", max_length=1000)
    if description:
        kwargs["description"] = description
    end_time = parse_datetime(data.get("end_time"), field="end_time")
    if end_time is not None:
        kwargs["end_time"] = end_time
    if data.get("image"):
        kwargs["image"] = await resolve_image(data["image"], session=ctx.http_session(), field="image")
    kwargs["privacy_level"] = parse_enum(
        discord.PrivacyLevel, data.get("privacy_level", "guild_only"), field="privacy_level"
    ) or discord.PrivacyLevel.guild_only
    kwargs["reason"] = ctx.reason(data, default=f"Event '{name}' angelegt (Arena AI)")

    event = await guard(guild.create_scheduled_event(**kwargs), action=f"Event '{name}' anlegen")
    await ctx.settle(0.5)
    result = serialize_event(event) or {}
    result["url"] = getattr(event, "url", None)
    return result


@route(
    "GET", "/api/v1/events/{event_id}", scope="read", tags=("events",),
    summary="Event-Details inkl. Teilnehmer",
    query={"users": "int — diese vielen Teilnehmer mitsenden (Standard 25)"},
)
async def get_event(ctx: Ctx) -> Dict[str, Any]:
    event = await _get_event(ctx, ctx.path_id("event_id"))
    limit = ctx.q_int("users", 25, minimum=0, maximum=100) or 0
    data = serialize_event(event) or {}
    data["url"] = getattr(event, "url", None)
    if limit:
        users: List[Any] = []
        try:
            async for user in event.users(limit=limit):
                users.append(serialize_user(user))
        except discord.HTTPException:
            users = []
        data["users"] = users
    return data


@route(
    "PATCH", "/api/v1/events/{event_id}", scope="write", tags=("events",),
    summary="Event bearbeiten (auch Status: starten/beenden/absagen)",
    body={
        "name": "str", "description": "str", "start_time": "ISO", "end_time": "ISO",
        "channel": "ID/Name", "location": "str", "entity_type": "…", "status": "…",
        "image": "URL/Data-URI", "reason": "str",
    },
)
async def patch_event(ctx: Ctx) -> Dict[str, Any]:
    event = await _get_event(ctx, ctx.path_id("event_id"))
    data = await ctx.body()
    guild = ctx.guild
    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "name" in data:
        kwargs["name"] = parse_str(data["name"], field="name", max_length=100, allow_empty=False) or event.name
        changes.append("name")
    if "description" in data:
        kwargs["description"] = parse_str(data["description"], field="description", max_length=1000) or ""
        changes.append("description")
    if "start_time" in data:
        start = parse_datetime(data["start_time"], field="start_time")
        if start is None:
            raise ApiError.bad_request("start_time darf nicht null sein.")
        kwargs["start_time"] = start
        changes.append("start_time")
    if "end_time" in data:
        kwargs["end_time"] = parse_datetime(data["end_time"], field="end_time")
        changes.append("end_time")
    if "channel" in data:
        raw = data["channel"]
        if raw is None:
            kwargs["channel"] = None
        else:
            kwargs["channel"] = resolve_channel_by_ref(
                guild, raw, field="channel", kinds=(discord.VoiceChannel, discord.StageChannel)
            )
        changes.append("channel")
    if "location" in data:
        kwargs["location"] = parse_str(data["location"], field="location", max_length=100) or ""
        changes.append("location")
    if "entity_type" in data:
        kwargs["entity_type"] = parse_enum(discord.EntityType, data["entity_type"], field="entity_type")
        changes.append("entity_type")
    if "image" in data:
        kwargs["image"] = await resolve_image(data["image"], session=ctx.http_session(), field="image") or MISSING
        changes.append("image")
    if "status" in data:
        kwargs["status"] = parse_enum(
            discord.EventStatus, data["status"], field="status", extra=_STATUS_ALIASES
        )
        changes.append(f"status → {getattr(kwargs['status'], 'name', kwargs['status'])}")

    if not kwargs:
        raise ApiError.bad_request(
            "Keine änderbaren Felder.",
            hint="Unterstützt: name, description, start_time, end_time, channel, location, "
                 "entity_type, image, status.",
            code="NO_CHANGES",
        )
    kwargs["reason"] = ctx.reason(data, default=f"Event '{event.name}' aktualisiert (Arena AI)")

    updated = await guard(event.edit(**kwargs), action=f"Event '{event.name}' bearbeiten")
    await ctx.settle(0.5)
    return {"changed": changes, "event": serialize_event(updated or event), "id": sf(event.id)}


@route(
    "POST", "/api/v1/events/{event_id}/start", scope="write", tags=("events",),
    summary="Event jetzt starten",
)
async def start_event(ctx: Ctx) -> Dict[str, Any]:
    event = await _get_event(ctx, ctx.path_id("event_id"))
    await guard(event.start(), action=f"Event '{event.name}' starten")
    await ctx.settle(0.4)
    return {"status": "active", "event": serialize_event(event)}


@route(
    "POST", "/api/v1/events/{event_id}/end", scope="write", tags=("events",),
    summary="Event beenden",
)
async def end_event(ctx: Ctx) -> Dict[str, Any]:
    event = await _get_event(ctx, ctx.path_id("event_id"))
    await guard(event.end(), action=f"Event '{event.name}' beenden")
    await ctx.settle(0.4)
    return {"status": "completed", "event": serialize_event(event)}


@route(
    "POST", "/api/v1/events/{event_id}/cancel", scope="write", tags=("events",),
    summary="Event absagen",
)
async def cancel_event(ctx: Ctx) -> Dict[str, Any]:
    event = await _get_event(ctx, ctx.path_id("event_id"))
    await guard(event.cancel(), action=f"Event '{event.name}' absagen")
    await ctx.settle(0.4)
    return {"status": "canceled", "event": serialize_event(event)}


@route(
    "DELETE", "/api/v1/events/{event_id}", scope="write", tags=("events",),
    summary="Event löschen",
    query={"reason": "str"},
)
async def delete_event(ctx: Ctx) -> Dict[str, Any]:
    event = await _get_event(ctx, ctx.path_id("event_id"))
    info = {"id": sf(event.id), "name": event.name}
    await guard(event.delete(reason=ctx.reason(default=f"Event '{event.name}' gelöscht (Arena AI)")),
                action=f"Event '{event.name}' löschen")
    await ctx.settle(0.4)
    return {"deleted": info}
