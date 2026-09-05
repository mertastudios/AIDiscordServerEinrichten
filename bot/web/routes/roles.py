"""
Rollen-Verwaltung plus eine vollständige Permission-Referenz.

``GET /api/v1/permissions`` ist bewusst dabei: Eine KI, die Discord-Rollen
bauen soll, braucht die exakten Flag-Namen. Statt zu raten (und 50035-Fehler
zu produzieren) kann sie die kanonische Liste samt deutscher Erklärung holen.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import discord
from discord.utils import MISSING

from ...serializers import serialize_member, serialize_role
from ...util import (
    ApiError,
    parse_bool,
    parse_int,
    parse_permissions,
    parse_str,
    resolve_role,
    sf,
)
from ..context import Ctx, guard
from ..images import resolve_image
from ..registry import route

#: Kanonische Permission-Namen mit deutscher Erklärung.
PERMISSION_DOCS: Dict[str, str] = {
    "administrator": "Alle Rechte — überschreibt alles, auch Kanal-Overwrites.",
    "view_audit_log": "Audit-Log des Servers einsehen.",
    "view_guild_insights": "Server-Insights (Analysen) einsehen.",
    "manage_guild": "Servereinstellungen ändern, Invites und Integrationen verwalten.",
    "manage_roles": "Rollen anlegen/löschen/zuweisen (nur unterhalb der eigenen Top-Rolle).",
    "manage_channels": "Kanäle anlegen, bearbeiten, löschen.",
    "manage_webhooks": "Webhooks verwalten.",
    "manage_expressions": "Emojis, Sticker und Soundboard-Sounds verwalten.",
    "manage_events": "Events verwalten (auch fremde).",
    "create_events": "Events erstellen.",
    "kick_members": "Mitglieder kicken.",
    "ban_members": "Mitglieder bannen.",
    "moderate_members": "Timeouts vergeben (ehemals 'timeout_members').",
    "manage_nicknames": "Nicknames anderer ändern.",
    "change_nickname": "Eigenen Nickname ändern.",
    "manage_threads": "Threads verwalten/löschen.",
    "create_public_threads": "Öffentliche Threads erstellen.",
    "create_private_threads": "Private Threads erstellen.",
    "send_messages_in_threads": "In Threads schreiben.",
    "view_channel": "Kanal sehen (Text & Voice). Alias: read_messages.",
    "send_messages": "Nachrichten in Textkanälen senden.",
    "send_tts_messages": "TTS-Nachrichten senden.",
    "manage_messages": "Fremde Nachrichten löschen/anheften.",
    "embed_links": "Links werden als Embed angezeigt.",
    "attach_files": "Dateien anhängen.",
    "add_reactions": "Reaktionen hinzufügen.",
    "read_message_history": "Ältere Nachrichten lesen.",
    "mention_everyone": "@everyone, @here und alle Rollen erwähnen.",
    "use_external_emojis": "Serverfremde Emojis nutzen.",
    "use_external_stickers": "Serverfremde Sticker nutzen.",
    "use_external_sounds": "Serverfremde Soundboard-Sounds nutzen.",
    "use_external_apps": "Externe Apps in Voice-Kanälen nutzen.",
    "use_application_commands": "Slash-Commands nutzen.",
    "use_embedded_activities": "Aktivitäten (z. B. Watch Together) in Voice starten.",
    "use_soundboard": "Soundboard nutzen.",
    "send_voice_messages": "Sprachnachrichten senden.",
    "send_polls": "Umfragen erstellen.",
    "pin_messages": "Nachrichten anheften.",
    "create_instant_invite": "Invites erstellen.",
    "connect": "Voice-Kanal betreten.",
    "speak": "In Voice sprechen.",
    "stream": "Video/Screen-Share in Voice.",
    "mute_members": "Andere stummschalten.",
    "deafen_members": "Andere taubschalten.",
    "move_members": "Andere zwischen Voice-Kanälen verschieben.",
    "use_voice_activation": "Sprachaktivierung nutzen (trotz Server-Mute).",
    "priority_speaker": "Prioritätssprecher in Voice.",
    "request_to_speak": "Auf einer Bühne um Sprechen bitten.",
    "set_voice_channel_status": "Voice-Kanal-Status setzen.",
    "bypass_slowmode": "Slowmode ignorieren.",
    "view_creator_monetization_analytics": "Monetarisierungs-Analysen einsehen.",
}

#: Sinnvolle Rollen-Vorlagen — damit die KI nicht bei null anfängt.
ROLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "admin": {
        "name": "👑 Admin", "color": "#E74C3C", "hoist": True, "mentionable": False,
        "permissions": ["administrator"],
    },
    "moderator": {
        "name": "🛡️ Moderator", "color": "#3498DB", "hoist": True, "mentionable": True,
        "permissions": [
            "kick_members", "ban_members", "moderate_members", "manage_messages",
            "manage_nicknames", "view_audit_log", "manage_threads", "mute_members",
            "deafen_members", "move_members", "view_channel", "send_messages",
            "read_message_history", "embed_links", "attach_files", "add_reactions",
            "connect", "speak", "stream", "use_voice_activation", "manage_channels",
        ],
    },
    "support": {
        "name": "🎧 Support", "color": "#1ABC9C", "hoist": True, "mentionable": True,
        "permissions": [
            "moderate_members", "manage_messages", "view_audit_log", "view_channel",
            "send_messages", "read_message_history", "embed_links", "attach_files",
            "add_reactions", "connect", "speak", "manage_threads",
        ],
    },
    "mitglied": {
        "name": "Mitglied", "color": "#2ECC71", "hoist": False, "mentionable": False,
        "permissions": [
            "view_channel", "send_messages", "read_message_history", "embed_links",
            "attach_files", "add_reactions", "change_nickname", "create_instant_invite",
            "connect", "speak", "stream", "use_voice_activation",
            "use_application_commands", "create_public_threads",
            "send_messages_in_threads", "use_external_emojis", "use_external_stickers",
            "send_voice_messages", "send_polls", "pin_messages",
        ],
    },
    "bot": {
        "name": "🤖 Bot", "color": "#95A5A6", "hoist": True, "mentionable": False,
        "permissions": [
            "view_channel", "send_messages", "read_message_history", "embed_links",
            "attach_files", "add_reactions", "manage_messages", "manage_threads",
            "connect", "speak", "use_application_commands",
        ],
    },
    "muted": {
        "name": "🔇 Muted", "color": "#7F8C8D", "hoist": False, "mentionable": False,
        "permissions": ["view_channel", "read_message_history"],
    },
    "vip": {
        "name": "💎 VIP", "color": "#F1C40F", "hoist": True, "mentionable": True,
        "permissions": [
            "view_channel", "send_messages", "read_message_history", "embed_links",
            "attach_files", "add_reactions", "use_external_emojis",
            "use_external_stickers", "connect", "speak", "stream", "priority_speaker",
        ],
    },
    "gast": {
        "name": "👋 Gast", "color": "#BDC3C7", "hoist": False, "mentionable": False,
        "permissions": ["view_channel", "read_message_history"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  Referenz
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/permissions", public=False, scope="read", tags=("roles", "referenz"),
    summary="Alle gültigen Permission-Namen mit deutscher Erklärung",
    query={"preset": "Rollen-Vorlage zeigen (admin, moderator, support, mitglied, bot, muted, vip, gast)"},
    description="Nutze exakt diese Namen in 'permissions' und 'overwrites'. Aliase "
                "(z. B. 'read_messages', 'timeout_members') werden ebenfalls akzeptiert.",
)
async def list_permissions(ctx: Ctx) -> Dict[str, Any]:
    preset = ctx.q("preset")
    data: Dict[str, Any] = {
        "count": len(PERMISSION_DOCS),
        "permissions": [{"name": name, "description": description}
                        for name, description in sorted(PERMISSION_DOCS.items())],
        "presets": sorted(ROLE_PRESETS),
        "note": "Überall, wo Permissions erwartet werden, gilt: Liste von Namen, "
                '"all", "none" oder eine Bitmaske als Zahl.',
    }
    if preset:
        key = preset.strip().lower()
        if key not in ROLE_PRESETS:
            raise ApiError.not_found(
                f"Rollen-Vorlage '{preset}' gibt es nicht.",
                hint="Verfügbar: " + ", ".join(sorted(ROLE_PRESETS)),
                code="PRESET_NOT_FOUND",
            )
        data["preset"] = {"key": key, **ROLE_PRESETS[key]}
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  Lesen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/roles", scope="read", tags=("roles",),
    summary="Alle Rollen (absteigend nach Position)",
    query={
        "with_member_count": "true — zählt Mitglieder pro Rolle (1 API-Call)",
        "bot_top_role_position": "wird immer mitgeliefert",
    },
)
async def list_roles(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    with_counts = ctx.q_bool("with_member_count", False)

    counts: Dict[int, int] = {}
    if with_counts:
        try:
            raw = await guard(
                guild._state.http.get_role_member_counts(guild.id),  # noqa: SLF001
                action="Rollen-Mitgliederzahlen laden",
            )
            counts = {int(key): int(value) for key, value in raw.items()}
        except ApiError:
            counts = {}

    me = guild.me
    bot_position = me.top_role.position if me else 0
    roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)
    items: List[Dict[str, Any]] = []
    for role in roles:
        entry = serialize_role(role) or {}
        entry["above_bot"] = role.position > bot_position
        entry["editable_by_bot"] = (
            not role.is_default()
            and not role.managed
            and role.position < bot_position
        )
        if counts:
            entry["member_count"] = counts.get(role.id)
        items.append(entry)

    return {
        "count": len(items),
        "bot_top_role": {"name": me.top_role.name, "position": bot_position} if me else None,
        "note": "Der Bot kann nur Rollen mit 'editable_by_bot': true ändern. "
                "Rollen über seiner Top-Rolle sind tabu (Discord-Fehler 50013/20012).",
        "roles": items,
        "presets_available": sorted(ROLE_PRESETS),
    }


@route(
    "GET", "/api/v1/roles/{role_id}", scope="read", tags=("roles",),
    summary="Einzelne Rolle",
    query={"members": "int — diese vielen Mitglieder der Rolle mitsenden (Standard 0)"},
)
async def get_role(ctx: Ctx) -> Dict[str, Any]:
    role = resolve_role(ctx.guild, ctx.path("role_id"))
    limit = ctx.q_int("members", 0, minimum=0, maximum=200) or 0
    data = serialize_role(role) or {}
    if limit:
        holders = [m for m in ctx.guild.members if role in m.roles][:limit]
        data["members"] = [serialize_member(m) for m in holders]
        data["members_returned"] = len(holders)
    return data


@route(
    "GET", "/api/v1/roles/{role_id}/members", scope="read", tags=("roles", "members"),
    summary="Mitglieder einer Rolle",
    query={"limit": "int, Standard 100 (max. 1000)"},
)
async def role_members(ctx: Ctx) -> Dict[str, Any]:
    role = resolve_role(ctx.guild, ctx.path("role_id"))
    limit = ctx.q_int("limit", 100, minimum=1, maximum=1000) or 100
    holders = [m for m in ctx.guild.members if role in m.roles]
    total = len(holders)
    return {
        "role": {"id": sf(role.id), "name": role.name, "position": role.position},
        "total_cached": total,
        "returned": min(total, limit),
        "note": "Zählt nur zwischengespeicherte Mitglieder. Vollständige Liste: "
                "GET /api/v1/members?limit=1000 und selbst filtern.",
        "members": [serialize_member(m) for m in holders[:limit]],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Anlegen
# ─────────────────────────────────────────────────────────────────────────────


def _role_kwargs_from_spec(guild: discord.Guild, spec: Dict[str, Any], *, index_label: str = "") -> Dict[str, Any]:
    where = f"{index_label}." if index_label else ""
    kwargs: Dict[str, Any] = {}

    preset_key = spec.get("preset")
    base: Dict[str, Any] = {}
    if preset_key:
        key = str(preset_key).strip().lower()
        if key not in ROLE_PRESETS:
            raise ApiError.bad_request(
                f"{where}preset '{preset_key}' existiert nicht.",
                hint="Verfügbar: " + ", ".join(sorted(ROLE_PRESETS)),
                code="PRESET_NOT_FOUND",
            )
        base = dict(ROLE_PRESETS[key])
    merged = {**base, **{k: v for k, v in spec.items() if v is not None}}

    name = parse_str(merged.get("name"), field=f"{where}name", min_length=1, max_length=100,
                     allow_empty=False)
    if not name:
        raise ApiError.bad_request(f"{where}name fehlt — jede Rolle braucht einen Namen.")
    kwargs["name"] = name

    color = merged.get("color", merged.get("colour"))
    if color is not None:
        from ...util import parse_color

        kwargs["color"] = parse_color(color, field=f"{where}color")

    if "hoist" in merged:
        kwargs["hoist"] = bool(parse_bool(merged["hoist"], field=f"{where}hoist"))
    if "mentionable" in merged:
        kwargs["mentionable"] = bool(parse_bool(merged["mentionable"], field=f"{where}mentionable"))

    permissions = merged.get("permissions")
    if permissions is not None:
        parsed = parse_permissions(permissions, field=f"{where}permissions")
        # Achtung: Permissions.none() ist "falsy" — deshalb explizit auf None prüfen,
        # sonst würde eine bewusst leere Rolle stillschweigend zu MISSING.
        kwargs["permissions"] = parsed if parsed is not None else MISSING

    icon = merged.get("icon", merged.get("display_icon"))
    if icon is not None:
        kwargs["_display_icon"] = icon  # wird async aufgelöst

    return kwargs


async def _finalize_role_kwargs(
    guild: discord.Guild, kwargs: Dict[str, Any], *, session
) -> Dict[str, Any]:
    """Löst ``_display_icon`` (URL/Emoji) in ``display_icon`` auf."""
    icon = kwargs.pop("_display_icon", None)
    if icon is None:
        return kwargs
    if isinstance(icon, str):
        text = icon.strip()
        if text.startswith(("http://", "https://", "data:")):
            kwargs["display_icon"] = await resolve_image(text, session=session, field="icon")
        elif text:
            kwargs["display_icon"] = text  # Unicode-Emoji
        else:
            kwargs["display_icon"] = None
    return kwargs


@route(
    "POST", "/api/v1/roles", scope="write", tags=("roles",),
    summary="Rolle anlegen",
    body={
        "name": "str (Pflicht)",
        "preset": "optionale Vorlage: admin|moderator|support|mitglied|bot|muted|vip|gast",
        "color": "'#RRGGBB' | Zahl | Farbname",
        "hoist": "bool — separat in der Mitgliederliste anzeigen",
        "mentionable": "bool",
        "permissions": '["view_channel", "send_messages"] | "all" | Bitmaske',
        "icon": "URL/Data-URI (Bild) oder Unicode-Emoji",
        "position": "int — Zielposition (wird nach dem Anlegen gesetzt)",
        "reason": "str",
    },
    examples=[
        {"body": {"name": "🛡️ Moderator", "preset": "moderator", "position": 5}},
        {"body": {"name": "VIP", "color": "#F1C40F", "hoist": True,
                  "permissions": ["view_channel", "send_messages", "priority_speaker"]}},
    ],
)
async def create_role_route(ctx: Ctx) -> Dict[str, Any]:
    spec = await ctx.body()
    guild = ctx.guild
    kwargs = _role_kwargs_from_spec(guild, dict(spec))
    kwargs = await _finalize_role_kwargs(guild, kwargs, session=ctx.http_session())
    kwargs["reason"] = ctx.reason(spec, default=f"Rolle '{kwargs['name']}' angelegt (Arena AI)")

    role = await guard(guild.create_role(**kwargs), action=f"Rolle '{kwargs['name']}' anlegen")
    await ctx.settle(0.5)

    position = parse_int(spec.get("position"), field="position", minimum=0, maximum=300)
    if position is not None:
        try:
            await guard(
                guild.edit_role_positions({role: position},
                                          reason=ctx.reason(spec, default="Rollenposition gesetzt")),
                action="Rollenposition setzen",
            )
            await ctx.settle(0.4)
            role = guild.get_role(role.id) or role
        except ApiError as exc:
            return {
                "created": serialize_role(role),
                "id": sf(role.id),
                "warning": f"Rolle angelegt, aber Position {position} konnte nicht gesetzt "
                           f"werden: {exc.message}",
            }

    return {"created": serialize_role(role), "id": sf(role.id)}


@route(
    "POST", "/api/v1/roles/bulk", scope="write", tags=("roles",),
    summary="Mehrere Rollen in einem Aufruf anlegen (inkl. Positionierung)",
    body={
        "roles": '[{"key":"admin","name":"👑 Admin","preset":"admin","position":10}, …]',
        "delay_ms": "int, Pause zwischen Aufrufen (Standard 350)",
        "stop_on_error": "bool, Standard false",
        "reason": "str",
    },
    description="Legt Rollen der Reihe nach an und setzt am Ende alle Positionen in "
                "**einem** API-Call (so verlangt es Discord). ``position``: höhere Zahl "
                "= weiter oben in der Hierarchie.",
)
async def create_roles_bulk(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    specs = data.get("roles")
    if not isinstance(specs, list) or not specs:
        raise ApiError.bad_request(
            "'roles' muss eine nicht-leere Liste sein.",
            hint='Beispiel: {"roles": [{"name": "Admin", "preset": "admin"}, {"name": "Mod", "preset": "moderator"}]}',
        )
    if len(specs) > 50:
        raise ApiError.bad_request("roles: maximal 50 pro Aufruf.")

    guild = ctx.guild
    delay = parse_int(data.get("delay_ms"), field="delay_ms", default=350, minimum=0, maximum=5000) or 0
    stop_on_error = bool(parse_bool(data.get("stop_on_error"), field="stop_on_error", default=False))
    base_reason = ctx.reason(data, default="Rollen per Bulk-API angelegt (Arena AI)")
    session = ctx.http_session()

    created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    keys: Dict[str, discord.Role] = {}
    positions: Dict[discord.Role, int] = {}

    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            errors.append({"index": index, "error": "Eintrag muss ein Objekt sein."})
            if stop_on_error:
                break
            continue
        try:
            kwargs = _role_kwargs_from_spec(guild, dict(spec), index_label=f"roles[{index}]")
            kwargs = await _finalize_role_kwargs(guild, kwargs, session=session)
            kwargs["reason"] = spec.get("reason") or base_reason
            role = await guard(guild.create_role(**kwargs),
                               action=f"Rolle '{kwargs['name']}' anlegen")
        except ApiError as exc:
            errors.append({"index": index, "name": spec.get("name"), **exc.to_dict()["error"]})
            if stop_on_error:
                break
            continue

        await ctx.settle(0.4)
        role = guild.get_role(role.id) or role
        key = str(spec.get("key") or role.name)
        keys[key] = role
        created.append({"index": index, "key": spec.get("key"), "role": serialize_role(role)})

        position = parse_int(spec.get("position"), field=f"roles[{index}].position",
                             minimum=0, maximum=300)
        if position is not None:
            positions[role] = position
        if delay:
            await asyncio.sleep(delay / 1000)

    position_note: Optional[str] = None
    if positions:
        try:
            await guard(
                guild.edit_role_positions(positions, reason=base_reason),
                action="Rollenpositionen setzen",
            )
            await ctx.settle(0.6)
            position_note = f"{len(positions)} Rolle(n) positioniert."
        except ApiError as exc:
            position_note = f"Positionen konnten nicht gesetzt werden: {exc.message}"

    return {
        "created_count": len(created),
        "failed_count": len(errors),
        "created": created,
        "errors": errors or None,
        "keys": {key: sf(role.id) for key, role in keys.items()},
        "positions": position_note,
        "hint": "GET /api/v1/roles zeigt die finale Hierarchie.",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Bearbeiten / Sortieren / Löschen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "PATCH", "/api/v1/roles/positions", scope="write", tags=("roles",),
    summary="Rollen-Hierarchie neu sortieren",
    body={
        "positions": '[{"id": "Rollen-ID", "position": 5}]  —  oder  {"admin": 10, "mod": 5} '
                     "(key→position, wenn Rollen per /setup angelegt wurden)",
        "reason": "str",
    },
    description="Höhere Zahl = weiter oben = mehr Macht. Die @everyone-Rolle ist immer 0 "
                "und nicht verschiebbar.",
)
async def move_roles(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    guild = ctx.guild
    mapping: Dict[discord.Role, int] = {}

    raw = data.get("positions")
    if isinstance(raw, dict):
        for key, value in raw.items():
            role = resolve_role(guild, key)
            mapping[role] = parse_int(value, field=f"positions.{key}", minimum=0, maximum=300) or 0
    elif isinstance(raw, list):
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ApiError.bad_request(f"positions[{index}]: erwartet Objekt.")
            role = resolve_role(guild, entry.get("id") or entry.get("role"))
            position = parse_int(entry.get("position"), field=f"positions[{index}].position",
                                 minimum=0, maximum=300)
            if position is None:
                raise ApiError.bad_request(f"positions[{index}].position fehlt.")
            mapping[role] = position
    else:
        raise ApiError.bad_request(
            "'positions' fehlt.",
            hint='Beispiel: {"positions": [{"id": "123…", "position": 3}]}',
        )

    if not mapping:
        raise ApiError.bad_request("Keine Positionen angegeben.")
    if guild.default_role in mapping:
        raise ApiError.bad_request(
            "@everyone kann nicht verschoben werden (immer Position 0).",
            code="EVERYONE_POSITION_LOCKED",
        )

    await guard(
        guild.edit_role_positions(mapping, reason=ctx.reason(data, default="Rollen sortiert (Arena AI)")),
        action="Rollenpositionen ändern",
    )
    await ctx.settle(0.7)
    fresh = ctx.client.get_guild(guild.id) or guild
    return {
        "moved": len(mapping),
        "positions": [{"id": sf(r.id), "name": r.name, "position": p} for r, p in mapping.items()],
        "roles": [serialize_role(r) for r in sorted(fresh.roles, key=lambda r: r.position, reverse=True)],
    }


@route(
    "PATCH", "/api/v1/roles/{role_id}", scope="write", tags=("roles",),
    summary="Rolle bearbeiten",
    body={
        "name": "str", "color": "'#RRGGBB'|Zahl|Name", "hoist": "bool", "mentionable": "bool",
        "permissions": '["…"] | "all" | "none" | Bitmaske',
        "permissions_add": '["…"] — nur ergänzen', "permissions_remove": '["…"] — nur entziehen',
        "icon": "URL/Data-URI oder Unicode-Emoji | null (entfernen)",
        "position": "int", "reason": "str",
    },
)
async def patch_role(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    role = resolve_role(guild, ctx.path("role_id"))
    data = await ctx.body()
    if role.is_default():
        raise ApiError.bad_request(
            "Die @everyone-Rolle kann nur über ihre Permissions geändert werden "
            "(kein Name, keine Farbe, keine Position).",
            hint='Nutze ausschließlich {"permissions": […]}.',
            code="EVERYONE_ROLE_LIMITED",
        )

    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "name" in data:
        name = parse_str(data["name"], field="name", min_length=1, max_length=100, allow_empty=False)
        if not name:
            raise ApiError.bad_request("name muss 1–100 Zeichen haben.")
        kwargs["name"] = name
        changes.append(f"name → {name}")

    color = data.get("color", data.get("colour"))
    if color is not None:
        from ...util import parse_color

        kwargs["color"] = parse_color(color, field="color")
        changes.append("color")

    if "hoist" in data:
        kwargs["hoist"] = bool(parse_bool(data["hoist"], field="hoist"))
        changes.append(f"hoist → {kwargs['hoist']}")
    if "mentionable" in data:
        kwargs["mentionable"] = bool(parse_bool(data["mentionable"], field="mentionable"))
        changes.append(f"mentionable → {kwargs['mentionable']}")

    if "permissions" in data:
        parsed = parse_permissions(data["permissions"], field="permissions")
        permissions = parsed if parsed is not None else discord.Permissions.none()
        kwargs["permissions"] = permissions
        changes.append("permissions (gesetzt)")
    else:
        permissions = discord.Permissions(role.permissions.value)
        touched = False
        if data.get("permissions_add"):
            added = parse_permissions(data["permissions_add"], field="permissions_add", base=permissions)
            permissions = added or permissions
            touched = True
            changes.append("permissions (ergänzt)")
        if data.get("permissions_remove"):
            removed = parse_permissions(data["permissions_remove"], field="permissions_remove")
            if removed:
                permissions = discord.Permissions(permissions.value & ~removed.value)
            touched = True
            changes.append("permissions (entzogen)")
        if touched:
            kwargs["permissions"] = permissions

    if "icon" in data or "display_icon" in data:
        icon = data.get("icon", data.get("display_icon"))
        if icon is None:
            kwargs["display_icon"] = None
            changes.append("icon → entfernt")
        elif isinstance(icon, str) and icon.strip().startswith(("http://", "https://", "data:")):
            kwargs["display_icon"] = await resolve_image(icon, session=ctx.http_session(), field="icon")
            changes.append("icon → Bild")
        elif isinstance(icon, str):
            kwargs["display_icon"] = icon.strip()
            changes.append(f"icon → {icon.strip()}")

    position = parse_int(data.get("position"), field="position", minimum=0, maximum=300)
    if position is not None:
        changes.append(f"position → {position}")

    kwargs["reason"] = ctx.reason(data, default=f"Rolle '{role.name}' aktualisiert (Arena AI)")

    if not changes:
        raise ApiError.bad_request(
            "Keine änderbaren Felder im Body.",
            hint="Unterstützt: name, color, hoist, mentionable, permissions, "
                 "permissions_add, permissions_remove, icon, position.",
            code="NO_CHANGES",
        )

    edit_kwargs = {k: v for k, v in kwargs.items()}
    if position is not None:
        edit_kwargs.pop("position", None)

    if edit_kwargs:
        updated = await guard(role.edit(**edit_kwargs), action=f"Rolle '{role.name}' bearbeiten")
        role = updated or role
        await ctx.settle(0.4)

    if position is not None:
        await guard(
            guild.edit_role_positions({role: position}, reason=kwargs["reason"]),
            action=f"Rolle '{role.name}' positionieren",
        )
        await ctx.settle(0.5)

    fresh = guild.get_role(role.id) or role
    return {"changed": changes, "role": serialize_role(fresh), "id": sf(fresh.id)}


@route(
    "DELETE", "/api/v1/roles/{role_id}", scope="danger", tags=("roles",),
    summary="Rolle löschen",
    query={"confirm": "muss true sein", "reason": "str"},
    description="Entzieht allen Mitgliedern diese Rolle und löscht sie unwiderruflich.",
)
async def delete_role(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    role = resolve_role(guild, ctx.path("role_id"))
    if role.is_default():
        raise ApiError.bad_request("Die @everyone-Rolle kann nicht gelöscht werden.",
                                   code="EVERYONE_ROLE_PROTECTED")
    if role.managed:
        raise ApiError.bad_request(
            f"Rolle '{role.name}' wird von einer Integration/Bot verwaltet und kann nicht "
            "über die API gelöscht werden.",
            hint="Lösche stattdessen die Integration bzw. den Bot unter "
                 "Servereinstellungen → Integrationen.",
            code="ROLE_MANAGED",
        )
    confirmed = ctx.q_bool("confirm", False)
    try:
        data = await ctx.body()
        confirmed = confirmed or bool(parse_bool(data.get("confirm"), field="confirm", default=False))
        reason = ctx.reason(data, default=None)
    except ApiError:
        reason = None
    if not confirmed:
        holders = sum(1 for m in guild.members if role in m.roles)
        raise ApiError.bad_request(
            f"Rolle '{role.name}' löschen? Sie haben aktuell {holders} zwischengespeicherte "
            "Mitglieder.",
            hint="Bestätige mit ?confirm=true.",
            code="CONFIRM_REQUIRED",
            members_cached=holders,
        )
    info = {"id": sf(role.id), "name": role.name, "position": role.position}
    await guard(role.delete(reason=reason or f"Rolle '{role.name}' gelöscht (Arena AI)"),
                action=f"Rolle '{role.name}' löschen")
    await ctx.settle(0.6)
    return {"deleted": info, "roles_remaining": len(guild.roles)}


# ─────────────────────────────────────────────────────────────────────────────
#  Zuweisen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "POST", "/api/v1/roles/{role_id}/members/{member_id}", scope="write", tags=("roles", "members"),
    summary="Rolle einem Mitglied zuweisen",
    body={"reason": "str"},
)
async def add_role_to_member(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    role = resolve_role(guild, ctx.path("role_id"))
    member = await ctx.member()
    data = await ctx.body()
    await guard(
        member.add_roles(role, reason=ctx.reason(data, default=f"Rolle '{role.name}' zugewiesen (Arena AI)")),
        action=f"'{member.display_name}' Rolle '{role.name}' geben",
    )
    await ctx.settle(0.4)
    fresh = guild.get_member(member.id) or member
    return {"member": serialize_member(fresh), "added_role": serialize_role(role)}


@route(
    "DELETE", "/api/v1/roles/{role_id}/members/{member_id}", scope="write", tags=("roles", "members"),
    summary="Rolle einem Mitglied entziehen",
    body={"reason": "str"},
)
async def remove_role_from_member(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    role = resolve_role(guild, ctx.path("role_id"))
    member = await ctx.member()
    data = await ctx.body()
    await guard(
        member.remove_roles(role, reason=ctx.reason(data, default=f"Rolle '{role.name}' entzogen (Arena AI)")),
        action=f"'{member.display_name}' Rolle '{role.name}' entziehen",
    )
    await ctx.settle(0.4)
    fresh = guild.get_member(member.id) or member
    return {"member": serialize_member(fresh), "removed_role": serialize_role(role)}
