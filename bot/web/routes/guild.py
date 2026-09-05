"""
Server-Ebene: Einstellungen, Community-Features, Audit-Log, Templates, Prune.

Die meisten Discord-Tücken stecken hier — entsprechend viel Sorgfalt fließt in
verständliche Fehlermeldungen:

* ``community: true`` verlangt **gleichzeitig** ``rules_channel`` UND
  ``public_updates_channel`` (Discord-Limitierung). Wird hier automatisch
  aufgelöst bzw. mit ``auto_community_channels`` selbst angelegt.
* ``mfa_level`` und Vanity-URL darf nur der **Server-Owner-Bot** ändern.
* ``afk_timeout`` kennt nur die Werte 60/300/900/1800/3600.
* Bilder müssen als Bytes an Discord — hier per URL/Data-URI bequem gelöst.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

import discord
from discord.utils import MISSING

from ...serializers import (
    serialize_channel,
    jsonable,
    serialize_audit_entry,
    serialize_guild,
    serialize_template,
    serialize_voice_state,
    serialize_welcome_screen,
    serialize_widget,
    serialize_onboarding,
)
from ...util import (
    ApiError,
    as_id,
    parse_bool,
    parse_enum,
    parse_int,
    parse_str,
    sf,
)
from ..context import Ctx, guard
from ..images import resolve_image
from ..registry import route

AFK_TIMEOUTS = (60, 300, 900, 1800, 3600)

_VERIFICATION_ALIASES = {
    "keine": discord.VerificationLevel.none,
    "niedrig": discord.VerificationLevel.low,
    "mittel": discord.VerificationLevel.medium,
    "hoch": discord.VerificationLevel.high,
    "streng": discord.VerificationLevel.highest,
    "sehr hoch": discord.VerificationLevel.highest,
    "low": discord.VerificationLevel.low,
    "medium": discord.VerificationLevel.medium,
    "high": discord.VerificationLevel.high,
    "highest": discord.VerificationLevel.highest,
    "table flip": discord.VerificationLevel.highest,
    "double table flip": discord.VerificationLevel.highest,
}

_FILTER_ALIASES = {
    "aus": discord.ContentFilter.disabled,
    "deaktiviert": discord.ContentFilter.disabled,
    "ohne rollen": discord.ContentFilter.no_role,
    "mitglieder ohne rolle": discord.ContentFilter.no_role,
    "alle": discord.ContentFilter.all_members,
    "alle mitglieder": discord.ContentFilter.all_members,
}

_NOTIFICATION_ALIASES = {
    "alle": discord.NotificationLevel.all_messages,
    "alle nachrichten": discord.NotificationLevel.all_messages,
    "nur erwähnungen": discord.NotificationLevel.only_mentions,
    "erwähnungen": discord.NotificationLevel.only_mentions,
    "mentions": discord.NotificationLevel.only_mentions,
}


# ─────────────────────────────────────────────────────────────────────────────
#  Lesen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/guild", scope="read", tags=("guild",),
    summary="Alle Server-Einstellungen",
    description="Vollständiger Server-Datensatz: Name, Bilder, Moderationsstufen, "
                "Community-Features, wichtige Kanäle und die Rechte des Bots.",
    query={"detailed": "true/false — Rollen/Emojis/Voice-States mitsenden (Standard true)"},
)
async def get_guild(ctx: Ctx) -> Dict[str, Any]:
    detailed = ctx.q_bool("detailed", True)
    return serialize_guild(ctx.guild, detailed=bool(detailed))


@route(
    "GET", "/api/v1/guild/preview", scope="read", tags=("guild",),
    summary="Öffentliche Server-Vorschau (Discovery-Daten)",
)
async def guild_preview(ctx: Ctx) -> Dict[str, Any]:
    try:
        preview = await guard(ctx.client.fetch_guild_preview(ctx.guild.id), action="Server-Vorschau laden")
    except ApiError as exc:
        if exc.code == "DISCORD_NOT_FOUND":
            raise ApiError.not_found(
                "Für diesen Server gibt es keine öffentliche Vorschau.",
                hint="Die Vorschau existiert nur für Community-/Discovery-Server.",
                code="NO_PREVIEW",
            ) from exc
        raise
    return {
        "id": sf(preview.id),
        "name": preview.name,
        "description": getattr(preview, "description", None),
        "features": list(getattr(preview, "features", []) or []),
        "approximate_member_count": getattr(preview, "approximate_member_count", None),
        "approximate_presence_count": getattr(preview, "approximate_presence_count", None),
        "emojis": [jsonable(e) for e in getattr(preview, "emojis", [])],
        "icon_url": str(preview.icon.url) if getattr(preview, "icon", None) else None,
        "splash_url": str(preview.splash.url) if getattr(preview, "splash", None) else None,
    }


@route(
    "GET", "/api/v1/guild/audit-logs", scope="read", tags=("guild", "moderation"),
    summary="Audit-Log des Servers",
    query={
        "limit": "int, Standard 25 (max. 100)",
        "user_id": "nur Einträge dieses Nutzers",
        "action": "Filter, z. B. 'channel_create', 'member_ban', 'guild_update'",
        "before": "Eintrags-ID, älter als dieser",
        "after": "Eintrags-ID, neuer als dieser",
        "oldest_first": "true/false — Sortierung umdrehen",
    },
)
async def audit_logs(ctx: Ctx) -> Dict[str, Any]:
    limit = ctx.q_int("limit", 25, minimum=1, maximum=100) or 25
    user_id = ctx.q_id("user_id")
    action_name = ctx.q("action")
    before = ctx.q("before")
    after = ctx.q("after")

    kwargs: Dict[str, Any] = {"limit": limit}
    if user_id:
        kwargs["user"] = discord.Object(id=user_id)
    if action_name:
        kwargs["action"] = parse_enum(discord.AuditLogAction, action_name, field="?action")
    if before:
        kwargs["before"] = discord.Object(id=as_id(before, field="?before"))
    if after:
        kwargs["after"] = discord.Object(id=as_id(after, field="?after"))
    if ctx.q_bool("oldest_first", False):
        kwargs["oldest_first"] = True

    entries: List[Dict[str, Any]] = []
    try:
        async for entry in ctx.guild.audit_logs(**kwargs):
            entries.append(serialize_audit_entry(entry))
    except discord.Forbidden as exc:
        raise ApiError.forbidden(
            "Audit-Log nicht lesbar: dem Bot fehlt 'view_audit_log'.",
            code="AUDIT_LOG_FORBIDDEN",
        ) from exc

    return {
        "count": len(entries),
        "available_actions": sorted({str(a.name) for a in discord.AuditLogAction}),
        "entries": entries,
    }


@route(
    "GET", "/api/v1/guild/voice-states", scope="read", tags=("guild",),
    summary="Wer ist gerade in welchem Sprachkanal?",
)
async def voice_states(ctx: Ctx) -> Dict[str, Any]:
    states = [
        serialize_voice_state(m) for m in ctx.guild.members if getattr(m, "voice", None) is not None
    ]
    by_channel: Dict[str, List[Any]] = {}
    for state in states:
        channel = (state or {}).get("channel") or {}
        key = str(channel.get("id") or "unknown")
        by_channel.setdefault(key, []).append(state)
    return {
        "count": len(states),
        "in_voice": states,
        "by_channel": by_channel,
        "voice_channels": [
            serialize_channel(c)
            for c in ctx.guild.voice_channels + list(getattr(ctx.guild, "stage_channels", []))
        ],
    }


@route(
    "GET", "/api/v1/guild/widget", scope="read", tags=("guild",),
    summary="Server-Widget (Status + JSON-Endpoint)",
)
async def get_widget(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    widget = await guard(_maybe_widget(guild), action="Widget laden")
    return {
        "enabled": guild.widget_enabled,
        "channel": {"id": sf(guild.widget_channel.id), "name": guild.widget_channel.name}
        if guild.widget_channel
        else None,
        "widget": serialize_widget(widget),
        "widget_json_url": f"https://discord.com/api/guilds/{guild.id}/widget.json",
        "widget_png_url": f"https://discord.com/api/guilds/{guild.id}/widget.png?style=shield",
    }


async def _maybe_widget(guild: discord.Guild) -> Optional[discord.Widget]:
    if not guild.widget_enabled:
        return None
    try:
        return await guild.widget()
    except discord.HTTPException:
        return None


@route(
    "PATCH", "/api/v1/guild/widget", scope="manage", tags=("guild",),
    summary="Server-Widget aktivieren/deaktivieren",
    body={"enabled": "bool", "channel_id": "Text-/Voice-Kanal-ID oder null", "reason": "str"},
)
async def edit_widget(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    guild = ctx.guild
    kwargs: Dict[str, Any] = {}
    if "enabled" in data:
        kwargs["enabled"] = bool(parse_bool(data["enabled"], field="enabled"))
    if "channel_id" in data:
        raw = data["channel_id"]
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"remove", "none", "null"}):
            kwargs["channel"] = None
        else:
            channel_id = as_id(raw, field="channel_id")
            channel = guild.get_channel(channel_id)
            if channel is None:
                raise ApiError.not_found(f"Kanal {channel_id} nicht gefunden.")
            kwargs["channel"] = channel
    if not kwargs:
        raise ApiError.bad_request("Nichts zu tun: 'enabled' und/oder 'channel_id' angeben.")
    kwargs["reason"] = ctx.reason(data, default="Widget aktualisiert (Arena AI)")
    await guard(guild.edit_widget(**kwargs), action="Widget aktualisieren")
    await ctx.settle()
    return {"ok": True, "widget_enabled": guild.widget_enabled, "changed": list(kwargs)}


@route(
    "GET", "/api/v1/guild/welcome-screen", scope="read", tags=("guild", "community"),
    summary="Willkommensbildschirm",
)
async def get_welcome_screen(ctx: Ctx) -> Dict[str, Any]:
    try:
        screen = await guard(ctx.guild.welcome_screen(), action="Welcome-Screen laden")
    except ApiError as exc:
        if exc.code in {"DISCORD_NOT_FOUND", "DISCORD_ERROR"}:
            return {
                "configured": False,
                "hint": "Kein Willkommensbildschirm vorhanden. Community-Server aktivieren: "
                        'PATCH /api/v1/guild {"community": true, "rules_channel": "…", '
                        '"public_updates_channel": "…"}',
            }
        raise
    return {"configured": True, **serialize_welcome_screen(screen)}


@route(
    "PATCH", "/api/v1/guild/welcome-screen", scope="manage", tags=("guild", "community"),
    summary="Willkommensbildschirm bearbeiten",
    body={
        "enabled": "bool",
        "description": "str",
        "welcome_channels": '[{"channel_id": "…", "description": "…", "emoji": "👋 oder Emoji-ID"}]',
        "reason": "str",
    },
    description="Benötigt einen Community-Server. Kanäle werden per ID oder Name aufgelöst.",
)
async def edit_welcome_screen(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    return await apply_welcome_screen(ctx, data)


async def apply_welcome_screen(ctx: Ctx, data: Dict[str, Any]) -> Dict[str, Any]:
    """Kernlogik von ``PATCH /api/v1/guild/welcome-screen``.

    Auch vom Setup-Wizard direkt aufrufbar (ohne HTTP-Umweg), damit beide Pfade
    exakt dieselbe Validierung und dieselben Fehlermeldungen produzieren.
    """
    guild = ctx.guild
    kwargs: Dict[str, Any] = {}

    if "enabled" in data:
        kwargs["enabled"] = bool(parse_bool(data["enabled"], field="enabled"))
    if "description" in data:
        kwargs["description"] = parse_str(data["description"], field="description", max_length=120) or ""

    raw_channels = data.get("welcome_channels")
    if raw_channels is not None:
        if not isinstance(raw_channels, list):
            raise ApiError.bad_request("welcome_channels muss eine Liste sein.")
        if len(raw_channels) > 5:
            raise ApiError.bad_request("welcome_channels: Discord erlaubt maximal 5 Einträge.")
        channels: List[discord.WelcomeChannel] = []
        for index, entry in enumerate(raw_channels):
            if not isinstance(entry, dict):
                raise ApiError.bad_request(f"welcome_channels[{index}]: erwartet ein Objekt.")
            channel = _resolve_channel_flexible(
                guild, entry.get("channel_id") or entry.get("channel"), field=f"welcome_channels[{index}]"
            )
            description = parse_str(
                entry.get("description", ""), field=f"welcome_channels[{index}].description", max_length=42
            )
            emoji = entry.get("emoji")
            emoji_obj = _resolve_emoji(guild, emoji, field=f"welcome_channels[{index}].emoji")
            channels.append(discord.WelcomeChannel(channel=channel, description=description or "", emoji=emoji_obj))
        kwargs["welcome_channels"] = channels

    if not kwargs:
        raise ApiError.bad_request(
            "Nichts zu tun: 'enabled', 'description' und/oder 'welcome_channels' angeben."
        )
    kwargs["reason"] = ctx.reason(data, default="Willkommensbildschirm aktualisiert (Arena AI)")

    screen = await guard(guild.edit_welcome_screen(**kwargs), action="Welcome-Screen bearbeiten")
    await ctx.settle()
    return {"configured": True, **serialize_welcome_screen(screen)}


@route(
    "GET", "/api/v1/guild/onboarding", scope="read", tags=("guild", "community"),
    summary="Onboarding (Server-geführter Einstieg)",
)
async def get_onboarding(ctx: Ctx) -> Dict[str, Any]:
    try:
        onboarding = await guard(ctx.guild.onboarding(), action="Onboarding laden")
    except ApiError as exc:
        if exc.code in {"DISCORD_NOT_FOUND", "DISCORD_ERROR"}:
            return {"configured": False, "hint": "Onboarding ist nicht eingerichtet."}
        raise
    return {"configured": True, **serialize_onboarding(onboarding)}


@route(
    "PATCH", "/api/v1/guild/onboarding", scope="manage", tags=("guild", "community"),
    summary="Onboarding bearbeiten",
    body={
        "enabled": "bool",
        "mode": "'default' | 'advanced'",
        "default_channels": '["Kanal-ID", …]',
        "prompts": '[{"title": "…", "type": "multiple_choice|single_choice", "in_onboarding": true, '
                   '"required": false, "options": [{"title": "…", "description": "…", '
                   '"channel_ids": ["…"], "emoji": "👋"}]}]',
        "reason": "str",
    },
)
async def edit_onboarding(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    guild = ctx.guild
    kwargs: Dict[str, Any] = {}

    if "enabled" in data:
        kwargs["enabled"] = bool(parse_bool(data["enabled"], field="enabled"))
    if "mode" in data:
        kwargs["mode"] = parse_enum(discord.OnboardingMode, data["mode"], field="mode")

    if data.get("default_channels") is not None:
        raw = data["default_channels"]
        if not isinstance(raw, list):
            raise ApiError.bad_request("default_channels muss eine Liste sein.")
        kwargs["default_channels"] = [
            _resolve_channel_flexible(guild, item, field=f"default_channels[{i}]")
            for i, item in enumerate(raw)
        ]

    if data.get("prompts") is not None:
        raw = data["prompts"]
        if not isinstance(raw, list):
            raise ApiError.bad_request("prompts muss eine Liste sein.")
        prompts: List[discord.OnboardingPrompt] = []
        for index, prompt in enumerate(raw):
            if not isinstance(prompt, dict):
                raise ApiError.bad_request(f"prompts[{index}]: erwartet ein Objekt.")
            options: List[discord.PromptOption] = []
            for o_index, option in enumerate(prompt.get("options") or []):
                if not isinstance(option, dict):
                    raise ApiError.bad_request(f"prompts[{index}].options[{o_index}]: erwartet ein Objekt.")
                channel_ids = option.get("channel_ids") or []
                if not isinstance(channel_ids, list):
                    raise ApiError.bad_request(
                        f"prompts[{index}].options[{o_index}].channel_ids muss eine Liste sein."
                    )
                resolved_ids: List[int] = []
                for c_index, item in enumerate(channel_ids):
                    channel = _resolve_channel_flexible(
                        guild, item, field=f"prompts[{index}].options[{o_index}].channel_ids[{c_index}]"
                    )
                    resolved_ids.append(channel.id)
                emoji_arg: Any = _resolve_emoji(
                    guild, option.get("emoji"), field=f"prompts[{index}].options[{o_index}].emoji"
                )
                role_ids: List[int] = []
                for r_index, item in enumerate(option.get("role_ids") or []):
                    role_ids.append(as_id(item, field=f"prompts[{index}].options[{o_index}].role_ids[{r_index}]"))
                options.append(
                    discord.OnboardingPromptOption(
                        title=parse_str(option.get("title"), field=f"options[{o_index}].title",
                                        max_length=50, allow_empty=False) or "",
                        description=parse_str(option.get("description"),
                                              field=f"options[{o_index}].description", max_length=100),
                        channels=resolved_ids,
                        roles=role_ids,
                        emoji=emoji_arg if emoji_arg is not None else MISSING,
                    )
                )
            prompt_type = parse_enum(
                discord.OnboardingPromptType,
                _normalize_prompt_type(prompt.get("type")),
                field=f"prompts[{index}].type",
                default=discord.OnboardingPromptType.multiple_choice,
            )
            single_select = prompt.get("single_select")
            if single_select is None:
                single_select = str(prompt.get("type") or "").lower() in {"single_choice", "single", "single-choice"}
            prompts.append(
                discord.OnboardingPrompt(
                    type=prompt_type,
                    title=parse_str(prompt.get("title"), field=f"prompts[{index}].title",
                                    max_length=100, allow_empty=False) or "",
                    single_select=bool(single_select),
                    in_onboarding=bool(parse_bool(prompt.get("in_onboarding", True),
                                                  field=f"prompts[{index}].in_onboarding", default=True)),
                    required=bool(parse_bool(prompt.get("required", False),
                                             field=f"prompts[{index}].required", default=False)),
                    options=options,
                )
            )
        kwargs["prompts"] = prompts

    if not kwargs:
        raise ApiError.bad_request(
            "Nichts zu tun: 'enabled', 'mode', 'default_channels' und/oder 'prompts' angeben."
        )
    kwargs["reason"] = ctx.reason(data, default="Onboarding aktualisiert (Arena AI)")

    result = await guard(guild.edit_onboarding(**kwargs), action="Onboarding bearbeiten")
    await ctx.settle()
    return {"configured": True, **serialize_onboarding(result)}


@route(
    "GET", "/api/v1/guild/templates", scope="read", tags=("guild",),
    summary="Server-Vorlagen auflisten",
)
async def get_templates(ctx: Ctx) -> Dict[str, Any]:
    templates = await guard(ctx.guild.templates(), action="Templates laden")
    return {"count": len(templates), "templates": [serialize_template(t) for t in templates]}


@route(
    "POST", "/api/v1/guild/templates", scope="manage", tags=("guild",),
    summary="Server-Vorlage aus dem aktuellen Zustand erstellen",
    body={"name": "str (Pflicht)", "description": "str (optional)"},
)
async def create_template(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    name = parse_str(data.get("name"), field="name", max_length=100, allow_empty=False)
    if not name:
        raise ApiError.bad_request("'name' fehlt — die Vorlage braucht einen Namen.")
    template = await guard(
        ctx.guild.create_template(name=name, description=parse_str(data.get("description"),
                                                                  field="description", max_length=120)),
        action="Template erstellen",
    )
    await ctx.settle()
    return serialize_template(template)


@route(
    "GET", "/api/v1/guild/prune/count", scope="manage", tags=("guild", "moderation"),
    summary="Wie viele inaktive Mitglieder würden gekickt?",
    query={"days": "int 1–30, Standard 7", "roles": "Kanal/Rollen-IDs (kommagetrennt), die zählen"},
)
async def prune_count(ctx: Ctx) -> Dict[str, Any]:
    days = ctx.q_int("days", 7, minimum=1, maximum=30) or 7
    role_ids = [as_id(part, field="?roles") for part in ctx.q_list("roles") if part.strip()]
    if not role_ids and ctx.q("roles"):
        role_ids = [as_id(p.strip(), field="?roles") for p in ctx.q("roles").split(",") if p.strip()]
    roles = [discord.Object(id=r) for r in role_ids]
    count = await guard(
        ctx.guild.estimate_pruned_members(days=days, roles=roles or MISSING),
        action="Prune-Zählung",
    )
    return {
        "days": days,
        "pruned_members_estimate": count,
        "note": "POST /api/v1/guild/prune führt die Aktion wirklich aus (kickt die Mitglieder).",
    }


@route(
    "POST", "/api/v1/guild/prune", scope="danger", tags=("guild", "moderation"),
    summary="Inaktive Mitglieder kicken (Prune)",
    body={
        "days": "int 1–30, Standard 7",
        "compute_prune_count": "bool, Standard true",
        "roles": '["Rollen-ID", …] — zusätzlich zu berücksichtigende Rollen',
        "reason": "str",
        "confirm": "muss true sein — Schutz vor versehentlichem Aufruf",
    },
    description="**Destruktiv.** Kickt alle Mitglieder, die X Tage inaktiv waren und keine "
                "der angegebenen Rollen besitzen. Erfordert 'confirm': true.",
)
async def prune(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    if not parse_bool(data.get("confirm"), field="confirm", default=False):
        raise ApiError.bad_request(
            "Prune ist destruktiv und braucht eine Bestätigung.",
            hint='Sende {"days": 7, "confirm": true}. Vorher GET /api/v1/guild/prune/count '
            "für die Anzahl.",
            code="CONFIRM_REQUIRED",
        )
    days = parse_int(data.get("days"), field="days", default=7, minimum=1, maximum=30) or 7
    roles = [discord.Object(id=as_id(r, field="roles")) for r in (data.get("roles") or [])]
    pruned = await guard(
        ctx.guild.prune_members(
            days=days,
            compute_prune_count=bool(parse_bool(data.get("compute_prune_count"),
                                                field="compute_prune_count", default=True)),
            roles=roles or MISSING,
            reason=ctx.reason(data, default=f"Prune: {days} Tage inaktiv (Arena AI)"),
        ),
        action="Prune ausführen",
    )
    await ctx.settle(0.8)
    return {"days": days, "pruned_members": pruned}


@route(
    "GET", "/api/v1/voice/regions", scope="read", tags=("guild",),
    summary="Verfügbare Sprach-Regionen (für rtc_region von Voice-Kanälen)",
)
async def voice_regions(ctx: Ctx) -> Dict[str, Any]:
    route = discord.http.Route("GET", "/voice/regions")
    raw = await guard(ctx.client.http.request(route), action="Sprachregionen laden")
    regions = raw if isinstance(raw, list) else []
    return {
        "count": len(regions),
        "regions": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "optimal": r.get("optimal"),
                "deprecated": r.get("deprecated"),
                "custom": r.get("custom"),
            }
            for r in regions
        ],
        "current_guild_region": getattr(ctx.guild, "region", None) and str(getattr(ctx.guild, "region", "")),
        "hint": "rtc_region wird pro Voice-Kanal gesetzt: "
                'PATCH /api/v1/channels/{id} {"rtc_region": "rotterdam"}',
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Schreiben
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "PATCH", "/api/v1/guild", scope="manage", tags=("guild",),
    summary="Server-Einstellungen ändern (Name, Bilder, Sicherheit, Community …)",
    body={
        "name": "str (2–100 Zeichen)",
        "description": "str (max. 120)",
        "icon": "URL | data:image/png;base64,… | null (entfernen)",
        "banner": "URL | Data-URI | null — braucht Boost-Stufe 2",
        "splash": "URL | Data-URI | null — braucht Boost-Stufe 1",
        "verification_level": "'none'|'low'|'medium'|'high'|'highest' (oder 0–4)",
        "explicit_content_filter": "'disabled'|'no_role'|'all_members' (oder 0–2)",
        "default_notifications": "'all_messages'|'only_mentions' (oder 0–1)",
        "afk_channel_id": "Voice-Kanal-ID | null",
        "afk_timeout": "60|300|900|1800|3600",
        "system_channel_id": "Text-Kanal-ID | null",
        "system_channel_flags": '{"join_notifications": bool, "premium_subscriptions": bool, …}',
        "rules_channel_id": "Text-Kanal-ID | null (Community)",
        "public_updates_channel_id": "Text-Kanal-ID | null (Community)",
        "safety_alerts_channel_id": "Text-Kanal-ID | null",
        "preferred_locale": "'de'|'en-US'|… (Locale-Code)",
        "community": "bool — aktiviert/deaktiviert Community-Features",
        "auto_community_channels": "bool — legt 'regeln' + 'moderator-updates' an, falls nötig",
        "discoverable": "bool",
        "invites_disabled": "bool",
        "raid_alerts_disabled": "bool",
        "premium_progress_bar_enabled": "bool",
        "vanity_code": "str — braucht Boost-Stufe 3",
        "reason": "str",
    },
    description="Alle Felder sind optional — es wird nur geändert, was gesendet wird. "
                "Ein Bild per URL wird serverseitig heruntergeladen.",
)
async def patch_guild(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    return await apply_guild_patch(ctx, data)


async def apply_guild_patch(ctx: Ctx, data: Dict[str, Any], *, reason_default: Optional[str] = None) -> Dict[str, Any]:
    """Kernlogik von ``PATCH /api/v1/guild`` — auch für den Setup-Wizard nutzbar."""
    if not data:
        raise ApiError.bad_request(
            "Leerer Body — mindestens ein Feld ändern.",
            hint='Beispiel: {"verification_level": "medium", "reason": "Spam-Schutz"}',
        )

    guild = ctx.guild
    session = ctx.http_session()
    kwargs: Dict[str, Any] = {}
    changes: List[str] = []
    dangerous: List[str] = []

    # ── Textfelder ───────────────────────────────────────────────────────────
    if "name" in data:
        name = parse_str(data["name"], field="name", min_length=2, max_length=100, allow_empty=False)
        if not name:
            raise ApiError.bad_request("name muss 2–100 Zeichen haben.")
        kwargs["name"] = name
        changes.append(f"name → {name}")

    if "description" in data:
        kwargs["description"] = parse_str(data["description"], field="description", max_length=120)
        changes.append("description")

    # ── Bilder ───────────────────────────────────────────────────────────────
    image_tasks = []
    for key, discord_key in (
        ("icon", "icon"), ("banner", "banner"), ("splash", "splash"),
        ("discovery_splash", "discovery_splash"),
    ):
        if key in data:
            image_tasks.append((discord_key, key, data[key]))
    if image_tasks:
        results = await asyncio.gather(
            *[resolve_image(value, session=session, field=key) for _, key, value in image_tasks],
            return_exceptions=True,
        )
        for (discord_key, key, _), result in zip(image_tasks, results):
            if isinstance(result, BaseException):
                if isinstance(result, ApiError):
                    raise result
                raise ApiError.bad_request(f"{key}: {result}") from result
            kwargs[discord_key] = result
            changes.append(f"{key} → {'entfernt' if result is None else 'gesetzt'}")

    # ── Enums ────────────────────────────────────────────────────────────────
    if "verification_level" in data:
        kwargs["verification_level"] = parse_enum(
            discord.VerificationLevel, data["verification_level"],
            field="verification_level", extra=_VERIFICATION_ALIASES,
        )
        changes.append(f"verification_level → {kwargs['verification_level'].name}")

    if "explicit_content_filter" in data:
        kwargs["explicit_content_filter"] = parse_enum(
            discord.ContentFilter, data["explicit_content_filter"],
            field="explicit_content_filter", extra=_FILTER_ALIASES,
        )
        changes.append(f"explicit_content_filter → {kwargs['explicit_content_filter'].name}")

    if "default_notifications" in data:
        kwargs["default_notifications"] = parse_enum(
            discord.NotificationLevel, data["default_notifications"],
            field="default_notifications", extra=_NOTIFICATION_ALIASES,
        )
        changes.append(f"default_notifications → {kwargs['default_notifications'].name}")

    if "preferred_locale" in data:
        raw = parse_str(data["preferred_locale"], field="preferred_locale", max_length=16)
        kwargs["preferred_locale"] = _parse_locale(raw)
        changes.append(f"preferred_locale → {raw}")

    # ── Kanal-Zuweisungen ────────────────────────────────────────────────────
    channel_fields = {
        "afk_channel_id": ("afk_channel", (discord.VoiceChannel, discord.StageChannel), "Voice"),
        "afk_channel": ("afk_channel", (discord.VoiceChannel, discord.StageChannel), "Voice"),
        "system_channel_id": ("system_channel", (discord.TextChannel,), "Text"),
        "system_channel": ("system_channel", (discord.TextChannel,), "Text"),
        "rules_channel_id": ("rules_channel", (discord.TextChannel,), "Text"),
        "rules_channel": ("rules_channel", (discord.TextChannel,), "Text"),
        "public_updates_channel_id": ("public_updates_channel", (discord.TextChannel,), "Text"),
        "public_updates_channel": ("public_updates_channel", (discord.TextChannel,), "Text"),
        "safety_alerts_channel_id": ("safety_alerts_channel", (discord.TextChannel,), "Text"),
        "safety_alerts_channel": ("safety_alerts_channel", (discord.TextChannel,), "Text"),
    }
    for source_key, (target_key, allowed, kind_label) in channel_fields.items():
        if source_key not in data:
            continue
        raw = data[source_key]
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"remove", "none", "null"}):
            kwargs[target_key] = None
            changes.append(f"{target_key} → entfernt")
            continue
        channel = _resolve_channel_flexible(guild, raw, field=source_key, expected=allowed,
                                            kind_label=kind_label)
        kwargs[target_key] = channel
        changes.append(f"{target_key} → #{channel.name}")

    if "afk_timeout" in data:
        timeout = parse_int(data["afk_timeout"], field="afk_timeout")
        if timeout not in AFK_TIMEOUTS:
            raise ApiError.bad_request(
                f"afk_timeout {timeout} ist ungültig.",
                hint=f"Discord erlaubt ausschließlich: {', '.join(map(str, AFK_TIMEOUTS))} Sekunden.",
                code="AFK_TIMEOUT_INVALID",
            )
        kwargs["afk_timeout"] = timeout
        changes.append(f"afk_timeout → {timeout}s")

    if "system_channel_flags" in data:
        raw = data["system_channel_flags"]
        if not isinstance(raw, dict):
            raise ApiError.bad_request("system_channel_flags muss ein Objekt sein.")
        flags = discord.SystemChannelFlags()
        flags.value = guild.system_channel_flags.value
        for flag_name, enabled in raw.items():
            normalized = str(flag_name).strip().lower().replace("-", "_")
            if normalized not in discord.SystemChannelFlags.VALID_FLAGS:
                raise ApiError.bad_request(
                    f"system_channel_flags: '{flag_name}' ist unbekannt.",
                    hint="Erlaubt: " + ", ".join(sorted(discord.SystemChannelFlags.VALID_FLAGS)),
                )
            setattr(flags, normalized, bool(parse_bool(enabled, field=f"system_channel_flags.{flag_name}")))
        kwargs["system_channel_flags"] = flags
        changes.append("system_channel_flags")

    # ── Feature-Schalter ─────────────────────────────────────────────────────
    for key, label in (
        ("discoverable", "discoverable"),
        ("invites_disabled", "invites_disabled"),
        ("raid_alerts_disabled", "raid_alerts_disabled"),
        ("premium_progress_bar_enabled", "premium_progress_bar_enabled"),
    ):
        if key in data:
            kwargs[key] = bool(parse_bool(data[key], field=key))
            changes.append(f"{label} → {kwargs[key]}")
    if "premium_progress_bar" in data and "premium_progress_bar_enabled" not in kwargs:
        kwargs["premium_progress_bar_enabled"] = bool(parse_bool(data["premium_progress_bar"],
                                                                 field="premium_progress_bar"))
        changes.append("premium_progress_bar_enabled")

    if "community" in data:
        wants_community = bool(parse_bool(data["community"], field="community"))
        kwargs, changes = await _prepare_community(
            ctx, guild, data, wants_community, kwargs, changes
        )

    if "vanity_code" in data:
        code = parse_str(data["vanity_code"], field="vanity_code", max_length=32)
        kwargs["vanity_code"] = code
        changes.append(f"vanity_code → {code}")
        dangerous.append("vanity_code (nur mit Boost-Stufe 3 & Owner-Bot)")

    if "mfa_level" in data:
        kwargs["mfa_level"] = parse_enum(discord.MFALevel, data["mfa_level"], field="mfa_level")
        changes.append(f"mfa_level → {kwargs['mfa_level'].name}")
        dangerous.append("mfa_level (nur der Server-Owner-Bot darf das)")

    kwargs["reason"] = ctx.reason(data, default=reason_default
                                  or "Server-Einstellungen aktualisiert (Arena AI)")

    updated = await guard(guild.edit(**kwargs), action="Server aktualisieren")
    await ctx.settle(0.6)
    fresh = ctx.client.get_guild(guild.id) or updated

    return {
        "changed": changes,
        "dangerous": dangerous or None,
        "guild": serialize_guild(fresh, detailed=False),
        "hint": "GET /api/v1/guild liefert den verifizierten neuen Zustand.",
    }


@route(
    "PATCH", "/api/v1/guild/mfa", scope="danger", tags=("guild", "moderation"),
    summary="2FA-Pflicht für Moderationsaktionen umschalten",
    body={"level": "0|1 oder 'disabled'|'require_2fa'", "reason": "str"},
    description="Funktioniert nur, wenn der Bot selbst der Server-Owner ist. "
                "Sonst antwortet Discord mit 50041/403.",
)
async def patch_mfa(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    level = parse_enum(discord.MFALevel, data.get("level", data.get("mfa_level")), field="level")
    if level is None:
        raise ApiError.bad_request("'level' fehlt (0 = aus, 1 = 2FA Pflicht).")
    guild = ctx.guild
    if guild.owner_id != (ctx.client.user.id if ctx.client.user else 0):
        raise ApiError.forbidden(
            "Die 2FA-Stufe darf ausschließlich ein Bot ändern, der Owner des Servers ist.",
            hint="Bitte den Server-Owner, das in den Servereinstellungen umzustellen, "
                 "oder übertrage den Bot zum Owner (nur empfohlen, wenn du weißt, was du tust).",
            code="OWNER_REQUIRED",
        )
    await guard(
        guild.edit(mfa_level=level, reason=ctx.reason(data, default="MFA-Level geändert (Arena AI)")),
        action="MFA-Level setzen",
    )
    await ctx.settle(0.5)
    return {"mfa_level": int(level.value), "mfa_level_name": level.name, "guild_id": sf(guild.id)}


# ─────────────────────────────────────────────────────────────────────────────
#  Helfer
# ─────────────────────────────────────────────────────────────────────────────


def _parse_locale(raw: Optional[str]) -> discord.Locale:
    if not raw:
        return discord.Locale.german
    text = raw.strip()
    try:
        return discord.Locale(text)
    except ValueError:
        pass
    lowered = text.lower().replace("_", "-")
    shortcuts = {
        "de": "de", "deutsch": "de", "german": "de", "de-de": "de",
        "en": "en-US", "englisch": "en-US", "english": "en-US", "en-us": "en-US", "en-gb": "en-GB",
        "tr": "tr", "türkisch": "tr", "turkish": "tr",
        "fr": "fr", "es": "es-ES", "it": "it", "nl": "nl", "pl": "pl", "pt": "pt-BR", "ru": "ru",
    }
    if lowered in shortcuts:
        target = shortcuts[lowered]
        try:
            return discord.Locale(target)
        except ValueError:
            pass
    for locale in discord.Locale:
        if locale.value.lower() == lowered:
            return locale
    raise ApiError.bad_request(
        f"preferred_locale '{raw}' ist unbekannt.",
        hint="Beispiele: 'de', 'en-US', 'tr', 'fr'. Vollständige Liste: "
        + ", ".join(str(l.value) for l in list(discord.Locale)[:12]) + ", …",
        code="LOCALE_INVALID",
    )


def _resolve_channel_flexible(
    guild: discord.Guild,
    value: Any,
    *,
    field: str = "channel",
    expected: tuple = (discord.TextChannel,),
    kind_label: str = "Text",
) -> Any:
    """
    Kanal per ID, ``<#id>``, ``channel:<name>``, Name **oder Objekt** auflösen.

    Das Objekt ist kein Sonderfall für Spezialisten: Der Setup-Wizard löst
    ``afk_channel``/``system_channel``/… vorab über seine ``keys``-Tabelle auf
    und reicht dann echte ``CategoryChannel``/``VoiceChannel``-Instanzen weiter.
    Ohne diesen Zweig lief jeder ``settings``-Block der Vorlagen in ein 404.
    """
    if value is None:
        raise ApiError.bad_request(f"{field}: fehlt.")
    channel: Any = None

    if isinstance(value, discord.abc.GuildChannel) or isinstance(value, discord.Thread):
        channel = value
    elif isinstance(value, int):
        channel = guild.get_channel(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("channel:"):
            text = text[len("channel:"):]
        if text.startswith("<#") and text.endswith(">"):
            text = text[2:-1]
        if text.isdigit():
            channel = guild.get_channel(int(text))
        else:
            wanted = text.lower().lstrip("#")
            for candidate in guild.channels:
                if candidate.name.lower() == wanted:
                    channel = candidate
                    break
            if channel is None:
                for candidate in guild.channels:
                    if wanted in candidate.name.lower():
                        channel = candidate
                        break
    elif isinstance(value, dict):
        return _resolve_channel_flexible(
            guild, value.get("id") or value.get("name"), field=field,
            expected=expected, kind_label=kind_label,
        )

    if channel is None:
        raise ApiError.not_found(
            f"{field}: Kanal '{value}' nicht gefunden.",
            hint="Nutze GET /api/v1/channels für alle Kanäle. Akzeptiert werden ID, '#name' "
                 "oder der reine Name.",
            code="CHANNEL_NOT_FOUND",
        )
    if expected and not isinstance(channel, expected):
        names = ", ".join(getattr(e, "__name__", str(e)) for e in expected)
        raise ApiError.bad_request(
            f"{field}: '{channel.name}' ist ein {type(channel).__name__}, erwartet wurde {names}.",
            hint=f"Für diesen Zweck braucht Discord einen {kind_label}-Kanal.",
            code="CHANNEL_TYPE_MISMATCH",
        )
    return channel


_CUSTOM_EMOJI_RE = re.compile(r"<(?P<animated>a)?:(?P<name>[^:>]+):(?P<id>\d{15,25})>")


def _resolve_emoji(guild: discord.Guild, value: Any, *, field: str = "emoji") -> Optional[Any]:
    """
    Unicode-Emoji (``"👋"``), Kurzform (``":pepe:"``), ID oder ``<a:name:id>``
    → ``Emoji``/``PartialEmoji``/``str``. Unicode bleibt bewusst ein String,
    denn genau das akzeptieren ``WelcomeChannel`` und ``OnboardingPromptOption``.
    """
    if value is None or value == "":
        return None

    match = _CUSTOM_EMOJI_RE.match(str(value).strip())
    if match:
        emoji_id = int(match.group("id"))
        return guild.get_emoji(emoji_id) or discord.PartialEmoji.with_state(
            guild._state, name=match.group("name"), animated=bool(match.group("animated")), id=emoji_id
        )

    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        emoji_id = int(value)
        emoji = guild.get_emoji(emoji_id)
        if emoji is None:
            raise ApiError.not_found(
                f"{field}: Server-Emoji mit ID {emoji_id} nicht gefunden.",
                hint="GET /api/v1/emojis listet alle verfügbaren Emojis.",
                code="EMOJI_NOT_FOUND",
            )
        return emoji

    text = str(value).strip()
    if text.startswith(":") and text.endswith(":"):
        name = text[1:-1].lower()
        for emoji in guild.emojis:
            if emoji.name and emoji.name.lower() == name:
                return emoji
        raise ApiError.not_found(
            f"{field}: Server-Emoji '{text}' nicht gefunden.",
            hint="GET /api/v1/emojis listet alle Namen. Für Unicode einfach '👋' senden.",
            code="EMOJI_NOT_FOUND",
        )
    return text  # Unicode-Emoji


async def _prepare_community(
    ctx: Ctx,
    guild: discord.Guild,
    data: Dict[str, Any],
    wants_community: bool,
    kwargs: Dict[str, Any],
    changes: List[str],
) -> tuple:
    """
    ``community: true`` braucht zwingend ``rules_channel`` UND
    ``public_updates_channel`` **im selben** ``guild.edit``-Aufruf.

    Strategie:
      1. Bereits im Body angegeben? → verwenden.
      2. Schon am Server gesetzt? → mitliefern (sonst meckert Discord).
      3. ``auto_community_channels: true``? → Kanäle anlegen.
      4. Sonst → präziser 400er mit exakter Handlungsanweisung.
    """
    kwargs["community"] = wants_community
    changes.append(f"community → {wants_community}")
    if not wants_community:
        return kwargs, changes

    rules = kwargs.get("rules_channel") or guild.rules_channel
    updates = kwargs.get("public_updates_channel") or guild.public_updates_channel

    if parse_bool(data.get("auto_community_channels"), field="auto_community_channels", default=False):
        reason = ctx.reason(data, default="Community-Aktivierung (Arena AI)")
        if rules is None:
            rules = await guard(
                guild.create_text_channel(
                    "regeln",
                    topic="Serverregeln — bitte lesen!",
                    slowmode_delay=0,
                    overwrites={guild.default_role: discord.PermissionOverwrite(send_messages=False)},
                    reason=reason,
                ),
                action="Kanal 'regeln' anlegen",
            )
            changes.append("Kanal 'regeln' automatisch angelegt")
            await ctx.settle(0.4)
        if updates is None:
            updates = await guard(
                guild.create_text_channel(
                    "moderator-updates",
                    topic="Community-Updates von Discord",
                    overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
                    reason=reason,
                ),
                action="Kanal 'moderator-updates' anlegen",
            )
            changes.append("Kanal 'moderator-updates' automatisch angelegt")
            await ctx.settle(0.4)

    if rules is None or updates is None:
        missing = ", ".join(
            name for name, value in (("rules_channel_id", rules), ("public_updates_channel_id", updates))
            if value is None
        )
        raise ApiError.bad_request(
            f"community: true braucht gleichzeitig {missing} — Discord verlangt beide im selben Aufruf.",
            hint='Lösung A: beide Kanäle mitsenden, z. B. {"community": true, '
                 '"rules_channel_id": "…", "public_updates_channel_id": "…"}. '
                 'Lösung B: {"community": true, "auto_community_channels": true} — '
                 "dann legt der Bot 'regeln' und 'moderator-updates' selbst an.",
            code="COMMUNITY_CHANNELS_REQUIRED",
        )

    kwargs["rules_channel"] = rules
    kwargs["public_updates_channel"] = updates
    return kwargs, changes


def _normalize_prompt_type(value: Any) -> Any:
    """``single_choice`` gibt es als Enum nicht — Discord löst das über ``single_select``."""
    if isinstance(value, str):
        lowered = value.strip().lower().replace("-", "_")
        if lowered in {"single_choice", "single", "single_select", "radio"}:
            return "multiple_choice"
        if lowered in {"multi", "multi_choice", "checkbox"}:
            return "multiple_choice"
        if lowered in {"menu", "select"}:
            return "dropdown"
    return value
