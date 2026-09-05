"""
Mitglieder: suchen, lesen, bearbeiten (Nickname, Rollen, Timeout, Voice), kicken.

Suchstrategie (wichtig, weil Discord hier restriktiv ist):

1. **Cache** (``guild.members``) — sofort, kostenlos, aber nur vollständig,
   wenn das privilegierte ``GUILD_MEMBERS``-Intent aktiviert ist.
2. **Gateway-Suche** (``guild.query_members``) — funktioniert auch ohne
   Intent, liefert bis zu 100 Treffer und füllt dabei den Cache.
3. **REST** (``guild.fetch_members``) — für vollständige Listen/Paginierung.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import discord

from ...serializers import serialize_member, serialize_voice_state
from ...util import (
    ApiError,
    iso,
    now_utc,
    parse_bool,
    parse_datetime,
    parse_str,
    parse_timedelta,
    resolve_role,
    sf,
)
from ..channel_ops import resolve_channel_by_ref
from ..context import Ctx, guard
from ..images import resolve_image
from ..registry import route


# ─────────────────────────────────────────────────────────────────────────────
#  Such-Helfer
# ─────────────────────────────────────────────────────────────────────────────


def _matches(member: discord.Member, needle: str) -> bool:
    lowered = needle.lower()
    candidates = {
        member.name,
        member.display_name,
        getattr(member, "nick", None) or "",
        getattr(member, "global_name", None) or "",
        str(member.id),
    }
    return any(lowered in (value or "").lower() for value in candidates)


async def search_members(
    guild: discord.Guild, query: str, *, limit: int = 25, include_bots: bool = True
) -> List[discord.Member]:
    found: List[discord.Member] = []
    seen = set()

    for member in guild.members:
        if _matches(member, query) and (include_bots or not member.bot):
            if member.id not in seen:
                found.append(member)
                seen.add(member.id)
        if len(found) >= limit:
            return found

    if len(found) < limit:
        try:
            async_found = await guild.query_members(query=query, limit=min(limit, 100), cache=True)
            for member in async_found:
                if member.id in seen:
                    continue
                if not include_bots and member.bot:
                    continue
                found.append(member)
                seen.add(member.id)
                if len(found) >= limit:
                    break
        except discord.HTTPException:
            pass

    return found[:limit]


# ─────────────────────────────────────────────────────────────────────────────
#  Lesen
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/members", scope="read", tags=("members",),
    summary="Mitgliederliste (Cache, Suche oder REST)",
    query={
        "query": "Suchbegriff (Name, Nickname, ID)",
        "limit": "int, Standard 100 (max. 1000)",
        "after": "ID — Paginierung (nur ohne query)",
        "role": "Rollen-ID oder Name — filtert auf diese Rolle",
        "bots": "true|false — nur Bots / nur Menschen",
        "sort": "'name'|'joined'|'id' (Standard name)",
        "online": "true — nur nicht-offline Mitglieder",
    },
    description="Ohne ``query`` wird aus dem Cache gelesen und bei Bedarf per REST "
                "aufgefüllt. Mit ``query`` zusätzlich über das Gateway gesucht.",
)
async def list_members(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    query = ctx.q("query")
    limit = ctx.q_int("limit", 100, minimum=1, maximum=ctx.config.max_member_fetch) or 100
    after = ctx.q_id("after")
    role_filter = ctx.q("role")
    bots = ctx.q_bool("bots")
    sort = (ctx.q("sort") or "name").lower()
    online_only = ctx.q_bool("online", False)

    role: Optional[discord.Role] = None
    if role_filter:
        role = resolve_role(guild, role_filter)

    if query:
        members = await search_members(guild, query, limit=limit, include_bots=bots is not False)
        source = "cache+gateway"
    else:
        members = list(guild.members)
        source = "cache"
        if len(members) < min(limit, guild.member_count or limit) and not guild.chunked:
            collected: List[discord.Member] = []
            try:
                async for member in guild.fetch_members(limit=limit, after=discord.Object(id=after) if after else None):
                    collected.append(member)
                    if len(collected) >= limit:
                        break
                members = collected
                source = "rest"
            except discord.Forbidden:
                members = list(guild.members)
                source = "cache (GUILD_MEMBERS-Intent fehlt für REST)"
            except discord.HTTPException:
                members = list(guild.members)
                source = "cache (REST fehlgeschlagen)"

    if role is not None:
        members = [m for m in members if role in m.roles]
    if bots is True:
        members = [m for m in members if m.bot]
    elif bots is False:
        members = [m for m in members if not m.bot]
    if online_only:
        members = [m for m in members if getattr(m, "status", None) not in (None, discord.Status.offline)]

    if sort == "joined":
        members.sort(key=lambda m: m.joined_at or now_utc())
    elif sort == "id":
        members.sort(key=lambda m: m.id)
    else:
        members.sort(key=lambda m: (m.bot, (m.display_name or m.name).lower()))

    total = len(members)
    members = members[:limit]

    return {
        "source": source,
        "returned": len(members),
        "matched": total,
        "truncated": total > len(members),
        "guild_member_count": guild.member_count,
        "cached_member_count": len(guild.members),
        "filter": {
            "query": query, "role": role.name if role else None,
            "bots": bots, "online": online_only, "sort": sort,
        },
        "note": None if guild.chunked or source.startswith("rest") else
                "Der Cache ist möglicherweise unvollständig. Für die komplette Liste: "
                "?limit=1000 (nutzt REST) — setzt das GUILD_MEMBERS-Intent voraus.",
        "members": [serialize_member(m) for m in members],
    }


@route(
    "GET", "/api/v1/members/search", scope="read", tags=("members",),
    summary="Mitglieder nach Name/Nickname suchen",
    query={"q": "Suchbegriff (Pflicht)", "limit": "int, Standard 25 (max. 100)"},
)
async def members_search(ctx: Ctx) -> Dict[str, Any]:
    query = ctx.q("q") or ctx.q("query")
    if not query:
        raise ApiError.bad_request("?q fehlt.", hint="Beispiel: /api/v1/members/search?q=tim&limit=10")
    limit = ctx.q_int("limit", 25, minimum=1, maximum=100) or 25
    members = await search_members(ctx.guild, query, limit=limit)
    return {"query": query, "count": len(members),
            "members": [serialize_member(m) for m in members]}


@route(
    "GET", "/api/v1/members/me", scope="read", tags=("members",),
    summary="Der Bot als Mitglied dieses Servers",
)
async def member_me(ctx: Ctx) -> Dict[str, Any]:
    me = ctx.guild.me
    if me is None:
        raise ApiError.conflict("Der Bot ist auf diesem Server nicht als Mitglied zwischengespeichert.",
                                code="BOT_MEMBER_MISSING")
    data = serialize_member(me) or {}
    data["top_role_position"] = me.top_role.position
    data["note"] = "Rollen mit position >= top_role_position kann der Bot NICHT verwalten."
    return data


@route(
    "GET", "/api/v1/members/{member_id}", scope="read", tags=("members",),
    summary="Einzelnes Mitglied (lädt bei Bedarf per REST nach)",
    query={"permissions_in": "Kanal-ID — effektive Rechte in diesem Kanal"},
)
async def get_member(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    data = serialize_member(member) or {}

    channel_ref = ctx.q("permissions_in")
    if channel_ref:
        channel = resolve_channel_by_ref(guild, channel_ref, field="?permissions_in")
        perms = channel.permissions_for(member)
        from ...util import permission_names

        data["permissions_in_channel"] = {
            "channel": {"id": sf(channel.id), "name": getattr(channel, "name", None)},
            "value": perms.value,
            "names": permission_names(perms),
        }
    return data


@route(
    "GET", "/api/v1/members/{member_id}/permissions", scope="read", tags=("members", "permissions"),
    summary="Effektive Rechte eines Mitglieds (serverweit oder pro Kanal)",
    query={"channel": "Kanal-ID/Name — optional"},
)
async def member_permissions(ctx: Ctx) -> Dict[str, Any]:
    from ...util import permission_names

    guild = ctx.guild
    member = await ctx.member()
    perms = member.guild_permissions
    result: Dict[str, Any] = {
        "member": {"id": sf(member.id), "display_name": member.display_name},
        "guild_permissions": {"value": perms.value, "names": permission_names(perms),
                              "administrator": bool(perms.administrator)},
    }
    channel_ref = ctx.q("channel")
    if channel_ref:
        channel = resolve_channel_by_ref(guild, channel_ref, field="?channel")
        channel_perms = channel.permissions_for(member)
        result["channel"] = {
            "id": sf(channel.id),
            "name": getattr(channel, "name", None),
            "value": channel_perms.value,
            "names": permission_names(channel_perms),
            "administrator": bool(channel_perms.administrator),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Bearbeiten
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "PATCH", "/api/v1/members/{member_id}", scope="write", tags=("members",),
    summary="Mitglied bearbeiten (Nickname, Rollen, Timeout, Voice …)",
    body={
        "nick": "str | null — Server-Nickname (braucht manage_nicknames)",
        "roles": '["Rollen-ID"] — ersetzt die komplette Rollenliste',
        "roles_add": '["Rollen-ID oder Name"]',
        "roles_remove": '["Rollen-ID oder Name"]',
        "mute": "bool — Server-Mute (Voice)",
        "deafen": "bool — Server-Deafen (Voice)",
        "suppress": "bool — auf Bühne stummschalten",
        "voice_channel": "Kanal-ID | null — verschieben / aus Voice werfen",
        "timeout_duration": "'30m' | Sekunden | ISO-Datum — Timeout setzen",
        "timeout_until": "ISO-Datum | null — Timeout setzen/aufheben",
        "bypass_verification": "bool — Mitgliedschafts-Gating überspringen",
        "avatar": "URL/Data-URI | null — Server-Avatar",
        "banner": "URL/Data-URI | null — Server-Banner",
        "bio": "str | null — Über mich (Server-Profil)",
        "reason": "str",
    },
)
async def patch_member(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    data = await ctx.body()
    if not data:
        raise ApiError.bad_request("Leerer Body — mindestens ein Feld angeben.")

    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "nick" in data:
        nick = data["nick"]
        if nick is None or (isinstance(nick, str) and not nick.strip()):
            kwargs["nick"] = None
            changes.append("nick → entfernt")
        else:
            kwargs["nick"] = parse_str(nick, field="nick", min_length=1, max_length=32, allow_empty=False)
            changes.append(f"nick → {kwargs['nick']}")

    if "roles" in data:
        raw = data["roles"]
        if not isinstance(raw, list):
            raise ApiError.bad_request("roles muss eine Liste von Rollen-IDs/Namen sein.")
        roles = [resolve_role(guild, item) for item in raw]
        kwargs["roles"] = roles
        changes.append(f"roles → {len(roles)} Rolle(n) gesetzt")
    else:
        add = data.get("roles_add")
        remove = data.get("roles_remove")
        if add or remove:
            current = [r for r in member.roles if not r.is_default()]
            if remove:
                for item in remove:
                    role = resolve_role(guild, item)
                    if role.is_default():
                        raise ApiError.bad_request("@everyone kann nicht entzogen werden.")
                    current = [r for r in current if r.id != role.id]
                    changes.append(f"- Rolle '{role.name}'")
            if add:
                for item in add:
                    role = resolve_role(guild, item)
                    if role.is_default():
                        continue
                    if role not in current:
                        current.append(role)
                        changes.append(f"+ Rolle '{role.name}'")
            kwargs["roles"] = current

    for key, label in (("mute", "mute"), ("deafen", "deafen"), ("suppress", "suppress"),
                       ("bypass_verification", "bypass_verification")):
        if key in data:
            kwargs[key] = bool(parse_bool(data[key], field=key))
            changes.append(f"{label} → {kwargs[key]}")

    if "voice_channel" in data:
        raw = data["voice_channel"]
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"none", "null", "disconnect", "remove"}):
            kwargs["voice_channel"] = None
            changes.append("voice → getrennt")
        else:
            channel = resolve_channel_by_ref(
                guild, raw, field="voice_channel",
                kinds=(discord.VoiceChannel, discord.StageChannel),
            )
            kwargs["voice_channel"] = channel
            changes.append(f"voice → #{channel.name}")

    timeout_until = None
    if data.get("timeout_duration") is not None:
        delta = parse_timedelta(data["timeout_duration"], field="timeout_duration")
        if delta is None:
            raise ApiError.bad_request("timeout_duration ist leer.")
        timeout_until = now_utc() + delta
        changes.append(f"timeout → {delta}")
    elif "timeout_until" in data:
        timeout_until = parse_datetime(data["timeout_until"], field="timeout_until")
        changes.append("timeout_until → " + (iso(timeout_until) or "aufgehoben"))
    if "timeout_until" in data or data.get("timeout_duration") is not None:
        kwargs["timed_out_until"] = timeout_until

    for key in ("avatar", "banner"):
        if key in data:
            kwargs[key] = await resolve_image(data[key], session=ctx.http_session(), field=key)
            changes.append(f"{key} → {'entfernt' if kwargs[key] is None else 'gesetzt'}")

    if "bio" in data:
        kwargs["bio"] = parse_str(data["bio"], field="bio", max_length=190)
        changes.append("bio")

    kwargs["reason"] = ctx.reason(data, default=f"Mitglied {member.display_name} bearbeitet (Arena AI)")

    if not changes:
        raise ApiError.bad_request(
            "Keine änderbaren Felder gefunden.",
            hint="Unterstützt: nick, roles, roles_add, roles_remove, mute, deafen, suppress, "
                 "voice_channel, timeout_duration, timeout_until, bypass_verification, "
                 "avatar, banner, bio.",
            code="NO_CHANGES",
        )

    if member.id == (ctx.client.user.id if ctx.client.user else 0):
        raise ApiError.bad_request(
            "Der Bot kann sich nicht selbst bearbeiten (Timeout/Rollen-Konflikte).",
            code="SELF_TARGET",
        )
    if member.id == guild.owner_id and any(
        k in kwargs for k in ("timed_out_until", "roles")
    ):
        raise ApiError.forbidden(
            "Der Server-Owner kann nicht getimet oder in der Rollenliste geändert werden.",
            code="OWNER_PROTECTED",
        )

    updated = await guard(member.edit(**kwargs), action=f"Mitglied '{member.display_name}' bearbeiten")
    await ctx.settle(0.5)
    fresh = guild.get_member(member.id) or updated or member
    return {"changed": changes, "member": serialize_member(fresh), "id": sf(fresh.id)}


@route(
    "POST", "/api/v1/members/{member_id}/timeout", scope="manage", tags=("members", "moderation"),
    summary="Timeout verhängen oder aufheben",
    body={
        "duration": "'10m' | '1h' | Sekunden | ISO-Datum — null hebt auf",
        "reason": "str",
    },
    description="Discord erlaubt Timeouts bis maximal 28 Tage. Der Bot braucht "
                "'moderate_members' und eine Rolle über der Zielrolle.",
)
async def set_timeout(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    data = await ctx.body()
    reason = ctx.reason(data, default=f"Timeout für {member.display_name} (Arena AI)")

    if "duration" not in data and "timeout_duration" not in data and "until" not in data:
        raise ApiError.bad_request(
            "'duration' fehlt.",
            hint='Beispiel: {"duration": "10m", "reason": "Spam"} — null hebt den Timeout auf.',
        )
    raw = data.get("duration", data.get("timeout_duration", data.get("until")))
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"none", "null", "remove", "aufheben"}):
        await guard(member.timeout(None, reason=reason or "Timeout aufgehoben (Arena AI)"),
                    action=f"Timeout von '{member.display_name}' aufheben")
        await ctx.settle(0.4)
        fresh = guild.get_member(member.id) or member
        return {"member": serialize_member(fresh), "timed_out": False,
                "timed_out_until": None}

    delta = parse_timedelta(raw, field="duration")
    if delta is None:
        raise ApiError.bad_request("duration ist ungültig.")
    if delta.total_seconds() > 28 * 24 * 3600:
        raise ApiError.bad_request(
            "Timeouts dürfen maximal 28 Tage dauern.",
            hint="Für längere Ausschlüsse: PUT /api/v1/bans/{user_id}",
            code="TIMEOUT_TOO_LONG",
        )
    if delta.total_seconds() <= 0:
        raise ApiError.bad_request("duration muss in der Zukunft liegen.",
                                   hint="Nutze null, um einen Timeout aufzuheben.")

    await guard(member.timeout(delta, reason=reason), action=f"Timeout für '{member.display_name}'")
    await ctx.settle(0.4)
    fresh = guild.get_member(member.id) or member
    return {
        "member": serialize_member(fresh),
        "timed_out": True,
        "timed_out_until": iso(fresh.timed_out_until),
        "duration_seconds": round(delta.total_seconds()),
    }


@route(
    "DELETE", "/api/v1/members/{member_id}/timeout", scope="manage", tags=("members", "moderation"),
    summary="Timeout aufheben",
    query={"reason": "str"},
)
async def remove_timeout(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    await guard(member.timeout(None, reason=ctx.reason(default="Timeout aufgehoben (Arena AI)")),
                action=f"Timeout von '{member.display_name}' aufheben")
    await ctx.settle(0.4)
    fresh = guild.get_member(member.id) or member
    return {"member": serialize_member(fresh), "timed_out": False}


@route(
    "DELETE", "/api/v1/members/{member_id}", scope="manage", tags=("members", "moderation"),
    summary="Mitglied kicken",
    body={"reason": "str", "confirm": "bool"},
    query={"confirm": "true", "reason": "str"},
    description="Entfernt das Mitglied vom Server. Es kann mit einem neuen Invite "
                "wieder beitreten.",
)
async def kick_member(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    confirmed = ctx.q_bool("confirm", False)
    reason = ctx.reason(default=None)
    try:
        data = await ctx.body()
        confirmed = confirmed or bool(parse_bool(data.get("confirm"), field="confirm", default=False))
        reason = ctx.reason(data, default=reason)
    except ApiError:
        pass

    if member.id == guild.owner_id:
        raise ApiError.forbidden("Der Server-Owner kann nicht gekickt werden.", code="OWNER_PROTECTED")
    if member.id == (ctx.client.user.id if ctx.client.user else 0):
        raise ApiError.bad_request("Der Bot kann sich nicht selbst kicken.", code="SELF_TARGET")
    if not confirmed:
        raise ApiError.bad_request(
            f"{member.display_name} ({member.id}) kicken?",
            hint='Bestätige mit ?confirm=true oder {"confirm": true}.',
            code="CONFIRM_REQUIRED",
            member=member.display_name,
        )

    info = serialize_member(member)
    await guard(guild.kick(member, reason=reason or f"{member.display_name} gekickt (Arena AI)"),
                action=f"'{member.display_name}' kicken")
    await ctx.settle(0.6)
    return {"kicked": {"id": sf(member.id), "display_name": info.get("display_name")},
            "member_count": guild.member_count}


@route(
    "POST", "/api/v1/members/{member_id}/nick", scope="write", tags=("members",),
    summary="Server-Nickname setzen oder entfernen",
    body={"nick": "str | null", "reason": "str"},
)
async def set_nick(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    data = await ctx.body()
    nick = data.get("nick")
    nick_value = None if nick in (None, "") else parse_str(nick, field="nick", min_length=1,
                                                           max_length=32, allow_empty=False)
    await guard(
        member.edit(nick=nick_value, reason=ctx.reason(data, default="Nickname geändert (Arena AI)")),
        action=f"Nickname von '{member.display_name}' setzen",
    )
    await ctx.settle(0.4)
    fresh = guild.get_member(member.id) or member
    return {"id": sf(fresh.id), "nick": getattr(fresh, "nick", None),
            "display_name": fresh.display_name, "member": serialize_member(fresh)}


# ─────────────────────────────────────────────────────────────────────────────
#  Voice
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "POST", "/api/v1/members/{member_id}/voice", scope="manage", tags=("members", "voice"),
    summary="Mitglied im Voice verschieben / muten / deafen / trennen",
    body={
        "channel": "Ziel-Kanal (ID/Name) oder null = trennen",
        "mute": "bool", "deafen": "bool", "suppress": "bool (Bühne)",
        "reason": "str",
    },
)
async def member_voice(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    member = await ctx.member()
    data = await ctx.body()
    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "channel" in data:
        raw = data["channel"]
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"none", "null", "disconnect"}):
            kwargs["voice_channel"] = None
            changes.append("getrennt")
        else:
            channel = resolve_channel_by_ref(
                guild, raw, field="channel", kinds=(discord.VoiceChannel, discord.StageChannel)
            )
            kwargs["voice_channel"] = channel
            changes.append(f"→ #{channel.name}")

    for key in ("mute", "deafen", "suppress"):
        if key in data:
            kwargs[key] = bool(parse_bool(data[key], field=key))
            changes.append(f"{key}={kwargs[key]}")

    if not kwargs:
        raise ApiError.bad_request("Nichts zu tun: 'channel', 'mute', 'deafen' oder 'suppress' angeben.")
    kwargs["reason"] = ctx.reason(data, default=f"Voice-Aktion für {member.display_name} (Arena AI)")

    await guard(member.edit(**kwargs), action=f"Voice von '{member.display_name}' ändern")
    await ctx.settle(0.5)
    fresh = guild.get_member(member.id) or member
    return {"changes": changes, "voice": serialize_voice_state(fresh), "member": serialize_member(fresh)}
