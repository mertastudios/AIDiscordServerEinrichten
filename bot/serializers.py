"""
Serialisierer: discord.py-Objekte → JSON-sichere Dictionaries.

Konventionen (bewusst gewählt, damit eine KI sich nicht verhaspelt):

* **Alle IDs sind Strings.** JavaScript/JSON verliert ab 2^53 Präzision —
  Snowflakes sind aber 64 Bit. ``"123456789012345678"`` statt ``1234…``.
* **Zeitstempel sind ISO-8601 in UTC** mit ``Z``-Suffix.
* **Enums werden als Name UND Wert** ausgegeben (``"type": 0, "type_name": "text"``),
  wo es beim Lesen hilft.
* **Permissions** kommen als Bitmaske *und* als lesbare Namensliste.
* ``None`` bleibt ``None`` — nichts wird stillschweigend erfunden.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import discord

from .util import iso, permission_names, sf

__all__ = (
    "jsonable",
    "serialize_user",
    "serialize_member",
    "serialize_role",
    "serialize_permissions",
    "serialize_overwrites",
    "serialize_channel",
    "serialize_channel_tree",
    "serialize_guild",
    "serialize_guild_brief",
    "serialize_message",
    "serialize_embed",
    "serialize_invite",
    "serialize_emoji",
    "serialize_sticker",
    "serialize_webhook",
    "serialize_ban",
    "serialize_audit_entry",
    "serialize_automod_rule",
    "serialize_event",
    "serialize_thread",
    "serialize_voice_state",
    "serialize_welcome_screen",
    "serialize_onboarding",
    "serialize_widget",
    "serialize_template",
    "serialize_stage_instance",
)


# ─────────────────────────────────────────────────────────────────────────────
#  Basis
# ─────────────────────────────────────────────────────────────────────────────

_PRIMITIVES = (str, int, float, bool, type(None))


def jsonable(value: Any, *, depth: int = 0) -> Any:
    """Letzte Rettung: macht beliebige Objekte JSON-tauglich."""
    if depth > 8:
        return str(value)
    if isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, (discord.Object, discord.abc.Snowflake)):
        return sf(value.id)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth=depth + 1) for v in value]
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return jsonable(value.value, depth=depth + 1)
    if hasattr(value, "isoformat"):
        return iso(value)
    return str(value)


def _enum(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if hasattr(value, "value"):
        return value.value
    return value


def _name(value: Any) -> Optional[str]:
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _asset(value: Any) -> Optional[str]:
    """``Asset`` → URL (oder ``None``)."""
    if value is None:
        return None
    try:
        return str(value.url)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Nutzer / Mitglieder / Rollen
# ─────────────────────────────────────────────────────────────────────────────


def serialize_user(user: Optional[discord.User]) -> Optional[Dict[str, Any]]:
    if user is None:
        return None
    return {
        "id": sf(user.id),
        "username": user.name,
        "global_name": getattr(user, "global_name", None),
        "discriminator": getattr(user, "discriminator", None),
        "display_name": getattr(user, "display_name", None) or getattr(user, "global_name", None) or user.name,
        "bot": user.bot,
        "system": getattr(user, "system", False),
        "avatar_url": _asset(getattr(user, "display_avatar", None)),
        "banner_url": _asset(getattr(user, "banner", None)),
        "accent_color": getattr(user, "accent_color", None),
        "created_at": iso(user.created_at),
        "mention": user.mention,
    }


def serialize_member(member: Optional[discord.Member]) -> Optional[Dict[str, Any]]:
    if member is None:
        return None
    data: Dict[str, Any] = {
        "id": sf(member.id),
        "username": member.name,
        "global_name": getattr(member, "global_name", None),
        "display_name": member.display_name,
        "nick": getattr(member, "nick", None),
        "bot": member.bot,
        "avatar_url": _asset(getattr(member, "display_avatar", None)),
        "guild_avatar_url": _asset(getattr(member, "guild_avatar", None)),
        "color": getattr(member.color, "value", None),
        "color_hex": str(member.color) if getattr(member, "color", None) else None,
        "created_at": iso(member.created_at),
        "joined_at": iso(getattr(member, "joined_at", None)),
        "premium_since": iso(getattr(member, "premium_since", None)),
        "timed_out_until": iso(getattr(member, "timed_out_until", None)),
        "is_timed_out": bool(getattr(member, "timed_out", False)),
        "pending": getattr(member, "pending", False),
        "flags": getattr(member, "flags", None) and jsonable(member.flags),
        "roles": [
            {"id": sf(r.id), "name": r.name, "color": r.color.value, "position": r.position}
            for r in sorted(getattr(member, "roles", []), key=lambda r: r.position)
            if not r.is_default()
        ],
        "role_ids": [sf(r.id) for r in getattr(member, "roles", []) if not r.is_default()],
        "top_role": sf(member.top_role.id) if getattr(member, "top_role", None) else None,
        "guild_permissions": serialize_permissions(member.guild_permissions),
        "mention": member.mention,
        "voice": serialize_voice_state(member) if getattr(member, "voice", None) else None,
    }
    return data


def serialize_role(role: Optional[discord.Role]) -> Optional[Dict[str, Any]]:
    if role is None:
        return None
    return {
        "id": sf(role.id),
        "name": role.name,
        "color": role.color.value,
        "color_hex": str(role.color) if role.color and role.color.value else None,
        "hoist": role.hoist,
        "position": role.position,
        "managed": role.managed,
        "mentionable": role.mentionable,
        "is_default": role.is_default(),
        "is_bot_managed": getattr(role, "is_bot_managed", lambda: False)(),
        "is_premium_subscriber": getattr(role, "is_premium_subscriber", lambda: False)(),
        "is_integration": getattr(role, "is_integration", lambda: False)(),
        "icon_url": _asset(getattr(role, "icon", None)),
        "unicode_emoji": getattr(role, "unicode_emoji", None),
        "permissions": serialize_permissions(role.permissions),
        "tags": jsonable(getattr(role, "tags", None)),
        "member_count": getattr(role, "member_count", None) if hasattr(role, "member_count") else None,
        "mention": role.mention,
        "created_at": iso(role.created_at),
    }


def serialize_permissions(permissions: Optional[discord.Permissions]) -> Optional[Dict[str, Any]]:
    if permissions is None:
        return None
    return {"value": permissions.value, "names": permission_names(permissions)}


def serialize_overwrites(
    overwrites: Optional[Mapping[Any, discord.PermissionOverwrite]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for target, overwrite in (overwrites or {}).items():
        allow: List[str] = []
        deny: List[str] = []
        for name, value in overwrite:
            if value is True:
                allow.append(name)
            elif value is False:
                deny.append(name)
        entry: Dict[str, Any] = {
            "id": sf(getattr(target, "id", target)),
            "type": "role" if isinstance(target, discord.Role) else "member",
            "name": getattr(target, "name", None),
            "allow": sorted(allow),
            "deny": sorted(deny),
        }
        result.append(entry)
    return sorted(result, key=lambda item: (item["type"] != "role", item["name"] or ""))


# ─────────────────────────────────────────────────────────────────────────────
#  Kanäle
# ─────────────────────────────────────────────────────────────────────────────


def serialize_channel(channel: Any, *, detailed: bool = True) -> Optional[Dict[str, Any]]:
    if channel is None:
        return None

    kind = _name(getattr(channel, "type", None)) or channel.__class__.__name__.lower()
    data: Dict[str, Any] = {
        "id": sf(channel.id),
        "name": getattr(channel, "name", None),
        "type": _enum(getattr(channel, "type", None)),
        "type_name": kind,
        "position": getattr(channel, "position", None),
        "created_at": iso(getattr(channel, "created_at", None)),
        "mention": getattr(channel, "mention", None),
        "jump_url": getattr(channel, "jump_url", None),
    }

    if isinstance(channel, discord.PartialInviteGuild):  # pragma: no cover
        return data

    category = getattr(channel, "category", None)
    if category is not None and not isinstance(channel, discord.CategoryChannel):
        data["category_id"] = sf(category.id)
        data["category_name"] = category.name

    guild = getattr(channel, "guild", None)
    if guild is not None:
        data["guild_id"] = sf(guild.id)

    if isinstance(channel, discord.abc.GuildChannel):
        data["overwrites"] = serialize_overwrites(channel.overwrites)
        perms = getattr(channel, "permissions_synced", None)
        if perms is not None:
            data["permissions_synced"] = perms

    if isinstance(channel, discord.CategoryChannel):
        data["channel_count"] = len(getattr(channel, "channels", []))
        if detailed:
            data["channels"] = [
                {"id": sf(c.id), "name": c.name, "type_name": _name(c.type), "position": c.position}
                for c in sorted(getattr(channel, "channels", []), key=lambda c: (c.position, c.id))
            ]
        return data

    if isinstance(channel, discord.TextChannel):
        data.update(
            {
                "topic": getattr(channel, "topic", None),
                "nsfw": channel.nsfw,
                "slowmode_delay": channel.slowmode_delay,
                "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", None),
                "default_thread_slowmode_delay": getattr(channel, "default_thread_slowmode_delay", None),
                "default_sort_order": _name(getattr(channel, "default_sort_order", None)),
                "is_news": channel.is_news(),
                "members_count": len(channel.members) if hasattr(channel, "members") else None,
                "last_message_id": sf(getattr(channel, "last_message_id", None)),
            }
        )
    elif isinstance(channel, discord.ForumChannel):
        data.update(
            {
                "topic": getattr(channel, "topic", None),
                "nsfw": channel.nsfw,
                "slowmode_delay": channel.slowmode_delay,
                "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", None),
                "default_sort_order": _name(getattr(channel, "default_sort_order", None)),
                "default_forum_layout": _name(getattr(channel, "default_forum_layout", None)),
                "available_tags": [
                    {
                        "id": sf(t.id),
                        "name": t.name,
                        "emoji": getattr(t.emoji, "name", None),
                        "moderated": t.moderated,
                    }
                    for t in getattr(channel, "available_tags", [])
                ],
            }
        )
    elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        data.update(
            {
                "bitrate": getattr(channel, "bitrate", None),
                "user_limit": getattr(channel, "user_limit", None),
                "rtc_region": getattr(channel, "rtc_region", None),
                "video_quality_mode": _name(getattr(channel, "video_quality_mode", None)),
                "nsfw": getattr(channel, "nsfw", None),
                "status": getattr(channel, "status", None),
                "connected_members": [
                    {"id": sf(m.id), "display_name": m.display_name}
                    for m in getattr(channel, "members", [])
                ],
            }
        )

    if isinstance(channel, discord.Thread):
        parent = getattr(channel, "parent", None)
        data.update(
            {
                "parent_id": sf(getattr(channel, "parent_id", None)),
                "parent_name": getattr(parent, "name", None),
                "owner_id": sf(getattr(channel, "owner_id", None)),
                "archived": getattr(channel, "archived", None),
                "locked": getattr(channel, "locked", None),
                "invitable": getattr(channel, "invitable", None),
                "auto_archive_duration": getattr(channel, "auto_archive_duration", None),
                "archive_timestamp": iso(getattr(channel, "archive_timestamp", None)),
                "message_count": getattr(channel, "message_count", None),
                "member_count": getattr(channel, "member_count", None),
                "total_message_sent": getattr(channel, "total_message_sent", None),
                "applied_tags": [sf(t) for t in getattr(channel, "applied_tags", [])],
            }
        )

    if detailed and isinstance(channel, discord.abc.GuildChannel) and not isinstance(channel, discord.Thread):
        try:
            data["invites"] = None  # wird bei Bedarf separat geholt (kostet einen API-Call)
        except Exception:  # pragma: no cover
            pass

    return data


def serialize_channel_tree(guild: discord.Guild) -> Dict[str, Any]:
    """
    Alle Kanäle als Baum: Kategorien → Kanäle → Threads.

    Genau die Sicht, die ein Mensch im Discord-Client hat — ideal für eine KI,
    um Struktur zu verstehen und Positionen zu planen.
    """
    categories: List[Dict[str, Any]] = []
    loose: List[Dict[str, Any]] = []
    category_map: Dict[int, Dict[str, Any]] = {}

    for category in sorted(guild.categories, key=lambda c: (c.position, c.id)):
        node = serialize_channel(category) or {}
        node["channels"] = []
        categories.append(node)
        category_map[category.id] = node

    for channel in sorted(guild.channels, key=lambda c: (c.position, c.id)):
        if isinstance(channel, discord.CategoryChannel):
            continue
        node = serialize_channel(channel) or {}
        threads = getattr(channel, "threads", [])
        if threads:
            node["threads"] = [serialize_thread(t) for t in threads]
        parent_id = getattr(channel, "category_id", None)
        if parent_id and parent_id in category_map:
            category_map[parent_id]["channels"].append(node)
        else:
            node.pop("category_id", None)
            node.pop("category_name", None)
            loose.append(node)

    counts = {
        "text": sum(1 for c in guild.channels if isinstance(c, discord.TextChannel)),
        "voice": sum(1 for c in guild.channels if isinstance(c, (discord.VoiceChannel, discord.StageChannel))),
        "forum": sum(1 for c in guild.channels if isinstance(c, discord.ForumChannel)),
        "category": len(guild.categories),
        "thread": sum(len(getattr(c, "threads", [])) for c in guild.channels),
    }

    return {
        "guild_id": sf(guild.id),
        "guild_name": guild.name,
        "counts": counts,
        "total": len(guild.channels),
        "categories": categories,
        "uncategorized": loose,
    }


def serialize_thread(thread: Optional[discord.Thread]) -> Optional[Dict[str, Any]]:
    if thread is None:
        return None
    return serialize_channel(thread)


# ─────────────────────────────────────────────────────────────────────────────
#  Server
# ─────────────────────────────────────────────────────────────────────────────


def serialize_guild_brief(guild: discord.Guild) -> Dict[str, Any]:
    return {
        "id": sf(guild.id),
        "name": guild.name,
        "icon_url": _asset(guild.icon),
        "member_count": guild.member_count,
        "owner_id": sf(guild.owner_id),
        "features": list(getattr(guild, "features", []) or []),
    }


def serialize_guild(guild: discord.Guild, *, detailed: bool = True) -> Dict[str, Any]:
    me = guild.me
    data: Dict[str, Any] = {
        "id": sf(guild.id),
        "name": guild.name,
        "icon_url": _asset(guild.icon),
        "banner_url": _asset(getattr(guild, "banner", None)),
        "splash_url": _asset(getattr(guild, "splash", None)),
        "discovery_splash_url": _asset(getattr(guild, "discovery_splash", None)),
        "description": getattr(guild, "description", None),
        "owner": serialize_member(guild.owner) if guild.owner else {"id": sf(guild.owner_id)},
        "owner_id": sf(guild.owner_id),
        "member_count": guild.member_count,
        "approximate_member_count": getattr(guild, "approximate_member_count", None),
        "approximate_presence_count": getattr(guild, "approximate_presence_count", None),
        "max_members": getattr(guild, "max_members", None),
        "max_presences": getattr(guild, "max_presences", None),
        "max_stage_video_channel_users": getattr(guild, "max_stage_video_channel_users", None),
        "premium_subscription_count": getattr(guild, "premium_subscription_count", 0),
        "premium_tier": _enum(getattr(guild, "premium_tier", None)),
        "premium_progress_bar_enabled": getattr(guild, "premium_progress_bar_enabled", False),
        "created_at": iso(guild.created_at),
        "shard_id": getattr(guild, "shard_id", None),
        "large": getattr(guild, "large", None),
        "unavailable": getattr(guild, "unavailable", None),
        "chunked": getattr(guild, "chunked", None),

        # ── Moderations- & Sicherheitseinstellungen ──────────────────────────
        "verification_level": _enum(guild.verification_level),
        "verification_level_name": _name(guild.verification_level),
        "explicit_content_filter": _enum(guild.explicit_content_filter),
        "explicit_content_filter_name": _name(guild.explicit_content_filter),
        "default_notifications": _enum(guild.default_notifications),
        "default_notifications_name": _name(guild.default_notifications),
        "mfa_level": _enum(guild.mfa_level),
        "mfa_level_name": _name(guild.mfa_level),
        "nsfw_level": _enum(getattr(guild, "nsfw_level", None)),
        "raid_alerts_disabled": getattr(guild, "raid_alerts_disabled", None),

        # ── Community / Features ─────────────────────────────────────────────
        "features": list(getattr(guild, "features", []) or []),
        "community": getattr(guild, "community", None),
        "discoverable": getattr(guild, "discoverable", None),
        "partnered": getattr(guild, "partnered", None),
        "verified": getattr(guild, "verified", None),
        "invites_disabled": getattr(guild, "invites_disabled", None),
        "vanity_url": getattr(guild, "vanity_url", None),
        "vanity_url_code": getattr(getattr(guild, "vanity_url", None), "code", None)
        if not isinstance(getattr(guild, "vanity_url", None), str)
        else getattr(guild, "vanity_url", None),
        "preferred_locale": _enum(getattr(guild, "preferred_locale", None)),
        "system_channel_flags": jsonable(getattr(guild, "system_channel_flags", None)),

        # ── Wichtige Kanäle ──────────────────────────────────────────────────
        "afk_channel": _ref(guild.afk_channel),
        "afk_timeout": getattr(guild, "afk_timeout", None),
        "system_channel": _ref(guild.system_channel),
        "rules_channel": _ref(getattr(guild, "rules_channel", None)),
        "public_updates_channel": _ref(getattr(guild, "public_updates_channel", None)),
        "safety_alerts_channel": _ref(getattr(guild, "safety_alerts_channel", None)),
        "widget_enabled": getattr(guild, "widget_enabled", None),
        "widget_channel": _ref(getattr(guild, "widget_channel", None)),

        # ── Der Bot selbst ───────────────────────────────────────────────────
        "bot_member": {
            "id": sf(me.id) if me else None,
            "display_name": me.display_name if me else None,
            "nickname": getattr(me, "nick", None) if me else None,
            "joined_at": iso(me.joined_at) if me else None,
            "permissions": serialize_permissions(me.guild_permissions) if me else None,
            "is_administrator": bool(me.guild_permissions.administrator) if me else False,
            "top_role": me.top_role.name if me else None,
            "roles": [r.name for r in (me.roles if me else []) if not r.is_default()],
        },
    }

    if detailed:
        data["counts"] = {
            "channels": len(guild.channels),
            "categories": len(guild.categories),
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "stage_channels": len(getattr(guild, "stage_channels", [])),
            "forums": len(getattr(guild, "forums", [])),
            "roles": len(guild.roles),
            "emojis": len(guild.emojis),
            "stickers": len(getattr(guild, "stickers", [])),
            "members_cached": len(guild.members),
            "members_online": sum(
                1 for m in guild.members if getattr(m, "status", None) not in (None, discord.Status.offline)
            ),
            "bots": sum(1 for m in guild.members if m.bot),
            "humans": sum(1 for m in guild.members if not m.bot),
            "bans_cached": None,
            "invites_cached": None,
            "scheduled_events": len(getattr(guild, "scheduled_events", [])),
            "threads": sum(len(getattr(c, "threads", [])) for c in guild.channels),
            "webhooks": None,
        }
        data["emojis"] = [serialize_emoji(e) for e in guild.emojis[:60]]
        data["roles"] = [serialize_role(r) for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)]
        data["voice_states"] = [
            serialize_voice_state(m) for m in guild.members if getattr(m, "voice", None) is not None
        ]
    return data


def _ref(channel: Any) -> Optional[Dict[str, Any]]:
    if channel is None:
        return None
    return {
        "id": sf(channel.id),
        "name": getattr(channel, "name", None),
        "type_name": _name(getattr(channel, "type", None)),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Nachrichten & Embeds
# ─────────────────────────────────────────────────────────────────────────────


def serialize_embed(embed: discord.Embed) -> Dict[str, Any]:
    return {
        "title": embed.title,
        "description": embed.description,
        "url": embed.url,
        "timestamp": iso(embed.timestamp),
        "color": embed.color.value if embed.color else None,
        "footer": (
            {"text": embed.footer.text, "icon_url": embed.footer.icon_url}
            if embed.footer and (embed.footer.text or embed.footer.icon_url)
            else None
        ),
        "image": embed.image.url if embed.image else None,
        "thumbnail": embed.thumbnail.url if embed.thumbnail else None,
        "author": (
            {"name": embed.author.name, "url": embed.author.url, "icon_url": embed.author.icon_url}
            if embed.author and embed.author.name
            else None
        ),
        "fields": [
            {"name": f.name, "value": f.value, "inline": f.inline} for f in embed.fields
        ],
    }


def embed_from_dict(data: Mapping[str, Any]) -> discord.Embed:
    """Baut aus einem JSON-Objekt ein ``discord.Embed`` (Gegenstück zum Serialisierer)."""
    from .util import ApiError, parse_color, parse_datetime  # lokal, um Zyklen zu vermeiden

    embed = discord.Embed(
        title=data.get("title"),
        description=data.get("description"),
        url=data.get("url"),
        color=parse_color(data.get("color") or data.get("colour"), field="embed.color"),
        timestamp=parse_datetime(data.get("timestamp"), field="embed.timestamp"),
    )
    footer = data.get("footer")
    if isinstance(footer, Mapping):
        embed.set_footer(text=footer.get("text"), icon_url=footer.get("icon_url"))
    elif isinstance(footer, str):
        embed.set_footer(text=footer)

    image = data.get("image")
    if isinstance(image, Mapping):
        embed.set_image(url=image.get("url", ""))
    elif isinstance(image, str):
        embed.set_image(url=image)

    thumbnail = data.get("thumbnail")
    if isinstance(thumbnail, Mapping):
        embed.set_thumbnail(url=thumbnail.get("url", ""))
    elif isinstance(thumbnail, str):
        embed.set_thumbnail(url=thumbnail)

    author = data.get("author")
    if isinstance(author, Mapping):
        embed.set_author(
            name=author.get("name", ""),
            url=author.get("url"),
            icon_url=author.get("icon_url"),
        )
    elif isinstance(author, str):
        embed.set_author(name=author)

    for index, raw_field in enumerate(data.get("fields") or []):
        if not isinstance(raw_field, Mapping):
            raise ApiError.bad_request(f"embed.fields[{index}]: erwartet Objekt mit 'name' und 'value'.")
        embed.add_field(
            name=str(raw_field.get("name", "")),
            value=str(raw_field.get("value", "")),
            inline=bool(raw_field.get("inline", True)),
        )
    return embed


def serialize_message(message: discord.Message, *, detailed: bool = True) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": sf(message.id),
        "channel_id": sf(message.channel.id),
        "author": serialize_user(message.author) if not isinstance(message.author, discord.Member)
                  else serialize_member(message.author),
        "content": message.content,
        "created_at": iso(message.created_at),
        "edited_at": iso(message.edited_at),
        "tts": message.tts,
        "pinned": message.pinned,
        "mention_everyone": message.mention_everyone,
        "type": _enum(message.type),
        "type_name": _name(message.type),
        "jump_url": message.jump_url,
        "embeds": [serialize_embed(e) for e in message.embeds],
        "attachments": [
            {
                "id": sf(a.id),
                "filename": a.filename,
                "url": a.url,
                "proxy_url": a.proxy_url,
                "size": a.size,
                "content_type": a.content_type,
                "width": a.width,
                "height": a.height,
                "ephemeral": getattr(a, "ephemeral", None),
            }
            for a in message.attachments
        ],
        "reactions": [
            {"emoji": str(r.emoji), "count": r.count, "me": r.me} for r in message.reactions
        ],
        "mentions": [serialize_user(u) for u in message.mentions[:20]],
        "role_mentions": [sf(r.id) for r in message.role_mentions],
        "channel_mentions": [sf(c.id) for c in message.channel_mentions],
        "reference": (
            {
                "message_id": sf(message.reference.message_id),
                "channel_id": sf(message.reference.channel_id),
                "guild_id": sf(message.reference.guild_id),
            }
            if message.reference
            else None
        ),
        "stickers": [{"id": sf(s.id), "name": getattr(s, "name", None)} for s in message.stickers],
        "flags": jsonable(message.flags),
    }
    if detailed and getattr(message, "components", None):
        data["components"] = [_serialize_components(message.components)]
    return data


def _serialize_components(components: Any) -> Any:
    result: List[Dict[str, Any]] = []
    for component in components or []:
        node: Dict[str, Any] = {
            "type": _enum(getattr(component, "type", None)),
            "type_name": _name(getattr(component, "type", None)),
        }
        children = getattr(component, "children", None)
        if children:
            node["components"] = _serialize_components(children)
        for attr in ("label", "style", "emoji", "custom_id", "url", "disabled", "placeholder", "value"):
            value = getattr(component, attr, None)
            if value is not None:
                node[attr] = jsonable(value)
        result.append(node)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Sonstiges
# ─────────────────────────────────────────────────────────────────────────────


def serialize_invite(invite: Optional[discord.Invite]) -> Optional[Dict[str, Any]]:
    if invite is None:
        return None
    inviter = getattr(invite, "inviter", None)
    return {
        "code": invite.code,
        "url": invite.url,
        "channel": _ref(getattr(invite, "channel", None)),
        "inviter": serialize_user(inviter) if inviter else None,
        "uses": getattr(invite, "uses", None),
        "max_uses": getattr(invite, "max_uses", None),
        "max_age": getattr(invite, "max_age", None),
        "temporary": getattr(invite, "temporary", None),
        "created_at": iso(getattr(invite, "created_at", None)),
        "expires_at": iso(getattr(invite, "expires_at", None)),
        "approximate_member_count": getattr(invite, "approximate_member_count", None),
        "approximate_presence_count": getattr(invite, "approximate_presence_count", None),
        "target_type": _name(getattr(invite, "target_type", None)),
    }


def serialize_emoji(emoji: Optional[discord.Emoji]) -> Optional[Dict[str, Any]]:
    if emoji is None:
        return None
    return {
        "id": sf(emoji.id),
        "name": emoji.name,
        "animated": emoji.animated,
        "managed": emoji.managed,
        "requires_colons": emoji.requires_colons,
        "available": emoji.available,
        "url": emoji.url,
        "roles": [{"id": sf(r.id), "name": r.name} for r in getattr(emoji, "roles", [])],
        "created_at": iso(emoji.created_at),
        "guild_available": getattr(emoji, "guild_available", None),
    }


def serialize_sticker(sticker: Any) -> Optional[Dict[str, Any]]:
    if sticker is None:
        return None
    return {
        "id": sf(sticker.id),
        "name": getattr(sticker, "name", None),
        "description": getattr(sticker, "description", None),
        "tags": getattr(sticker, "tags", None),
        "format": _name(getattr(sticker, "format", None)),
        "type": _name(getattr(sticker, "type", None)),
        "url": getattr(sticker, "url", None),
        "available": getattr(sticker, "available", None),
        "guild_id": sf(getattr(sticker, "guild_id", None)) if getattr(sticker, "guild_id", None) else None,
    }


def serialize_webhook(webhook: Optional[discord.Webhook]) -> Optional[Dict[str, Any]]:
    if webhook is None:
        return None
    return {
        "id": sf(webhook.id),
        "name": webhook.name,
        "type": _name(webhook.type),
        "channel_id": sf(webhook.channel_id) if webhook.channel_id else None,
        "guild_id": sf(webhook.guild_id) if getattr(webhook, "guild_id", None) else None,
        "avatar_url": _asset(getattr(webhook, "avatar", None)),
        "url": webhook.url,
        "token": None,  # bewusst NICHT exponiert
        "creator": serialize_user(getattr(webhook, "user", None)),
        "created_at": iso(getattr(webhook, "created_at", None)),
    }


def serialize_ban(entry: Any) -> Dict[str, Any]:
    user = getattr(entry, "user", entry)
    return {
        "user": serialize_user(user),
        "reason": getattr(entry, "reason", None),
    }


def serialize_audit_entry(entry: discord.AuditLogEntry) -> Dict[str, Any]:
    return {
        "id": sf(entry.id),
        "action": _name(entry.action),
        "action_value": _enum(entry.action),
        "user": serialize_user(getattr(entry, "user", None)),
        "target_id": sf(entry.target_id) if getattr(entry, "target_id", None) else None,
        "target": jsonable(getattr(entry, "target", None)) if not hasattr(entry.target, "id")
                  else {"id": sf(entry.target.id), "name": getattr(entry.target, "name", None)},
        "reason": entry.reason,
        "created_at": iso(entry.created_at),
        "category": _name(getattr(entry, "category", None)),
        "changes": [
            {
                "attribute": getattr(change, "attr", None) or getattr(change, "attribute", None),
                "before": jsonable(getattr(change, "before", None)),
                "after": jsonable(getattr(change, "after", None)),
            }
            for change in getattr(entry, "changes", [])
        ],
        "extra": jsonable(getattr(entry, "extra", None)),
    }


def serialize_automod_rule(rule: discord.AutoModRule) -> Dict[str, Any]:
    trigger = getattr(rule, "trigger", None)
    return {
        "id": sf(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "event_type": _name(rule.event_type),
        "event_type_value": _enum(rule.event_type),
        "trigger_type": _name(getattr(trigger, "type", None)),
        "trigger": jsonable(trigger),
        "exempt_roles": [sf(r) for r in getattr(rule, "exempt_roles", [])],
        "exempt_channels": [sf(c) for c in getattr(rule, "exempt_channels", [])],
        "actions": [jsonable(a) for a in getattr(rule, "actions", [])],
        "creator_id": sf(getattr(rule, "creator_id", None)) if getattr(rule, "creator_id", None) else None,
    }


def serialize_event(event: Any) -> Optional[Dict[str, Any]]:
    if event is None:
        return None
    return {
        "id": sf(event.id),
        "name": event.name,
        "description": getattr(event, "description", None),
        "start_time": iso(getattr(event, "start_time", None)),
        "end_time": iso(getattr(event, "end_time", None)),
        "location": getattr(event, "location", None),
        "channel": _ref(getattr(event, "channel", None)),
        "entity_type": _name(getattr(event, "entity_type", None)),
        "status": _name(getattr(event, "status", None)),
        "privacy_level": _name(getattr(event, "privacy_level", None)),
        "user_count": getattr(event, "user_count", None),
        "interested_count": getattr(event, "interested_count", None),
        "image_url": _asset(getattr(event, "image", None)),
        "creator": serialize_user(getattr(event, "creator", None)),
    }


def serialize_voice_state(member: Any) -> Optional[Dict[str, Any]]:
    voice = getattr(member, "voice", None)
    if voice is None:
        return None
    return {
        "member_id": sf(member.id),
        "display_name": getattr(member, "display_name", None),
        "channel": _ref(voice.channel),
        "session_id": getattr(voice, "session_id", None),
        "self_mute": voice.self_mute,
        "self_deaf": voice.self_deaf,
        "self_stream": getattr(voice, "self_stream", False),
        "self_video": getattr(voice, "self_video", False),
        "server_mute": voice.mute,
        "server_deaf": voice.deaf,
        "suppress": getattr(voice, "suppress", False),
        "request_to_speak_timestamp": iso(getattr(voice, "request_to_speak_timestamp", None)),
    }


def serialize_welcome_screen(screen: Any) -> Optional[Dict[str, Any]]:
    if screen is None:
        return None
    return {
        "enabled": getattr(screen, "enabled", None),
        "description": getattr(screen, "description", None),
        "welcome_channels": [
            {
                "channel": _ref(getattr(wc, "channel", None)),
                "description": getattr(wc, "description", None),
                "emoji": getattr(getattr(wc, "emoji", None), "name", None),
                "emoji_id": sf(getattr(getattr(wc, "emoji", None), "id", None))
                if getattr(getattr(wc, "emoji", None), "id", None)
                else None,
            }
            for wc in getattr(screen, "welcome_channels", [])
        ],
    }


def serialize_onboarding(onboarding: Any) -> Optional[Dict[str, Any]]:
    if onboarding is None:
        return None
    return {
        "enabled": getattr(onboarding, "enabled", None),
        "mode": _name(getattr(onboarding, "mode", None)),
        "default_channel_ids": [sf(c) for c in getattr(onboarding, "default_channel_ids", [])],
        "prompts": [jsonable(p) for p in getattr(onboarding, "prompts", [])],
    }


def serialize_widget(widget: Any) -> Optional[Dict[str, Any]]:
    if widget is None:
        return None
    return {
        "enabled": getattr(widget, "enabled", None),
        "channel": _ref(getattr(widget, "channel", None)),
        "invite_url": getattr(widget, "invite_url", None),
        "name": getattr(widget, "name", None),
        "presence_count": getattr(widget, "presence_count", None),
    }


def serialize_template(template: Any) -> Optional[Dict[str, Any]]:
    if template is None:
        return None
    return {
        "code": getattr(template, "code", None),
        "name": getattr(template, "name", None),
        "description": getattr(template, "description", None),
        "url": getattr(template, "url", None),
        "usage_count": getattr(template, "usage_count", None),
        "created_at": iso(getattr(template, "created_at", None)),
        "updated_at": iso(getattr(template, "updated_at", None)),
        "is_dirty": getattr(template, "is_dirty", None),
        "creator": serialize_user(getattr(template, "creator", None)),
        "source_guild": serialize_guild_brief(getattr(template, "source_guild", None))
        if getattr(template, "source_guild", None)
        else None,
    }


def serialize_stage_instance(instance: Any) -> Optional[Dict[str, Any]]:
    if instance is None:
        return None
    return {
        "id": sf(getattr(instance, "id", None)) if getattr(instance, "id", None) else None,
        "channel": _ref(getattr(instance, "channel", None)),
        "topic": getattr(instance, "topic", None),
        "privacy_level": _name(getattr(instance, "privacy_level", None)),
        "discoverable_disabled": getattr(instance, "discoverable_disabled", None),
    }


def serialize_list(items: Iterable[Any], serializer: Any) -> List[Any]:
    return [serializer(item) for item in items]


def summarize(items: Sequence[Any]) -> Dict[str, Any]:
    return {"count": len(items), "items": list(items)}
