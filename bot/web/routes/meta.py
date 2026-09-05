"""
Meta-Endpoints: Healthcheck, Selbstbeschreibung, Sitzung, Action-Log und der
große Server-Snapshot.

``GET /api/v1/capabilities`` ist der wichtigste Endpoint überhaupt: Er liefert
der KI die **komplette**, immer aktuelle Beschreibung aller Endpoints inkl.
Body-Feldern und Beispielen. Damit ist die API selbsterklärend — die KI muss
nichts vorher wissen.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any, Dict, List, Optional

import discord

from ... import __botname__, __version__
from ...serializers import (
    _asset,
    _enum,
    serialize_automod_rule,
    serialize_channel_tree,
    serialize_emoji,
    serialize_event,
    serialize_guild,
    serialize_invite,
    serialize_member,
    serialize_onboarding,
    serialize_role,
    serialize_sticker,
    serialize_template,
    serialize_welcome_screen,
    serialize_widget,
)
from ...util import iso, sf
from ..context import Ctx
from ..registry import ENDPOINTS, endpoints_by_tag, route

# ─────────────────────────────────────────────────────────────────────────────
#  Health (öffentlich — für UptimeRobot / Render Health Check)
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/health",
    public=True, scope="read", tags=("meta",),
    summary="Healthcheck für UptimeRobot & Render",
    description="Öffentlich, ohne Token. Antwortet in wenigen Millisekunden und hält "
                "den Dienst auf Render Free wach. UptimeRobot sollte genau diese URL "
                "alle 5 Minuten anpingen.",
    response='{"ok":true,"data":{"status":"healthy","bot":"connected","uptime_seconds":123}}',
)
async def health(ctx: Ctx) -> Dict[str, Any]:
    """
    Healthcheck — muss IMMER antworten, auch mitten im Hochfahren.

    UptimeRobot und der Render Health Check schauen genau hierher. Ein 500er
    würde den Free-Plan schlafen legen bzw. das Deploy als fehlerhaft markieren,
    deshalb ist jeder Zugriff auf den (möglicherweise noch nicht verbundenen)
    Discord-Client abgesichert.
    """
    state = ctx.request.app["relay_ctx"]
    client: Optional[discord.Client] = state.client
    user = getattr(client, "user", None) if client is not None else None
    connected = bool(client is not None and user is not None and not client.is_closed())

    return {
        # Für UptimeRobot zählt allein dieses Feld: 'healthy' = Prozess lebt und
        # beantwortet HTTP. Der Discord-Status steht separat darunter.
        "status": "healthy",
        "service": __botname__,
        "version": __version__,
        "bot": "connected" if connected else "connecting",
        "bot_user": user.name if user else None,
        "bot_id": sf(user.id) if user else None,
        "guilds": len(client.guilds) if client is not None else 0,
        "gateway_latency_ms": round(client.latency * 1000, 1) if connected else None,
        "uptime_seconds": round(state.uptime_seconds(), 1),
        "active_sessions": len(state.store.all_active()),
        "requests_served": state.request_count,
        "errors_served": state.error_count,
        "python": platform.python_version(),
        "discord_py": discord.__version__,
        "timestamp": state.now_iso(),
        "base_url": state.base_url(),
    }


@route(
    "GET", "/", public=True, tags=("meta",),
    summary="Landingpage (Weiterleitung zur Console)",
)
async def root(ctx: Ctx) -> Dict[str, Any]:
    state = ctx.request.app["relay_ctx"]
    return {
        "service": __botname__,
        "version": __version__,
        "message": "Dieser Dienst verbindet Arena AI mit deinem Discord-Server. "
                   "Führe auf Discord /connect aus, um Link + Token zu erhalten.",
        "status": "healthy" if getattr(state.client, "user", None) else "connecting",
        "links": {
            "health": f"{state.base_url()}/api/health",
            "capabilities": f"{state.base_url()}/api/v1/capabilities",
            "console": f"{state.base_url()}/console?t=<TOKEN>",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Capabilities — Selbstbeschreibung der API
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/capabilities", scope="read", tags=("meta",), public=True,
    summary="Vollständige, maschinenlesbare API-Beschreibung",
    description="Listet jeden Endpoint mit Methode, Pfad, Beschreibung, benötigtem "
                "Scope, Query-Parametern, Body-Feldern und Beispielen. **Immer als "
                "erstes aufrufen** — daraus ergibt sich alles Weitere.",
    query={"tag": "nur Endpoints eines Tags (z. B. 'channels')", "scope": "max. Scope-Filter"},
    response='{"ok":true,"data":{"endpoints":[…],"conventions":{…}}}',
    examples=[{"curl": 'curl -s "$BASE/api/v1/capabilities" -H "Authorization: Bearer $TOKEN"'}],
)
async def capabilities(ctx: Ctx) -> Dict[str, Any]:
    state = ctx.request.app["relay_ctx"]
    tag_filter = ctx.q("tag")
    scope_filter = ctx.q("scope")

    from ...sessions import SCOPE_LEVELS

    max_level = SCOPE_LEVELS.get(scope_filter or "", 3)
    session_level = ctx.session.scope_level if ctx.session else 0

    endpoints: List[Dict[str, Any]] = []
    for endpoint in sorted(ENDPOINTS, key=lambda e: (e.path, e.method)):
        if tag_filter and tag_filter not in endpoint.tags:
            continue
        level = SCOPE_LEVELS.get(endpoint.scope, 0)
        if level > max_level:
            continue
        item = endpoint.to_dict()
        item["available_with_current_token"] = level <= session_level
        endpoints.append(item)

    grouped = {
        tag: sorted({f"{e.method} {e.path}" for e in eps})
        for tag, eps in sorted(endpoints_by_tag().items())
    }

    base = state.base_url()
    token_hint = ctx.session.token_prefix if ctx.session else "adse_…"

    return {
        "service": __botname__,
        "version": __version__,
        "base_url": base,
        "authentication": {
            "type": "bearer",
            "header": f"Authorization: Bearer {token_hint}",
            "note": "Der Bearer-Wert ist das Sitzungs-Token aus dem Discord-Command /connect. "
                    "Es gilt NUR für diesen Server und läuft ab.",
        },
        "conventions": {
            "response_ok": '{"ok": true, "data": …}',
            "response_error": '{"ok": false, "error": {"code","message","hint","status","details"}}',
            "ids_are_strings": True,
            "timestamps_are_iso8601_utc": True,
            "permissions_as_names": [
                "administrator", "manage_guild", "manage_channels", "manage_roles",
                "manage_messages", "kick_members", "ban_members", "moderate_members",
                "view_channel", "send_messages", "read_message_history", "create_instant_invite",
                "connect", "speak", "stream", "mute_members", "deafen_members", "move_members",
            ],
            "colors_as_hex": "#RRGGBB oder Zahl 0–16777215 oder Farbname ('blurple')",
            "channel_types": ["text", "announcement", "voice", "stage", "forum", "media", "category"],
            "reason_field": 'Fast jeder schreibende Endpoint akzeptiert "reason" für das Audit-Log.',
            "retry_on_429": "Bei 429 error.details.retry_after Sekunden warten.",
        },
        "scopes": {
            "read": "nur GET",
            "write": "anlegen & bearbeiten",
            "manage": "+ Moderation (Timeout, Kick, Ban, AutoMod)",
            "danger": "+ Server-Einstellungen, Prune, Massenlöschung",
        },
        "current_session": ctx.session.to_public_dict() if ctx.session else None,
        "guild": (
            {
                "id": sf(ctx.guild.id),
                "name": ctx.guild.name,
                "member_count": ctx.guild.member_count,
            }
            if ctx.session
            else {
                "note": "Ohne Token sind keine Serverdaten sichtbar. "
                        "Sende 'Authorization: Bearer <TOKEN>' aus dem /connect-Command.",
            }
        ),
        "recommended_workflow": [
            f'GET {base}/api/v1/capabilities  — diese Datei',
            f'GET {base}/api/v1/guild/snapshot — kompletter Ist-Zustand in einem Aufruf',
            f'GET {base}/api/v1/channels/tree  — Kanalstruktur als Baum',
            f'GET {base}/api/v1/roles          — Rollen & deren Permissions',
            f'POST {base}/api/v1/setup         — komplettes Setup in EINEM Aufruf',
            "danach gezielt PATCH/POST/DELETE für Einzeländerungen",
            f'GET {base}/api/v1/guild/snapshot — Erfolg verifizieren',
        ],
        "curl_templates": {
            "get": f'curl -s "{base}/api/v1/<pfad>" -H "Authorization: Bearer <TOKEN>"',
            "post": (
                f'curl -s -X POST "{base}/api/v1/<pfad>" '
                '-H "Authorization: Bearer <TOKEN>" '
                '-H "Content-Type: application/json" '
                """-d '{"name": "allgemein", "type": "text"}'"""
            ),
        },
        "rate_limits": {
            "requests_per_token_per_window": state.config.api_rate_limit,
            "window_seconds": state.config.api_rate_window,
            "counted_by": "Token (nicht IP)",
            "on_429": "error.details.retry_after Sekunden warten und wiederholen.",
            "bulk_alternative": (
                "Sammel-Endpoints zählen als EIN Aufruf und sind für Massenänderungen "
                "gedacht: POST /api/v1/setup, /api/v1/channels/bulk, /api/v1/roles/bulk, "
                "/api/v1/channels/{id}/messages/bulk-delete, "
                "/api/v1/moderation/mass-timeout, PATCH /api/v1/channels/positions."
            ),
            "discord_rate_limits": (
                "Unabhängig davon drosselt Discord selbst (HTTP 429). Das Relay wartet "
                "automatisch und wiederholt — bei vielen Aufrufen trotzdem bündeln."
            ),
        },
        "setup_templates": _setup_template_index(),
        "endpoint_count": len(endpoints),
        "tags": grouped,
        "endpoints": endpoints,
    }


def _setup_template_index() -> List[Dict[str, Any]]:
    """
    Kurzübersicht der fertigen Server-Vorlagen für ``capabilities``.

    Die KI sieht damit sofort, dass es komplette Grundgerüste gibt, statt jeden
    Kanal einzeln anlegen zu müssen. Volle Pläne liefert
    ``GET /api/v1/setup/templates?name=<key>``.
    """
    try:
        from .setup import SETUP_TEMPLATES
    except Exception:  # noqa: BLE001 — capabilities darf daran nie scheitern
        return []
    return [
        {
            "key": key,
            "beschreibung": tpl.get("beschreibung", ""),
            "bausteine": sorted(k for k, v in tpl.items()
                                if isinstance(v, (list, dict)) and v),
            "plan_url": f"GET /api/v1/setup/templates?name={key}",
        }
        for key, tpl in sorted(SETUP_TEMPLATES.items())
        if isinstance(tpl, dict)
    ]


@route(
    "GET", "/api/v1/me", scope="read", tags=("meta",),
    summary="Wer bin ich, wo bin ich, was darf ich?",
    description="Kombinierte Auskunft über Bot, Sitzung und Server — inklusive der "
                "Permission-Liste des Bots und fehlender Rechte.",
)
async def me(ctx: Ctx) -> Dict[str, Any]:
    state = ctx.request.app["relay_ctx"]
    client = state.client
    guild = ctx.guild
    me = guild.me
    perms = me.guild_permissions if me else discord.Permissions.none()

    required = [
        "administrator", "manage_guild", "manage_channels", "manage_roles",
        "manage_webhooks", "manage_expressions", "manage_events", "kick_members",
        "ban_members", "moderate_members", "manage_messages", "view_audit_log",
        "view_channel", "send_messages", "read_message_history", "create_instant_invite",
        "manage_threads", "connect", "speak", "move_members", "mute_members",
        "deafen_members", "request_to_speak", "mention_everyone",
    ]
    missing = [name for name in required if not getattr(perms, name, False)]

    return {
        "service": {"name": __botname__, "version": __version__},
        "bot": {
            "id": sf(client.user.id) if client.user else None,
            "username": client.user.name if client.user else None,
            "discriminator": getattr(client.user, "discriminator", None),
            "avatar_url": _asset(getattr(client.user, "display_avatar", None)) if client.user else None,
            "verified": getattr(client.user, "verified", None),
            "created_at": iso(client.user.created_at) if client.user else None,
            "guilds_visible": len(client.guilds),
            "latency_ms": round(client.latency * 1000, 1) if client.latency else None,
        },
        "session": ctx.session.to_public_dict() if ctx.session else None,
        "guild": {
            "id": sf(guild.id),
            "name": guild.name,
            "icon_url": _asset(getattr(guild, "icon", None)),
            "member_count": guild.member_count,
            "owner_id": sf(guild.owner_id),
            "community": getattr(guild, "community", None),
            "boost_tier": int(_enum(getattr(guild, "premium_tier", 0)) or 0),
            "max_stage_video_channel_users": getattr(guild, "max_stage_video_channel_users", None),
        },
        "permissions": {
            "administrator": bool(perms.administrator),
            "value": perms.value,
            "missing_for_full_control": missing,
            "bot_top_role": me.top_role.name if me else None,
            "bot_top_role_position": me.top_role.position if me else None,
            "note": "Der Bot kann keine Rolle verwalten, deren Position >= seiner eigenen "
                    "Top-Rolle liegt. Rollen also möglichst tief anlegen oder die Bot-Rolle "
                    "nach ganz oben schieben.",
        },
        "base_url": state.base_url(),
        "links": {
            "capabilities": f"{state.base_url()}/api/v1/capabilities",
            "snapshot": f"{state.base_url()}/api/v1/guild/snapshot",
            "console": f"{state.base_url()}/console?t={ctx.session.token_prefix}",
            "invite_bot_with_admin": discord.utils.oauth_url(
                client.user.id if client.user else 0,
                permissions=discord.Permissions(administrator=True),
                scopes=("bot", "applications.commands"),
            ),
        },
        "uptime_seconds": round(state.uptime_seconds(), 1),
    }


@route(
    "GET", "/api/v1/session", scope="read", tags=("meta",),
    summary="Details zur aktuellen Sitzung",
)
async def get_session(ctx: Ctx) -> Dict[str, Any]:
    assert ctx.session is not None
    return {
        "session": ctx.session.to_public_dict(),
        "server_time": ctx.request.app["relay_ctx"].now_iso(),
    }


@route(
    "DELETE", "/api/v1/session", scope="read", tags=("meta",),
    summary="Aktuelle Sitzung widerrufen (Token sofort ungültig)",
    body={"reason": "str (optional) — steht im Log"},
)
async def delete_session(ctx: Ctx) -> Dict[str, Any]:
    assert ctx.session is not None
    data = await ctx.body()
    revoked = await ctx.store.revoke(
        session_id=ctx.session.id, by=f"api:{data.get('reason') or 'self'}"
    )
    return {
        "revoked": [s.to_public_dict() for s in revoked],
        "message": "Sitzung widerrufen. Das Token ist ab sofort ungültig.",
    }


@route(
    "GET", "/api/v1/prompt", scope="read", tags=("meta",),
    summary="Den fertigen Arena-AI-Prompt abrufen",
    description="Gibt exakt den Text zurück, den du in Arena AI einfügst. "
                "Mit ``format=text`` als ``text/plain`` (perfekt für die Console), "
                "sonst als JSON.",
    query={
        "variant": "'short' (Standard, passt in Discord) | 'long' (mit API-Referenz) | 'system'",
        "format": "'json' (Standard) | 'text'",
    },
)
async def get_prompt(ctx: Ctx) -> Any:
    from aiohttp import web

    from ...prompt import PromptContext, build_prompt
    from ...sessions import MODES

    state = ctx.request.app["relay_ctx"]
    session = ctx.session
    assert session is not None
    variant = (ctx.q("variant") or "short").strip().lower()
    if variant not in {"short", "long", "system"}:
        from ...util import ApiError as _ApiError

        raise _ApiError.bad_request(
            f"variant '{variant}' ist unbekannt.",
            hint="Erlaubt: short, long, system.",
            code="PROMPT_VARIANT_INVALID",
        )

    # Der Server speichert nur den Hash des Tokens. Aber: Wer diesen Endpoint
    # aufruft, hat sein Token im Header (oder als ?t=) mitgeschickt — kennt es
    # also längst. Deshalb wird genau dieses Token in den Prompt eingesetzt,
    # wodurch der Text sofort kopier- und ausführbar ist.
    # Fehlt es (z. B. Auth über Cookie), bleibt der Platzhalter stehen.
    presented = ctx.presented_token
    token_text = presented or "<DEIN-TOKEN>"
    guild = ctx.guild
    ctx_data = PromptContext(
        base_url=state.base_url(),
        token=token_text,
        guild_name=guild.name,
        guild_id=guild.id,
        mode_label=MODES.get(session.mode, {}).get("label", session.mode),
        scope=session.scope,
        expires_label=session.to_public_dict()["expires_in"],
        session_id=session.id,
        member_count=guild.member_count,
        console_url=f"{state.base_url()}/console?t={token_text}",
    )
    text = build_prompt(ctx_data, variant=variant)

    # Wenn der Aufrufer sein Token im Header mitgeschickt hat, kennen wir es
    # zwar nicht im Klartext — die Console ersetzt den Platzhalter selbst.
    if (ctx.q("format") or "json").strip().lower() == "text":
        return web.Response(text=text, content_type="text/plain", charset="utf-8",
                            headers={"Cache-Control": "no-store",
                                     "X-Content-Type-Options": "nosniff"})
    return {
        "variant": variant,
        "token_placeholder": "<DEIN-TOKEN>",
        "token_embedded": bool(presented),
        "note": (
            "Der Prompt enthält bereits dein Token — direkt kopierbar." if presented
            else "Ersetze <DEIN-TOKEN> durch dein Sitzungs-Token (steht in der "
                 "Discord-Nachricht von /connect bzw. in der Console im URL-Parameter t)."
        ),
        "length": len(text),
        "text": text,
    }


@route(
    "POST", "/api/v1/session/regenerate", scope="read", tags=("meta",),
    summary="Neues Token erzeugen (das alte wird sofort widerrufen)",
    body={"mode": "optional — read|write|manage|danger (Standard: bisheriger Modus)",
          "ttl_hours": "optional — Gültigkeit in Stunden (0 = unbegrenzt)"},
    description="Praktisch, wenn ein Token versehentlich weitergegeben wurde. "
                "Gibt das **neue Klartext-Token** zurück.",
)
async def regenerate_session(ctx: Ctx) -> Dict[str, Any]:
    from ...prompt import PromptContext, build_prompt
    from ...sessions import MODES

    state = ctx.request.app["relay_ctx"]
    session = ctx.session
    assert session is not None
    data = await ctx.body()

    mode = str(data.get("mode") or session.mode).strip().lower()
    if mode not in MODES:
        from ...util import ApiError as _ApiError

        raise _ApiError.bad_request(
            f"mode '{mode}' ist unbekannt.",
            hint="Erlaubt: " + ", ".join(sorted(MODES)),
            code="MODE_INVALID",
        )

    ttl_hours = data.get("ttl_hours")
    if ttl_hours is None:
        remaining = session.ttl_seconds
        ttl_hours = 0.0 if remaining is None else max(0.0, remaining / 3600.0)

    await ctx.store.revoke(session_id=session.id, by="api:regenerate")
    new_session, token = await ctx.store.create(
        guild_id=session.guild_id,
        guild_name=session.guild_name,
        created_by=session.created_by,
        created_by_name=session.created_by_name,
        mode=mode,
        ttl_hours=float(ttl_hours),
        note=f"erneuert aus Sitzung {session.id}",
    )
    state.maybe_save(force=True)

    guild = ctx.guild
    prompt_ctx = PromptContext(
        base_url=state.base_url(),
        token=token,
        guild_name=guild.name,
        guild_id=guild.id,
        mode_label=MODES[mode]["label"],
        scope=new_session.scope,
        expires_label=new_session.to_public_dict()["expires_in"],
        session_id=new_session.id,
        member_count=guild.member_count,
        console_url=f"{state.base_url()}/console?t={token}",
    )
    return {
        "token": token,
        "session": new_session.to_public_dict(),
        "revoked_session": session.to_public_dict(),
        "console_url": prompt_ctx.console_url,
        "prompt_short": build_prompt(prompt_ctx, variant="short"),
        "prompt_long": build_prompt(prompt_ctx, variant="long"),
        "warning": "Dieses Token steht nur in dieser Antwort. Bitte sicher aufbewahren.",
    }


@route(
    "GET", "/api/v1/actions", scope="read", tags=("meta",),
    summary="Audit-Log der Relay-API (wer hat was aufgerufen?)",
    query={"limit": "int, Standard 50 (max. 400)", "session_id": "nur diese Sitzung"},
)
async def actions(ctx: Ctx) -> Dict[str, Any]:
    limit = ctx.q_int("limit", 50, minimum=1, maximum=400) or 50
    session_id = ctx.q("session_id")
    items = ctx.store.actions(guild_id=ctx.guild.id, session_id=session_id, limit=limit)
    return {"count": len(items), "actions": items}


# ─────────────────────────────────────────────────────────────────────────────
#  Der große Snapshot
# ─────────────────────────────────────────────────────────────────────────────


async def _safe(coro: Any, key: str) -> Any:
    """Führt einen Aufruf aus; Fehler werden zu einer Notiz statt zum Abbruch."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - bewusst breit
        return {"_error": f"{type(exc).__name__}: {exc}", "_key": key}


@route(
    "GET", "/api/v1/guild/snapshot", scope="read", tags=("meta", "guild"),
    summary="Kompletter Ist-Zustand des Servers in EINEM Aufruf",
    description="Der empfohlene Einstieg. Liefert Server-Details, Kanalbaum, Rollen, "
                "Mitglieder, Emojis, Invites, AutoMod-Regeln, Events, Welcome-Screen, "
                "Widget und Onboarding. Fehler in Teilbereichen führen nicht zum "
                "Gesamtabbruch, sondern erscheinen als '_error'.",
    query={
        "members": "int — wie viele Mitglieder (Standard 200, max. 1000, 0 = keine)",
        "include": "kommagetrennte Teilbereiche, z. B. 'guild,channels,roles'",
        "audit": "int — Anzahl Audit-Log-Einträge (Standard 0 = keine)",
    },
)
async def snapshot(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member_limit = ctx.q_int("members", 200, minimum=0, maximum=ctx.config.max_member_fetch) or 0
    audit_limit = ctx.q_int("audit", 0, minimum=0, maximum=100) or 0
    include_raw = ctx.q("include")
    include = None
    if include_raw:
        include = {part.strip().lower() for part in include_raw.split(",") if part.strip()}

    def wanted(name: str) -> bool:
        return include is None or name in include

    tasks: Dict[str, Any] = {}
    if wanted("members") and member_limit > 0:
        tasks["members"] = _safe(_collect_members(guild, member_limit), "members")
    if wanted("invites"):
        tasks["invites"] = _safe(guild.invites(), "invites")
    if wanted("automod"):
        tasks["automod"] = _safe(guild.fetch_automod_rules(), "automod")
    if wanted("emojis"):
        tasks["stickers"] = _safe(guild.fetch_stickers(), "stickers")
    if wanted("events"):
        tasks["events"] = _safe(guild.fetch_scheduled_events(), "events")
    if wanted("welcome"):
        tasks["welcome"] = _safe(guild.welcome_screen(), "welcome")
    if wanted("widget"):
        tasks["widget"] = _safe(guild.widget(), "widget")
    if wanted("onboarding"):
        tasks["onboarding"] = _safe(guild.onboarding(), "onboarding")
    if wanted("vanity") and getattr(guild, "features", None) and "VANITY_URL" in guild.features:
        tasks["vanity"] = _safe(guild.vanity_invite(), "vanity")
    if wanted("templates"):
        tasks["templates"] = _safe(guild.templates(), "templates")
    if audit_limit:
        tasks["audit"] = _safe(_collect_audit(guild, audit_limit), "audit")

    results = await asyncio.gather(*tasks.values()) if tasks else []
    resolved = dict(zip(tasks.keys(), results))

    data: Dict[str, Any] = {
        "guild": serialize_guild(guild),
        "channels": serialize_channel_tree(guild) if wanted("channels") else None,
        "roles": (
            [serialize_role(r) for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)]
            if wanted("roles")
            else None
        ),
        "emojis": [serialize_emoji(e) for e in guild.emojis] if wanted("emojis") else None,
    }

    if "members" in resolved:
        members = resolved["members"]
        if isinstance(members, dict) and "_error" in members:
            data["members"] = members
        else:
            data["members"] = {
                "returned": len(members),
                "total": guild.member_count,
                "truncated": (guild.member_count or 0) > len(members),
                "items": members,
            }

    transformers = {
        "invites": lambda value: [serialize_invite(i) for i in value],
        "automod": lambda value: [serialize_automod_rule(r) for r in value],
        "events": lambda value: [serialize_event(e) for e in value],
        "stickers": lambda value: [serialize_sticker(s) for s in value],
        "templates": lambda value: [serialize_template(t) for t in value],
        "welcome": serialize_welcome_screen,
        "widget": serialize_widget,
        "vanity": lambda value: {"code": getattr(value, "code", None), "uses": getattr(value, "uses", None)},
        "onboarding": serialize_onboarding,
    }
    for key, transformer in transformers.items():
        if key not in resolved:
            continue
        raw = resolved[key]
        if isinstance(raw, dict) and "_error" in raw:
            data[key] = raw
        else:
            try:
                data[key] = transformer(raw)
            except Exception as exc:  # noqa: BLE001 - ein Teilbereich darf den Rest nicht killen
                data[key] = {"_error": f"{type(exc).__name__}: {exc}"}

    if "audit" in resolved:
        data["audit_logs"] = resolved["audit"]

    data["_meta"] = {
        "generated_at": ctx.request.app["relay_ctx"].now_iso(),
        "sections": sorted(k for k, v in data.items() if v is not None and not k.startswith("_")),
        "hint": "Einzelbereiche gezielt nachladen: GET /api/v1/channels/tree, /api/v1/roles, "
                "/api/v1/members?query=…, /api/v1/guild/audit-logs …",
    }
    return data


async def _collect_members(guild: discord.Guild, limit: int) -> List[Dict[str, Any]]:
    """
    Mitglieder sammeln — erst aus dem Cache, bei Bedarf per REST nachladen.

    Der Cache ist nur vollständig, wenn das privilegierte ``GUILD_MEMBERS``-Intent
    aktiviert ist. Ist er das nicht, liefert ``fetch_members`` trotzdem Daten
    (Discord erlaubt den REST-Call), solange das Intent im Developer Portal an ist.
    """
    cached = list(guild.members)
    if len(cached) >= min(limit, guild.member_count or limit) or guild.chunked:
        chosen = cached[:limit]
    else:
        collected: List[discord.Member] = []
        try:
            async for member in guild.fetch_members(limit=limit):
                collected.append(member)
        except discord.Forbidden:
            collected = cached
        except discord.HTTPException:
            collected = cached
        chosen = (collected or cached)[:limit]

    # Menschen zuerst, dann Bots; jeweils alphabetisch → stabil & übersichtlich
    chosen.sort(key=lambda m: (m.bot, (m.display_name or m.name).lower()))
    return [serialize_member(m) for m in chosen]


async def _collect_audit(guild: discord.Guild, limit: int) -> List[Dict[str, Any]]:
    from ...serializers import serialize_audit_entry

    entries: List[Dict[str, Any]] = []
    try:
        async for entry in guild.audit_logs(limit=limit):
            entries.append(serialize_audit_entry(entry))
    except discord.Forbidden:
        return [{"_error": "VIEW_AUDIT_LOG fehlt — Audit-Log nicht lesbar."}]
    return entries
