"""
Moderation: Banns und AutoMod-Regeln.

AutoMod ist bewusst vollständig modelliert — es ist der Hebel, mit dem eine KI
einen Server wirklich "sicher" machen kann (Wortfilter, Spam, Mention-Raid,
Link-Schutz), ohne dass ein Mensch 20 Regeln von Hand klickt.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List

import discord

from ...serializers import serialize_automod_rule, serialize_ban
from ...util import ApiError, as_id, parse_bool, parse_int, parse_str, parse_timedelta, sf
from ..channel_ops import resolve_channel_by_ref
from ..context import Ctx, guard
from ..registry import route

_PRESET_FLAGS = ("profanity", "sexual_content", "slurs")

_TRIGGER_HINT = (
    "Erlaubte trigger_type: 'keyword' (keyword_filter/regex_patterns), "
    "'keyword_preset' (presets: profanity/sexual_content/slurs), 'spam', "
    "'mention_spam' (mention_limit), 'member_profile', 'harmful_link'."
)


# ─────────────────────────────────────────────────────────────────────────────
#  Banns
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "GET", "/api/v1/bans", scope="manage", tags=("moderation",),
    summary="Bann-Liste",
    query={"limit": "int, Standard 100 (max. 1000)", "before": "User-ID", "after": "User-ID"},
)
async def list_bans(ctx: Ctx) -> Dict[str, Any]:
    limit = ctx.q_int("limit", 100, minimum=1, maximum=1000) or 100
    before = ctx.q_id("before")
    after = ctx.q_id("after")
    kwargs: Dict[str, Any] = {"limit": limit}
    if before:
        kwargs["before"] = discord.Object(id=before)
    if after:
        kwargs["after"] = discord.Object(id=after)

    bans: List[Dict[str, Any]] = []
    async for entry in ctx.guild.bans(**kwargs):
        bans.append(serialize_ban(entry))
    return {"count": len(bans), "bans": bans}


@route(
    "GET", "/api/v1/bans/{user_id}", scope="manage", tags=("moderation",),
    summary="Ist dieser Nutzer gebannt?",
)
async def get_ban(ctx: Ctx) -> Dict[str, Any]:
    user_id = ctx.path_id("user_id")
    try:
        entry = await guard(ctx.guild.fetch_ban(discord.Object(id=user_id)), action="Bann abrufen")
    except ApiError as exc:
        if exc.code == "DISCORD_NOT_FOUND":
            return {"banned": False, "user_id": sf(user_id)}
        raise
    return {"banned": True, "user_id": sf(user_id), **serialize_ban(entry)}


@route(
    "PUT", "/api/v1/bans/{user_id}", scope="manage", tags=("moderation",),
    summary="Nutzer bannen (auch ohne Mitgliedschaft, per ID)",
    body={
        "reason": "str",
        "delete_message_seconds": "int 0–604800 (7 Tage) — Nachrichten löschen",
        "confirm": "bool — Schutz vor Versehen",
    },
    description="Funktioniert auch für Nutzer, die nicht (mehr) Mitglied sind — "
                "einfach die ID angeben. Der Bot braucht 'ban_members' und eine "
                "Rolle über der des Ziels.",
)
async def ban_user(ctx: Ctx) -> Dict[str, Any]:
    guild = ctx.guild
    user_id = ctx.path_id("user_id")
    data = await ctx.body()

    confirmed = bool(parse_bool(data.get("confirm"), field="confirm", default=False)) or ctx.q_bool("confirm", False)
    if not confirmed:
        target = guild.get_member(user_id)
        raise ApiError.bad_request(
            f"Nutzer {user_id}"
            + (f" ({target.display_name})" if target else "")
            + " dauerhaft bannen?",
            hint='Bestätige mit {"confirm": true}. Nachrichten löschen: '
                 '"delete_message_seconds": 86400.',
            code="CONFIRM_REQUIRED",
        )

    if user_id == guild.owner_id:
        raise ApiError.forbidden("Der Server-Owner kann nicht gebannt werden.", code="OWNER_PROTECTED")
    if user_id == (ctx.client.user.id if ctx.client.user else 0):
        raise ApiError.bad_request("Der Bot kann sich nicht selbst bannen.", code="SELF_TARGET")

    delete_seconds = parse_int(
        data.get("delete_message_seconds", data.get("delete_message_days")),
        field="delete_message_seconds", default=0, minimum=0, maximum=604800,
    )
    if "delete_message_days" in data and "delete_message_seconds" not in data:
        days = parse_int(data["delete_message_days"], field="delete_message_days",
                         minimum=0, maximum=7) or 0
        delete_seconds = days * 86400

    reason = ctx.reason(data, default=f"Ban {user_id} (Arena AI)")
    target: Any = guild.get_member(user_id) or discord.Object(id=user_id)
    await guard(
        guild.ban(target, reason=reason, delete_message_seconds=delete_seconds or 0),
        action=f"Nutzer {user_id} bannen",
    )
    await ctx.settle(0.7)
    return {
        "banned": True,
        "user_id": sf(user_id),
        "display_name": getattr(target, "display_name", None),
        "reason": reason,
        "deleted_message_seconds": delete_seconds,
        "member_count": guild.member_count,
    }


@route(
    "DELETE", "/api/v1/bans/{user_id}", scope="manage", tags=("moderation",),
    summary="Bann aufheben",
    body={"reason": "str"},
)
async def unban_user(ctx: Ctx) -> Dict[str, Any]:
    user_id = ctx.path_id("user_id")
    data = await ctx.body()
    await guard(
        ctx.guild.unban(discord.Object(id=user_id),
                        reason=ctx.reason(data, default=f"Bann {user_id} aufgehoben (Arena AI)")),
        action=f"Bann von {user_id} aufheben",
    )
    await ctx.settle(0.5)
    return {"unbanned": True, "user_id": sf(user_id)}


# ─────────────────────────────────────────────────────────────────────────────
#  AutoMod
# ─────────────────────────────────────────────────────────────────────────────


def _build_trigger(data: Dict[str, Any], *, field: str = "trigger") -> discord.AutoModTrigger:
    """
    Baut einen ``AutoModTrigger`` aus JSON.

    Unterstützt Klartext-Formate::

        {"trigger_type": "keyword", "keyword_filter": ["werbung", "casino"],
         "regex_patterns": ["discord\\\\.gg/[a-zA-Z0-9]+"], "allow_list": ["eigener.link"]}
        {"trigger_type": "keyword_preset", "presets": ["profanity", "slurs"]}
        {"trigger_type": "spam"}
        {"trigger_type": "mention_spam", "mention_limit": 5}
    """
    raw_type = data.get("trigger_type") or data.get("type") or "keyword"
    trigger_type = None
    if raw_type is not None:
        from ...util import parse_enum

        trigger_type = parse_enum(
            discord.AutoModRuleTriggerType, raw_type, field=f"{field}.trigger_type",
            extra={"keywords": "keyword", "keyword_list": "keyword", "word_filter": "keyword",
                   "preset": "keyword_preset", "presets": "keyword_preset",
                   "mention_total_limit": "mention_spam", "mentions": "mention_spam",
                   "spam_link": "harmful_link", "mention_raid": "mention_spam"},
        )

    kwargs: Dict[str, Any] = {}
    if trigger_type is not None:
        kwargs["type"] = trigger_type

    if data.get("keyword_filter") is not None:
        words = data["keyword_filter"]
        if isinstance(words, str):
            words = [w.strip() for w in words.split(",") if w.strip()]
        if not isinstance(words, list):
            raise ApiError.bad_request("keyword_filter muss eine Liste von Wörtern sein.")
        if len(words) > 1000:
            raise ApiError.bad_request("keyword_filter: Discord erlaubt maximal 1000 Einträge.")
        kwargs["keyword_filter"] = [str(w).lower()[:60] for w in words]

    if data.get("regex_patterns") is not None:
        patterns = data["regex_patterns"]
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            raise ApiError.bad_request("regex_patterns muss eine Liste sein.")
        if len(patterns) > 10:
            raise ApiError.bad_request("regex_patterns: Discord erlaubt maximal 10 Muster.")
        import re as _re

        for pattern in patterns:
            try:
                _re.compile(str(pattern))
            except _re.error as exc:
                raise ApiError.bad_request(
                    f"regex_patterns: '{pattern}' ist kein gültiger regulärer Ausdruck ({exc}).",
                    code="REGEX_INVALID",
                ) from exc
        kwargs["regex_patterns"] = [str(p) for p in patterns]

    if data.get("allow_list") is not None:
        allowed = data["allow_list"]
        if isinstance(allowed, str):
            allowed = [a.strip() for a in allowed.split(",") if a.strip()]
        if not isinstance(allowed, list):
            raise ApiError.bad_request("allow_list muss eine Liste sein.")
        if len(allowed) > 100:
            raise ApiError.bad_request("allow_list: Discord erlaubt maximal 100 Einträge.")
        kwargs["allow_list"] = [str(a) for a in allowed]

    if data.get("presets") is not None:
        presets = discord.AutoModPresets.none()
        for name in data["presets"] if isinstance(data["presets"], list) else [data["presets"]]:
            key = str(name).strip().lower().replace("-", "_")
            if key == "all" or key == "alle":
                presets = discord.AutoModPresets.all()
                break
            if key not in _PRESET_FLAGS:
                raise ApiError.bad_request(
                    f"presets: '{name}' ist unbekannt.",
                    hint=f"Erlaubt: {', '.join(_PRESET_FLAGS)}, 'all'.",
                    code="PRESET_INVALID",
                )
            setattr(presets, key, True)
        kwargs["presets"] = presets

    if data.get("mention_limit") is not None:
        limit = parse_int(data["mention_limit"], field="mention_limit", minimum=0, maximum=50)
        kwargs["mention_limit"] = limit

    # Nicht jeder Trigger-Typ braucht eine Zusatzregel: 'spam', 'harmful_link'
    # und 'member_profile' sind bei Discord schon mit dem reinen Typ gültig.
    # Nur diese drei hier verlangen wirklich ein Payload — sie pauschal
    # abzuweisen machte {"trigger_type": "spam"} unbenutzbar.
    _TT = discord.AutoModRuleTriggerType
    SELF_SUFFICIENT = {_TT.spam, _TT.harmful_link, _TT.member_profile}
    REQUIRED_PAYLOAD = {
        _TT.keyword: ("keyword_filter", "regex_patterns"),
        _TT.keyword_preset: ("presets",),
        _TT.mention_spam: ("mention_limit",),
    }

    declared = kwargs.get("type")
    provided = set(kwargs) - {"type"}

    if declared is None and not provided:
        raise ApiError.bad_request(
            "trigger braucht mindestens eine Regel (keyword_filter, regex_patterns, "
            "presets oder mention_limit) oder einen trigger_type.",
            hint=_TRIGGER_HINT,
            code="AUTOMOD_TRIGGER_EMPTY",
        )
    if declared in SELF_SUFFICIENT:
        return discord.AutoModTrigger(**kwargs)
    needed = REQUIRED_PAYLOAD.get(declared)
    if needed and not (provided & set(needed)):
        raise ApiError.bad_request(
            f"trigger_type '{getattr(declared, 'name', declared)}' braucht "
            f"{' oder '.join(needed)}.",
            hint=_TRIGGER_HINT,
            code="AUTOMOD_TRIGGER_EMPTY",
        )
    return discord.AutoModTrigger(**kwargs)


def _build_actions(guild: discord.Guild, raw: Any, *, field: str = "actions") -> List[discord.AutoModRuleAction]:
    if raw is None:
        return [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ApiError.bad_request(f"{field} muss eine nicht-leere Liste sein.")
    if len(raw) > 5:
        raise ApiError.bad_request(f"{field}: Discord erlaubt maximal 5 Aktionen pro Regel.")

    actions: List[discord.AutoModRuleAction] = []
    for index, entry in enumerate(raw):
        where = f"{field}[{index}]"
        if isinstance(entry, str):
            entry = {"type": entry}
        if not isinstance(entry, dict):
            raise ApiError.bad_request(f"{where}: erwartet Objekt oder String.")

        from ...util import parse_enum

        action_type = parse_enum(
            discord.AutoModRuleActionType, entry.get("type", "block_message"),
            field=f"{where}.type",
            extra={"block": "block_message", "delete": "block_message", "blocke": "block_message",
                   "alert": "send_alert_message", "warn": "send_alert_message",
                   "log": "send_alert_message", "timeout": "timeout",
                   "mute": "timeout", "block_interactions": "block_member_interactions"},
        )
        kwargs: Dict[str, Any] = {"type": action_type}

        if action_type is discord.AutoModRuleActionType.send_alert_message:
            channel_ref = entry.get("channel") or entry.get("channel_id")
            if not channel_ref:
                raise ApiError.bad_request(
                    f"{where}: 'send_alert_message' braucht einen Zielkanal.",
                    hint='Beispiel: {"type": "send_alert_message", "channel": "mod-log"}',
                    code="AUTOMOD_ALERT_CHANNEL_MISSING",
                )
            channel = resolve_channel_by_ref(
                guild, channel_ref, field=f"{where}.channel", kinds=(discord.TextChannel,)
            )
            kwargs["channel_id"] = channel.id

        if action_type is discord.AutoModRuleActionType.timeout:
            duration = parse_timedelta(
                entry.get("duration", entry.get("timeout_duration", "1h")), field=f"{where}.duration"
            )
            if duration is None or duration.total_seconds() <= 0:
                raise ApiError.bad_request(f"{where}.duration muss eine positive Dauer sein.")
            if duration > _dt.timedelta(days=28):
                raise ApiError.bad_request(f"{where}.duration: Timeouts max. 28 Tage.")
            kwargs["duration"] = duration

        custom = parse_str(entry.get("custom_message"), field=f"{where}.custom_message", max_length=150)
        if custom:
            kwargs["custom_message"] = custom

        actions.append(discord.AutoModRuleAction(**kwargs))
    return actions


@route(
    "GET", "/api/v1/automod/rules", scope="manage", tags=("moderation", "automod"),
    summary="Alle AutoMod-Regeln",
)
async def list_automod_rules(ctx: Ctx) -> Dict[str, Any]:
    rules = await guard(ctx.guild.fetch_automod_rules(), action="AutoMod-Regeln laden")
    return {
        "count": len(rules),
        "max_rules": 100,
        "rules": [serialize_automod_rule(r) for r in rules],
        "trigger_types": [str(t.name) for t in discord.AutoModRuleTriggerType],
        "action_types": [str(t.name) for t in discord.AutoModRuleActionType],
    }


@route(
    "POST", "/api/v1/automod/rules", scope="manage", tags=("moderation", "automod"),
    summary="AutoMod-Regel anlegen",
    body={
        "name": "str (Pflicht)",
        "event_type": "'message_send' (Standard) | 'member_update'",
        "enabled": "bool, Standard true",
        "trigger_type": "'keyword'|'keyword_preset'|'spam'|'mention_spam'|'member_profile'|'harmful_link'",
        "keyword_filter": '["casino", "gratis geld"]',
        "regex_patterns": '["discord\\\\.gg/[a-z0-9]+"]',
        "allow_list": '["eigener-server.de"]',
        "presets": '["profanity", "sexual_content", "slurs"] oder "all"',
        "mention_limit": "int 0–50 (bei mention_spam)",
        "actions": '[{"type": "block_message"}, {"type": "send_alert_message", "channel": "mod-log"}, '
                   '{"type": "timeout", "duration": "1h"}]',
        "exempt_roles": '["Rollen-ID/Name"]',
        "exempt_channels": '["Kanal-ID/Name"]',
        "reason": "str",
    },
    examples=[
        {"body": {"name": "Kein Fremd-Werbung", "trigger_type": "keyword",
                  "regex_patterns": ["discord\\.(gg|com)/\\w+", "https?://\\S*invite"],
                  "actions": [{"type": "block_message"},
                              {"type": "send_alert_message", "channel": "mod-log"}]}},
        {"body": {"name": "Beleidigungen", "trigger_type": "keyword_preset",
                  "presets": ["profanity", "slurs"],
                  "actions": [{"type": "block_message"}, {"type": "timeout", "duration": "10m"}]}},
        {"body": {"name": "Mention-Raid", "trigger_type": "mention_spam", "mention_limit": 5,
                  "actions": [{"type": "block_message"}, {"type": "timeout", "duration": "1h"}]}},
    ],
)
async def create_automod_rule(ctx: Ctx) -> Dict[str, Any]:
    data = await ctx.body()
    guild = ctx.guild
    name = parse_str(data.get("name"), field="name", min_length=1, max_length=100, allow_empty=False)
    if not name:
        raise ApiError.bad_request("'name' fehlt.")

    from ...util import parse_enum

    event_type = parse_enum(
        discord.AutoModRuleEventType, data.get("event_type", "message_send"), field="event_type",
        extra={"message": "message_send", "nachricht": "message_send",
               "profile": "member_update", "profil": "member_update"},
        default=discord.AutoModRuleEventType.message_send,
    )
    trigger = _build_trigger(data, field="trigger")
    actions = _build_actions(guild, data.get("actions"))

    exempt_roles: List[discord.abc.Snowflake] = []
    for index, item in enumerate(data.get("exempt_roles") or []):
        from ...util import resolve_role

        exempt_roles.append(resolve_role(guild, item))
    exempt_channels: List[discord.abc.Snowflake] = []
    for item in data.get("exempt_channels") or []:
        exempt_channels.append(resolve_channel_by_ref(guild, item, field="exempt_channels"))

    if len(exempt_roles) > 20:
        raise ApiError.bad_request("exempt_roles: Discord erlaubt maximal 20.")
    if len(exempt_channels) > 50:
        raise ApiError.bad_request("exempt_channels: Discord erlaubt maximal 50.")

    rule = await guard(
        guild.create_automod_rule(
            name=name,
            event_type=event_type or discord.AutoModRuleEventType.message_send,
            trigger=trigger,
            actions=actions,
            enabled=bool(parse_bool(data.get("enabled"), field="enabled", default=True)),
            exempt_roles=exempt_roles or discord.utils.MISSING,
            exempt_channels=exempt_channels or discord.utils.MISSING,
            reason=ctx.reason(data, default=f"AutoMod-Regel '{name}' angelegt (Arena AI)"),
        ),
        action=f"AutoMod-Regel '{name}' anlegen",
    )
    await ctx.settle(0.5)
    return {"created": serialize_automod_rule(rule), "id": sf(rule.id)}


@route(
    "PATCH", "/api/v1/automod/rules/{rule_id}", scope="manage", tags=("moderation", "automod"),
    summary="AutoMod-Regel bearbeiten",
    body={
        "name": "str", "enabled": "bool", "event_type": "'message_send'|'member_update'",
        "trigger": '{"trigger_type": "keyword", "keyword_filter": ["…"]} — komplett ersetzen',
        "keyword_filter": "Direkt auf der vorhandenen Regel ergänzen",
        "actions": "[…]", "exempt_roles": "[…]", "exempt_channels": "[…]", "reason": "str",
    },
)
async def patch_automod_rule(ctx: Ctx) -> Dict[str, Any]:
    rule_id = ctx.path_id("rule_id")
    data = await ctx.body()
    guild = ctx.guild

    rule = await guard(guild.fetch_automod_rule(rule_id), action="AutoMod-Regel laden")
    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "name" in data:
        kwargs["name"] = parse_str(data["name"], field="name", min_length=1, max_length=100,
                                   allow_empty=False) or rule.name
        changes.append("name")
    if "enabled" in data:
        kwargs["enabled"] = bool(parse_bool(data["enabled"], field="enabled"))
        changes.append(f"enabled → {kwargs['enabled']}")
    if "event_type" in data:
        from ...util import parse_enum

        kwargs["event_type"] = parse_enum(discord.AutoModRuleEventType, data["event_type"],
                                          field="event_type")
        changes.append("event_type")

    trigger_source = data.get("trigger") if isinstance(data.get("trigger"), dict) else data
    if any(key in trigger_source for key in
           ("trigger_type", "keyword_filter", "regex_patterns", "presets", "mention_limit", "allow_list")):
        kwargs["trigger"] = _build_trigger(dict(trigger_source), field="trigger")
        changes.append("trigger")

    if "actions" in data:
        kwargs["actions"] = _build_actions(guild, data["actions"])
        changes.append(f"actions ({len(kwargs['actions'])})")

    if "exempt_roles" in data:
        from ...util import resolve_role

        kwargs["exempt_roles"] = [resolve_role(guild, item) for item in data["exempt_roles"]]
        changes.append(f"exempt_roles ({len(kwargs['exempt_roles'])})")
    if "exempt_channels" in data:
        kwargs["exempt_channels"] = [
            resolve_channel_by_ref(guild, item, field="exempt_channels")
            for item in data["exempt_channels"]
        ]
        changes.append(f"exempt_channels ({len(kwargs['exempt_channels'])})")

    if not kwargs:
        raise ApiError.bad_request(
            "Keine änderbaren Felder.",
            hint="Unterstützt: name, enabled, event_type, trigger/keyword_filter/"
                 "regex_patterns/presets/mention_limit, actions, exempt_roles, exempt_channels.",
            code="NO_CHANGES",
        )
    kwargs["reason"] = ctx.reason(data, default=f"AutoMod-Regel '{rule.name}' aktualisiert (Arena AI)")

    updated = await guard(rule.edit(**kwargs), action=f"AutoMod-Regel '{rule.name}' bearbeiten")
    await ctx.settle(0.5)
    return {"changed": changes, "rule": serialize_automod_rule(updated or rule), "id": sf(rule.id)}


@route(
    "DELETE", "/api/v1/automod/rules/{rule_id}", scope="manage", tags=("moderation", "automod"),
    summary="AutoMod-Regel löschen",
    query={"reason": "str"},
)
async def delete_automod_rule(ctx: Ctx) -> Dict[str, Any]:
    rule_id = ctx.path_id("rule_id")
    guild = ctx.guild
    rule = await guard(guild.fetch_automod_rule(rule_id), action="AutoMod-Regel laden")
    await guard(rule.delete(reason=ctx.reason(default=f"AutoMod-Regel '{rule.name}' gelöscht (Arena AI)")),
                action=f"AutoMod-Regel '{rule.name}' löschen")
    await ctx.settle(0.4)
    return {"deleted": {"id": sf(rule.id), "name": rule.name}}


# ─────────────────────────────────────────────────────────────────────────────
#  Massen-Moderation
# ─────────────────────────────────────────────────────────────────────────────


@route(
    "POST", "/api/v1/moderation/mass-timeout", scope="manage", tags=("moderation",),
    summary="Timeout für mehrere Mitglieder gleichzeitig",
    body={
        "member_ids": '["ID", …]',
        "duration": "'10m' | Sekunden | ISO-Datum",
        "reason": "str",
        "confirm": "true",
    },
    description="Für Raid-Aufräumaktionen. Bricht nicht ab, wenn ein Mitglied "
                "nicht erreichbar ist — Fehler erscheinen pro Eintrag.",
)
async def mass_timeout(ctx: Ctx) -> Dict[str, Any]:
    import asyncio

    data = await ctx.body()
    if not parse_bool(data.get("confirm"), field="confirm", default=False):
        raise ApiError.bad_request(
            "Massen-Timeout ist destruktiv.",
            hint='Sende {"member_ids": […], "duration": "10m", "confirm": true}.',
            code="CONFIRM_REQUIRED",
        )
    raw_ids = data.get("member_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ApiError.bad_request("'member_ids' muss eine nicht-leere Liste sein.")
    if len(raw_ids) > 100:
        raise ApiError.bad_request("Maximal 100 Mitglieder pro Aufruf.")

    delta = parse_timedelta(data.get("duration", "10m"), field="duration")
    if delta is None or delta.total_seconds() <= 0:
        raise ApiError.bad_request("duration muss eine positive Dauer sein.")
    if delta > _dt.timedelta(days=28):
        raise ApiError.bad_request("Timeouts dürfen maximal 28 Tage dauern.")

    guild = ctx.guild
    reason = ctx.reason(data, default=f"Massen-Timeout ({len(raw_ids)} Mitglieder, Arena AI)")
    done: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_ids):
        member_id = as_id(raw, field=f"member_ids[{index}]")
        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guard(guild.fetch_member(member_id), action="Mitglied laden")
            except ApiError as exc:
                errors.append({"member_id": sf(member_id), "code": exc.code, "message": exc.message})
                continue
        if member.guild_permissions.administrator:
            errors.append({"member_id": sf(member_id), "code": "ADMIN_PROTECTED",
                           "message": f"{member.display_name} hat Administrator-Rechte — übersprungen."})
            continue
        try:
            await guard(member.timeout(delta, reason=reason), action=f"Timeout für {member.display_name}")
            done.append({"member_id": sf(member_id), "display_name": member.display_name})
        except ApiError as exc:
            errors.append({"member_id": sf(member_id), "code": exc.code, "message": exc.message})
        await asyncio.sleep(0.4)

    await ctx.settle(0.7)
    return {
        "timed_out_count": len(done),
        "failed_count": len(errors),
        "duration_seconds": round(delta.total_seconds()),
        "timed_out": done,
        "errors": errors or None,
    }

