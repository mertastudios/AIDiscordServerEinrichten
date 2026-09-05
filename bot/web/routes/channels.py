"""
Kanal-Verwaltung: lesen, anlegen, bearbeiten, sortieren, löschen, Berechtigungen.

Enthält auch die Komfort-Endpoints ``lock``/``unlock`` (Privatkanal auf
Knopfdruck) und ``bulk``/``bulk-delete`` für Massenaktionen.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import discord

from ...serializers import (
    serialize_channel,
    serialize_channel_tree,
    serialize_webhook,
)
from ...util import (
    ApiError,
    as_id,
    channel_type_from_name,
    parse_bool,
    parse_int,
    parse_str,
    resolve_target,
    sf,
)
from ..channel_ops import create_channel, edit_channel, resolve_category, resolve_channel_by_ref
from ..context import Ctx, guard
from ..registry import route

# ─────────────────────────────────────────────────────────────────────────────
#  Lesen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/channels", scope="read", tags=("channels",),
    summary="Alle Kanäle als flache Liste",
    query={
        "type": "Filter: text|voice|category|forum|stage|announcement|media|thread",
        "category": "nur Kanäle dieser Kategorie (ID oder Name)",
        "detailed": "true/false — Overwrites & Metadaten mitsenden (Standard true)",
    },
)
async def list_channels(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    type_filter = ctx.q("type")
    category_filter = ctx.q("category")
    detailed = ctx.q_bool("detailed", True)

    wanted_type = channel_type_from_name(type_filter, field="?type") if type_filter else None
    category: Optional[discord.CategoryChannel] = None
    if category_filter:
        category = resolve_category(guild, category_filter, field="?category")

    channels = [serialize_channel(c, detailed=bool(detailed)) for c in guild.channels]
    if wanted_type is not None:
        channels = [c for c in channels if c and c.get("type") == int(wanted_type.value)]
    if category is not None:
        channels = [c for c in channels if c and c.get("category_id") == sf(category.id)]

    threads = [serialize_channel(t) for t in guild.threads]

    return {
        "count": len(channels),
        "thread_count": len(threads),
        "channels": channels,
        "threads": threads,
        "hint": "GET /api/v1/channels/tree liefert dieselben Daten als Kategorien-Baum.",
    }


@route(
    "GET", "/api/v1/channels/tree", scope="read", tags=("channels",),
    summary="Kanalstruktur als Baum (Kategorien → Kanäle → Threads)",
    description="Spiegelt exakt die Ansicht im Discord-Client. Ideal, um die "
                "Serverstruktur zu verstehen und Umstrukturierungen zu planen.",
)
async def channel_tree(ctx: Ctx) -> Dict[str, Any]:
    return serialize_channel_tree(ctx.guild)


@route(
    "GET", "/api/v1/channels/{channel_id}", scope="read", tags=("channels",),
    summary="Einzelnen Kanal abrufen",
)
async def get_channel(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = serialize_channel(channel) or {}
    if isinstance(channel, discord.CategoryChannel):
        data["channels"] = [serialize_channel(c) for c in sorted(channel.channels, key=lambda c: c.position)]
    if isinstance(channel, discord.ForumChannel):
        try:
            posts: List[Any] = []
            async for thread in channel.archived_threads(limit=25):
                posts.append(serialize_channel(thread))
            data["archived_posts_sample"] = posts
        except discord.HTTPException:
            data["archived_posts_sample"] = None
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  Anlegen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "POST", "/api/v1/channels", scope="write", tags=("channels",),
    summary="Kanal anlegen (beliebiger Typ)",
    body={
        "name": "str (Pflicht)",
        "type": "text|announcement|voice|stage|forum|media|category (Standard text)",
        "category": "Kategorie-ID, '#name' oder null",
        "topic": "str",
        "nsfw": "bool",
        "slowmode_delay": "int 0–21600 Sekunden",
        "position": "int",
        "overwrites": '[{"id": "@everyone", "deny": ["send_messages"]}, {"type":"role","id":"…","allow":[…]}]',
        "bitrate": "int 8000–384000 (Voice)",
        "user_limit": "int 0–99 (Voice)",
        "rtc_region": "str oder 'auto' (Voice)",
        "video_quality_mode": "'auto'|'full' (Voice)",
        "status": "str — Voice-Kanal-Status (Boost-Stufe 1+)",
        "default_auto_archive_duration": "60|1440|4320|10080 Minuten",
        "default_thread_slowmode_delay": "int Sekunden",
        "available_tags": '["Frage"] oder [{"name":"Frage","emoji":"❓","moderated":false}] (Forum)',
        "default_sort_order": "'latest_activity'|'creation_date' (Forum)",
        "default_layout": "'not_set'|'list_view'|'gallery_view' (Forum)",
        "reason": "str",
    },
    examples=[
        {"body": {"name": "allgemein", "type": "text", "topic": "Hauptchat",
                  "slowmode_delay": 3, "reason": "Setup"}},
        {"body": {"name": "🎧 Lounge", "type": "voice", "user_limit": 10}},
        {"body": {"name": "📌 INFORMATION", "type": "category",
                  "overwrites": [{"id": "@everyone", "deny": ["send_messages"]}]}},
    ],
)
async def create_channel_route(ctx: Ctx) -> Dict[str, Any]:
    spec = await ctx.body()
    channel = await create_channel(
        ctx.guild, spec, reason=ctx.reason(spec, default=None)
    )
    await ctx.settle(0.5)
    return {"created": serialize_channel(channel), "id": sf(channel.id)}


@route(
    "POST", "/api/v1/channels/bulk", scope="write", tags=("channels",),
    summary="Mehrere Kanäle in einem Aufruf anlegen",
    body={
        "channels": "[ … ] — Liste von Kanal-Objekten wie bei POST /api/v1/channels",
        "category": "gemeinsame Kategorie für alle (kann pro Kanal überschrieben werden)",
        "stop_on_error": "bool, Standard false — bei false werden Fehler pro Kanal gemeldet",
        "delay_ms": "int, Pause zwischen den Aufrufen (Standard 350, schützt vor Rate-Limits)",
        "reason": "str",
    },
    description="Praktisch für 'lege 12 Kanäle in 4 Kategorien an'. Jeder Eintrag darf "
                "einen eigenen ``key`` haben; die Antwort mappt ``key → neue ID``.",
)
async def create_channels_bulk(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    specs = data.get("channels")
    if not isinstance(specs, list) or not specs:
        raise ApiError.bad_request(
            "'channels' muss eine nicht-leere Liste von Kanal-Objekten sein.",
            hint='Beispiel: {"channels": [{"name": "chat", "type": "text"}, {"name": "Voice", "type": "voice"}]}',
        )
    if len(specs) > 100:
        raise ApiError.bad_request("channels: maximal 100 Einträge pro Aufruf.")

    guild = ctx.guild
    shared_category = data.get("category")
    stop_on_error = bool(parse_bool(data.get("stop_on_error"), field="stop_on_error", default=False))
    delay = parse_int(data.get("delay_ms"), field="delay_ms", default=350, minimum=0, maximum=5000) or 0
    base_reason = ctx.reason(data, default="Kanäle per Bulk-API angelegt (Arena AI)")

    keys: Dict[str, Any] = {}
    created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            errors.append({"index": index, "error": "Eintrag muss ein Objekt sein."})
            if stop_on_error:
                break
            continue
        payload = dict(spec)
        if shared_category is not None and not any(
            k in payload for k in ("category", "category_id", "parent_id")
        ):
            payload["category"] = shared_category
        if payload.get("type") in (None, "category") and "category" in payload:
            payload.pop("category", None)
        try:
            channel = await create_channel(guild, payload, keys=keys,
                                           reason=payload.get("reason") or base_reason, index=index)
        except ApiError as exc:
            errors.append({"index": index, "name": payload.get("name"), **exc.to_dict()["error"]})
            if stop_on_error:
                break
            continue
        keys[str(payload.get("key") or payload.get("name") or channel.id)] = channel
        created.append({"index": index, "key": payload.get("key"), "channel": serialize_channel(channel)})
        if delay:
            await asyncio.sleep(delay / 1000)

    await ctx.settle(0.7)
    return {
        "created_count": len(created),
        "failed_count": len(errors),
        "created": created,
        "errors": errors or None,
        "keys": {k: sf(getattr(v, "id", v)) for k, v in keys.items()},
        "guild_channels_total": len(guild.channels),
    }


@route(
    "POST", "/api/v1/channels/bulk-delete", scope="danger", tags=("channels",),
    summary="Mehrere Kanäle löschen (destruktiv)",
    body={
        "channel_ids": '["ID", …] — Pflicht',
        "names": '["chat", "memes"] — alternativ per Name',
        "confirm": "muss true sein",
        "reason": "str",
    },
    description="**Löscht unwiderruflich.** Erfordert ``confirm: true``.",
)
async def bulk_delete_channels(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    if not parse_bool(data.get("confirm"), field="confirm", default=False):
        raise ApiError.bad_request(
            "Löschen ist destruktiv und braucht eine Bestätigung.",
            hint='Sende {"channel_ids": ["…"], "confirm": true}.',
            code="CONFIRM_REQUIRED",
        )
    guild = ctx.guild
    targets: List[Any] = []
    for raw in data.get("channel_ids") or []:
        targets.append(resolve_channel_by_ref(guild, raw, field="channel_ids"))
    for raw in data.get("names") or []:
        targets.append(resolve_channel_by_ref(guild, raw, field="names"))
    if not targets:
        raise ApiError.bad_request("Weder 'channel_ids' noch 'names' angegeben.")
    if len(targets) > 50:
        raise ApiError.bad_request("Maximal 50 Kanäle pro Aufruf.")

    reason = ctx.reason(data, default="Kanäle gelöscht (Arena AI)")
    deleted: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for channel in targets:
        try:
            await guard(channel.delete(reason=reason), action=f"Kanal '{channel.name}' löschen")
            deleted.append({"id": sf(channel.id), "name": channel.name,
                            "type": str(getattr(channel.type, "name", "?"))})
        except ApiError as exc:
            errors.append({"id": sf(channel.id), "name": getattr(channel, "name", None),
                           **exc.to_dict()["error"]})
        await asyncio.sleep(0.35)

    await ctx.settle(0.8)
    return {"deleted_count": len(deleted), "failed_count": len(errors),
            "deleted": deleted, "errors": errors or None}


# ─────────────────────────────────────────────────────────────────────────────
#  Bearbeiten / Sortieren / Löschen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "PATCH", "/api/v1/channels/positions", scope="write", tags=("channels",),
    summary="Kanäle/Kategorien neu sortieren",
    body={
        "positions": '[{"id": "Kanal-ID", "position": 0, "parent_id": "Kategorie-ID oder null", '
                     '"lock_permissions": bool}]',
        "reason": "str",
    },
    description="Discord verlangt für eine Sortierung immer die komplette betroffene Ebene. "
                "Am einfachsten: erst GET /api/v1/channels/tree, dann die gewünschte "
                "Reihenfolge als Liste senden.",
)
async def move_channels(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    raw = data.get("positions")
    if not isinstance(raw, list) or not raw:
        raise ApiError.bad_request(
            "'positions' muss eine nicht-leere Liste sein.",
            hint='Beispiel: {"positions": [{"id": "123…", "position": 0}, {"id": "456…", "position": 1}]}',
        )
    guild = ctx.guild
    payload: List[Dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ApiError.bad_request(f"positions[{index}]: erwartet ein Objekt.")
        channel = resolve_channel_by_ref(
            guild, entry.get("id") or entry.get("channel"), field=f"positions[{index}].id"
        )
        item: Dict[str, Any] = {
            "id": channel.id,
            "position": parse_int(entry.get("position"), field=f"positions[{index}].position",
                                  minimum=0, maximum=999),
        }
        if "parent_id" in entry or "category" in entry:
            category = resolve_category(guild, entry.get("parent_id", entry.get("category")),
                                        field=f"positions[{index}].parent_id")
            item["parent_id"] = category.id if category else None
        if "lock_permissions" in entry:
            item["lock_permissions"] = bool(parse_bool(entry["lock_permissions"],
                                                       field=f"positions[{index}].lock_permissions"))
        payload.append({k: v for k, v in item.items() if v is not None})

    reason = ctx.reason(data, default="Kanalreihenfolge geändert (Arena AI)")
    # discord.py 2.x bietet dafür keine öffentliche Methode — der offizielle
    # REST-Endpoint ist PATCH /guilds/{id}/channels.
    await guard(
        guild._state.http.bulk_channel_update(guild.id, payload, reason=reason),  # noqa: SLF001
        action="Kanalpositionen ändern",
    )
    await ctx.settle(0.7)
    return {"moved": len(payload), "positions": payload, "tree": serialize_channel_tree(guild)}


@route(
    "PATCH", "/api/v1/channels/{channel_id}", scope="write", tags=("channels",),
    summary="Kanal bearbeiten",
    body={
        "name": "str", "topic": "str", "nsfw": "bool", "slowmode_delay": "int",
        "position": "int", "category": "ID/Name/null", "overwrites": "[…]",
        "bitrate": "int", "user_limit": "int", "rtc_region": "str",
        "video_quality_mode": "'auto'|'full'", "status": "str (Voice)",
        "default_auto_archive_duration": "60|1440|4320|10080",
        "default_thread_slowmode_delay": "int", "available_tags": "[…]",
        "default_sort_order": "'latest_activity'|'creation_date'",
        "default_layout": "'not_set'|'list_view'|'gallery_view'",
        "archived": "bool (Thread)", "locked": "bool (Thread)", "invitable": "bool (Thread)",
        "applied_tags": '["Tag-ID"] (Forum-Post)', "reason": "str",
    },
)
async def patch_channel(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    spec = await ctx.body()
    result = await edit_channel(ctx, channel, spec, reason=ctx.reason(spec, default=None))
    return {
        "changed": result["changes"],
        "channel": serialize_channel(result["channel"]),
        "id": sf(result["channel"].id),
    }


@route(
    "DELETE", "/api/v1/channels/{channel_id}", scope="danger", tags=("channels",),
    summary="Kanal löschen",
    query={"confirm": "muss true sein", "reason": "str"},
    description="**Unwiderruflich.** Erfordert ``?confirm=true`` oder ``{\"confirm\": true}`` im Body.",
)
async def delete_channel(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    confirmed = ctx.q_bool("confirm", False)
    try:
        data = await ctx.body()
        confirmed = confirmed or bool(parse_bool(data.get("confirm"), field="confirm", default=False))
        reason = ctx.reason(data, default=None)
    except ApiError:
        reason = None
    if not confirmed:
        raise ApiError.bad_request(
            f"Kanal '{channel.name}' löschen? Das ist unwiderruflich.",
            hint="Bestätige mit ?confirm=true (oder {\"confirm\": true} im Body).",
            code="CONFIRM_REQUIRED",
        )
    info = {"id": sf(channel.id), "name": channel.name, "type_name": str(getattr(channel.type, "name", "?"))}
    await guard(channel.delete(reason=reason or f"Kanal '{channel.name}' gelöscht (Arena AI)"),
                action=f"Kanal '{channel.name}' löschen")
    await ctx.settle(0.6)
    return {"deleted": info, "channels_remaining": len(ctx.guild.channels)}


# ─────────────────────────────────────────────────────────────────────────────
#  Kanal-Berechtigungen (Overwrites)
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "PUT", "/api/v1/channels/{channel_id}/permissions/{target_id}", scope="write",
    tags=("channels", "permissions"),
    summary="Berechtigung für Rolle/Mitglied auf einem Kanal setzen",
    body={
        "allow": '["send_messages", "view_channel"]',
        "deny": '["attach_files"]',
        "reason": "str",
    },
    description="``target_id`` darf sein: ``@everyone`` (URL-kodiert ``%40everyone``), "
                "``everyone``, ``role:<id>``, ``<Rollen-ID>``, ``member:<id>``, ``<@id>`` "
                "oder ein Rollen-/Mitgliedsname.",
)
async def put_permission(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    target_raw = ctx.path("target_id")
    guild = ctx.guild
    target = resolve_target(guild, target_raw.replace("%40", "@"))
    if not isinstance(target, (discord.Role, discord.Member)):
        raise ApiError.bad_request("Ziel muss eine Rolle oder ein Mitglied sein.")

    data = await ctx.body()
    overwrite = discord.PermissionOverwrite()
    existing = channel.overwrites_for(target)
    if parse_bool(data.get("merge", True), field="merge", default=True) and existing:
        for name, value in existing:
            if value is not None:
                setattr(overwrite, name, value)

    from ...util import normalize_permission

    for name in data.get("allow") or []:
        setattr(overwrite, normalize_permission(name), True)
    for name in data.get("deny") or []:
        setattr(overwrite, normalize_permission(name), False)
    for key, value in data.items():
        if key in {"allow", "deny", "merge", "reason"} or value is None:
            continue
        setattr(overwrite, normalize_permission(key), bool(value))

    reason = ctx.reason(data, default=f"Kanalberechtigung für {getattr(target, 'name', target.id)} (Arena AI)")
    await guard(
        channel.set_permissions(target, overwrite=overwrite, reason=reason),
        action=f"Berechtigung auf '{channel.name}' setzen",
    )
    await ctx.settle(0.4)
    allow, deny = overwrite.pair()
    from ...util import permission_names

    return {
        "channel": serialize_channel(channel),
        "target": {"id": sf(target.id), "name": getattr(target, "name", None),
                   "type": "role" if isinstance(target, discord.Role) else "member"},
        "allow": permission_names(allow),
        "deny": permission_names(deny),
    }


@route(
    "DELETE", "/api/v1/channels/{channel_id}/permissions/{target_id}", scope="write",
    tags=("channels", "permissions"),
    summary="Kanalberechtigung entfernen (auf Kategorie-/Server-Standard zurücksetzen)",
    query={"reason": "str"},
)
async def delete_permission(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    guild = ctx.guild
    target = resolve_target(guild, ctx.path("target_id").replace("%40", "@"))
    reason = ctx.reason(default="Kanalberechtigung entfernt (Arena AI)")
    await guard(
        channel.set_permissions(target, overwrite=None, reason=reason),
        action=f"Berechtigung auf '{channel.name}' entfernen",
    )
    await ctx.settle(0.4)
    return {"channel": serialize_channel(channel),
            "removed_target": {"id": sf(target.id), "name": getattr(target, "name", None)}}


@route(
    "POST", "/api/v1/channels/{channel_id}/lock", scope="manage", tags=("channels", "permissions"),
    summary="Kanal sperren (privat machen)",
    body={
        "allow_roles": '["Rollen-ID oder Name"] — diese Rollen behalten Zugriff',
        "deny": '["view_channel", "send_messages"] — was @everyone verliert',
        "reason": "str",
    },
    description="Setzt ``@everyone`` auf ``deny`` (Standard: view_channel + send_messages) "
                "und erlaubt dem Bot sowie optional weiteren Rollen den Zugriff.",
)
async def lock_channel(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    guild = ctx.guild
    deny_names = data.get("deny") or ["view_channel", "send_messages"]
    from ...util import normalize_permission

    overwrite = discord.PermissionOverwrite(**{normalize_permission(n): False for n in deny_names})
    kwargs: Dict[str, Any] = {
        guild.default_role: overwrite,
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True,
            read_message_history=True, manage_messages=True, manage_roles=True,
        ),
    }
    for raw in data.get("allow_roles") or []:
        role = resolve_target(guild, raw)
        if isinstance(role, discord.Role):
            kwargs[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                       read_message_history=True)
    reason = ctx.reason(data, default=f"Kanal '{channel.name}' gesperrt (Arena AI)")
    await guard(channel.edit(overwrites=kwargs, reason=reason), action=f"Kanal '{channel.name}' sperren")
    await ctx.settle(0.5)
    return {"locked": True, "channel": serialize_channel(channel)}


@route(
    "POST", "/api/v1/channels/{channel_id}/unlock", scope="manage", tags=("channels", "permissions"),
    summary="Kanal entsperren (für @everyone sichtbar machen)",
    body={"allow": '["view_channel","send_messages","read_message_history"]', "reason": "str"},
)
async def unlock_channel(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    from ...util import normalize_permission

    allow = data.get("allow") or ["view_channel", "send_messages", "read_message_history"]
    overwrite = discord.PermissionOverwrite(**{normalize_permission(n): True for n in allow})
    reason = ctx.reason(data, default=f"Kanal '{channel.name}' entsperrt (Arena AI)")
    await guard(channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=reason),
                action=f"Kanal '{channel.name}' entsperren")
    await ctx.settle(0.4)
    return {"locked": False, "channel": serialize_channel(channel)}


# ─────────────────────────────────────────────────────────────────────────────
#  Threads
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/channels/{channel_id}/threads", scope="read", tags=("channels", "threads"),
    summary="Threads eines Kanals (aktiv + archiviert)",
    query={"limit": "int für archivierte Threads, Standard 25", "private": "true = private Threads"},
)
async def list_threads(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    limit = ctx.q_int("limit", 25, minimum=1, maximum=100) or 25
    private = ctx.q_bool("private", False)

    active = [serialize_channel(t) for t in getattr(channel, "threads", [])]
    archived: List[Any] = []
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        try:
            if isinstance(channel, discord.ForumChannel):
                async for thread in channel.archived_threads(limit=limit):
                    archived.append(serialize_channel(thread))
            elif private:
                async for thread in channel.archived_threads(limit=limit, private=True):
                    archived.append(serialize_channel(thread))
            else:
                async for thread in channel.archived_threads(limit=limit, private=False):
                    archived.append(serialize_channel(thread))
        except discord.Forbidden:
            archived = [{"_error": "READ_MESSAGE_HISTORY fehlt — archivierte Threads nicht lesbar."}]
        except discord.HTTPException as exc:
            archived = [{"_error": str(exc)}]
    return {"channel": {"id": sf(channel.id), "name": getattr(channel, "name", None)},
            "active_count": len(active), "archived_count": len(archived),
            "active": active, "archived": archived}


@route(
    "POST", "/api/v1/channels/{channel_id}/threads", scope="write", tags=("channels", "threads"),
    summary="Thread anlegen",
    body={
        "name": "str (Pflicht)",
        "type": "'public'|'private' (Text) — Forum-Posts sind immer öffentlich",
        "auto_archive_duration": "60|1440|4320|10080 Minuten",
        "slowmode_delay": "int Sekunden",
        "invitable": "bool (private Threads)",
        "message_id": "Nachricht, auf die der Thread antwortet (Text-Kanäle)",
        "content": "Startnachricht (Forum-Posts)",
        "applied_tags": '["Tag-ID"] (Forum-Posts)',
        "reason": "str",
    },
)
async def create_thread(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    name = parse_str(data.get("name"), field="name", min_length=1, max_length=100, allow_empty=False)
    if not name:
        raise ApiError.bad_request("'name' fehlt.")

    auto_archive = parse_int(
        data.get("auto_archive_duration"), field="auto_archive_duration", default=1440
    )
    if auto_archive not in (60, 1440, 4320, 10080):
        raise ApiError.bad_request(
            f"auto_archive_duration {auto_archive} ist ungültig.",
            hint="Erlaubt: 60, 1440, 4320, 10080 Minuten.",
            code="AUTO_ARCHIVE_INVALID",
        )
    reason = ctx.reason(data, default=f"Thread '{name}' erstellt (Arena AI)")

    if isinstance(channel, discord.ForumChannel):
        kwargs: Dict[str, Any] = {
            "name": name,
            "auto_archive_duration": auto_archive,
            "reason": reason,
        }
        content = parse_str(data.get("content"), field="content", max_length=2000)
        if content:
            kwargs["content"] = content
        slowmode = parse_int(data.get("slowmode_delay"), field="slowmode_delay", minimum=0, maximum=21600)
        if slowmode is not None:
            kwargs["slowmode_delay"] = slowmode
        tags = data.get("applied_tags")
        if tags:
            available = {t.id: t for t in channel.available_tags}
            chosen = []
            for item in tags:
                tag_id = as_id(item, field="applied_tags")
                if tag_id not in available:
                    raise ApiError.not_found(
                        f"Forum-Tag {tag_id} existiert nicht.",
                        hint="GET /api/v1/channels/{forum_id} zeigt available_tags.",
                        code="FORUM_TAG_NOT_FOUND",
                    )
                chosen.append(available[tag_id])
            kwargs["applied_tags"] = chosen
        thread = await guard(channel.create_thread(**kwargs), action=f"Forum-Post '{name}' anlegen")
    else:
        if not isinstance(channel, discord.TextChannel):
            raise ApiError.bad_request(
                f"'{getattr(channel, 'name', channel.id)}' unterstützt keine Threads.",
                hint="Threads gibt es nur in Text- und Forum-Kanälen.",
                code="CHANNEL_TYPE_MISMATCH",
            )
        thread_type: Optional[discord.ChannelType] = None
        if data.get("type"):
            raw = str(data["type"]).strip().lower()
            if raw in {"private", "privat"}:
                thread_type = discord.ChannelType.private_thread
            elif raw in {"public", "öffentlich", "oeffentlich"}:
                thread_type = discord.ChannelType.public_thread
            else:
                raise ApiError.bad_request("type muss 'public' oder 'private' sein.")

        message_id = data.get("message_id")
        message = None
        if message_id:
            message = channel.get_partial_message(as_id(message_id, field="message_id"))

        kwargs = {
            "name": name,
            "auto_archive_duration": auto_archive,
            "reason": reason,
            "type": thread_type,
            "invitable": bool(parse_bool(data.get("invitable"), field="invitable", default=True)),
            "slowmode_delay": parse_int(data.get("slowmode_delay"), field="slowmode_delay",
                                        minimum=0, maximum=21600),
        }
        if message is not None:
            kwargs["message"] = message
        thread = await guard(channel.create_thread(**kwargs), action=f"Thread '{name}' anlegen")

    await ctx.settle(0.5)
    return {"created": serialize_channel(thread), "id": sf(thread.id)}


# ─────────────────────────────────────────────────────────────────────────────
#  Webhooks & Sonstiges
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/channels/{channel_id}/webhooks", scope="read", tags=("channels", "webhooks"),
    summary="Webhooks eines Kanals",
)
async def list_webhooks(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    if not hasattr(channel, "webhooks"):
        raise ApiError.bad_request(
            f"'{getattr(channel, 'name', channel.id)}' unterstützt keine Webhooks.",
            code="CHANNEL_TYPE_MISMATCH",
        )
    hooks = await guard(channel.webhooks(), action="Webhooks laden")
    return {"count": len(hooks), "webhooks": [serialize_webhook(h) for h in hooks]}


@route(
    "POST", "/api/v1/channels/{channel_id}/webhooks", scope="manage", tags=("channels", "webhooks"),
    summary="Webhook anlegen",
    body={"name": "str (Pflicht)", "avatar": "URL/Data-URI (optional)", "reason": "str"},
)
async def create_webhook(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    name = parse_str(data.get("name"), field="name", min_length=1, max_length=80, allow_empty=False)
    if not name:
        raise ApiError.bad_request("'name' fehlt.")
    if not hasattr(channel, "create_webhook"):
        raise ApiError.bad_request(f"'{channel.name}' unterstützt keine Webhooks.",
                                   code="CHANNEL_TYPE_MISMATCH")
    kwargs: Dict[str, Any] = {"reason": ctx.reason(data, default=f"Webhook '{name}' (Arena AI)")}
    if data.get("avatar"):
        from ..images import resolve_image

        kwargs["avatar"] = await resolve_image(data["avatar"], session=ctx.http_session(),
                                               field="avatar")
    hook = await guard(channel.create_webhook(name=name, **kwargs), action=f"Webhook '{name}' anlegen")
    await ctx.settle(0.4)
    result = serialize_webhook(hook) or {}
    # Der Webhook-Token wird EINMALIG zurückgegeben — danach ist er nicht mehr abrufbar.
    result["webhook_token"] = hook.token
    result["webhook_url"] = hook.url
    result["warning"] = "webhook_token nur einmal sichtbar. Wer ihn hat, darf in diesem " \
                        "Kanal als dieser Webhook posten."
    return result


@route(
    "DELETE", "/api/v1/webhooks/{webhook_id}", scope="manage", tags=("webhooks",),
    summary="Webhook löschen",
    query={"reason": "str"},
)
async def delete_webhook(ctx: Ctx) -> Dict[str, Any]:
    webhook_id = ctx.path_id("webhook_id")
    hook = await guard(ctx.client.fetch_webhook(webhook_id), action="Webhook laden")
    if getattr(hook, "guild_id", None) and hook.guild_id != ctx.guild.id:
        raise ApiError.forbidden("Dieser Webhook gehört zu einem anderen Server.", code="CROSS_GUILD")
    await guard(hook.delete(reason=ctx.reason(default="Webhook gelöscht (Arena AI)")),
                action="Webhook löschen")
    return {"deleted": {"id": sf(webhook_id), "name": hook.name}}


@route(
    "POST", "/api/v1/channels/{channel_id}/typing", scope="write", tags=("channels",),
    summary="'schreibt …'-Indikator auslösen",
    description="Nützlich, um lange Aktionen anzukündigen. Hält ~10 Sekunden.",
)
async def trigger_typing(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    await guard(channel.trigger_typing(), action="Typing-Indikator")
    return {"typing": True, "channel_id": sf(channel.id)}


@route(
    "POST", "/api/v1/channels/sync", scope="write", tags=("channels", "permissions"),
    summary="Kanalberechtigungen mit der Kategorie synchronisieren",
    body={"channel_ids": '["ID", …] oder "all_in_category": "Kategorie-ID"', "reason": "str"},
)
async def sync_permissions(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    guild = ctx.guild
    targets: List[Any] = []
    for raw in data.get("channel_ids") or []:
        targets.append(resolve_channel_by_ref(guild, raw, field="channel_ids"))
    category_ref = data.get("all_in_category") or data.get("category")
    if category_ref:
        category = resolve_category(guild, category_ref, field="all_in_category")
        if category is None:
            raise ApiError.bad_request("all_in_category: Kategorie nicht gefunden.")
        targets.extend(c for c in category.channels if not isinstance(c, discord.CategoryChannel))
    if not targets:
        raise ApiError.bad_request("'channel_ids' oder 'all_in_category' angeben.")

    reason = ctx.reason(data, default="Berechtigungen mit Kategorie synchronisiert (Arena AI)")
    synced: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for channel in targets:
        category = getattr(channel, "category", None)
        if category is None:
            errors.append({"id": sf(channel.id), "name": channel.name, "message": "Keine Kategorie."})
            continue
        try:
            await guard(
                channel.edit(overwrites=dict(category.overwrites), reason=reason),
                action=f"Berechtigungen von '{channel.name}' synchronisieren",
            )
            synced.append({"id": sf(channel.id), "name": channel.name, "category": category.name})
        except ApiError as exc:
            errors.append({"id": sf(channel.id), "name": channel.name, **exc.to_dict()["error"]})
        await asyncio.sleep(0.3)

    await ctx.settle(0.6)
    return {"synced_count": len(synced), "failed_count": len(errors),
            "synced": synced, "errors": errors or None}
