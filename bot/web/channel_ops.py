"""
Kanal-Erzeugung und -Bearbeitung als geteilte Logik.

Wird sowohl von ``routes/channels.py`` als auch vom Setup-Wizard
(``routes/setup.py``) verwendet — damit sich beide exakt gleich verhalten.

Warum eigene Wrapper?
 * discord.py hat pro Kanal-Typ eine eigene ``create_*``-Methode mit
   unterschiedlichen Parametern. Die API vereinheitlicht das auf **ein**
   Schema (``type`` + optionale Felder), das auch Foren/Media/Bühnen kennt.
 * Kategorie-Angaben dürfen ID, ``#name``, reiner Name oder ein ``key`` aus
   dem laufenden Setup sein.
 * Jede Abweichung wirft einen ``ApiError`` mit konkreter Korrekturhilfe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import discord
from discord.utils import MISSING

from ..util import (
    ApiError,
    as_id,
    build_overwrites,
    channel_type_from_name,
    parse_bool,
    parse_enum,
    parse_int,
    parse_str,
)
from .context import guard

__all__ = (
    "resolve_category",
    "resolve_channel_by_ref",
    "create_channel",
    "edit_channel",
    "channel_edit_fields",
)

AUTO_ARCHIVE_DURATIONS = (60, 1440, 4320, 10080)


# ─────────────────────────────────────────────────────────────────────────────
#  Auflösung von Referenzen
# ─────────────────────────────────────────────────────────────────────────────


def resolve_channel_by_ref(
    guild: discord.Guild,
    value: Any,
    *,
    field: str = "channel",
    kinds: Optional[tuple] = None,
    keys: Optional[Mapping[str, Any]] = None,
) -> Any:
    """
    Kanal aus fast jeder denkbaren Angabe bauen.

    Akzeptiert: ``int``/``str``-ID, ``"<#123>"``, ``"#name"``, ``"name"``,
    ``"category:Name"``, ``discord.abc.GuildChannel`` und (im Setup) einen
    zuvor vergebenen ``key``.
    """
    if value is None:
        raise ApiError.bad_request(f"{field}: fehlt.")
    if isinstance(value, discord.abc.GuildChannel):
        channel: Any = value
    elif isinstance(value, int):
        channel = guild.get_channel(value) or guild.get_thread(value)
    elif isinstance(value, Mapping):
        return resolve_channel_by_ref(
            guild, value.get("id") or value.get("channel_id") or value.get("name") or value.get("key"),
            field=field, kinds=kinds, keys=keys,
        )
    else:
        text = str(value).strip()
        if not text:
            raise ApiError.bad_request(f"{field}: leere Angabe.")

        if keys and text in keys:
            channel = keys[text]
        elif text.startswith("<#") and text.endswith(">") and text[2:-1].isdigit():
            channel = guild.get_channel(int(text[2:-1]))
        elif text.lower().startswith("channel:"):
            channel = resolve_channel_by_ref(guild, text.split(":", 1)[1], field=field, kinds=kinds, keys=keys)
            return _check_kind(channel, kinds, field, guild)
        elif text.isdigit():
            channel = guild.get_channel(int(text)) or guild.get_thread(int(text))
        else:
            channel = _find_by_name(guild, text)

    if channel is None:
        raise ApiError.not_found(
            f"{field}: Kanal '{value}' auf Server '{guild.name}' nicht gefunden.",
            hint="GET /api/v1/channels liefert alle Kanäle mit IDs. Akzeptiert werden "
                 "ID, '#name', 'name' oder '<#id>'.",
            code="CHANNEL_NOT_FOUND",
        )
    return _check_kind(channel, kinds, field, guild)


def _check_kind(channel: Any, kinds: Optional[tuple], field: str, guild: discord.Guild) -> Any:
    if kinds and not isinstance(channel, kinds):
        raise ApiError.bad_request(
            f"{field}: '{getattr(channel, 'name', channel)}' ist ein {type(channel).__name__}; "
            f"erwartet: {', '.join(k.__name__ for k in kinds)}.",
            hint="Vorhandene Kanäle: GET /api/v1/channels",
            code="CHANNEL_TYPE_MISMATCH",
        )
    return channel


def _find_by_name(guild: discord.Guild, text: str) -> Any:
    wanted = text.lower().lstrip("#").strip()
    if not wanted:
        return None
    for channel in guild.channels:
        if channel.name.lower() == wanted:
            return channel
    for channel in guild.channels:
        if wanted in channel.name.lower():
            return channel
    for thread in guild.threads:
        if thread.name.lower() == wanted:
            return thread
    return None


def resolve_category(
    guild: discord.Guild,
    value: Any,
    *,
    field: str = "category",
    keys: Optional[Mapping[str, Any]] = None,
) -> Optional[discord.CategoryChannel]:
    """``null`` ⇒ keine Kategorie; sonst ID/Name/Key → ``CategoryChannel``."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() in {"none", "null", "-", "keine", "remove"}:
        return None
    if isinstance(value, discord.CategoryChannel):
        return value
    channel = resolve_channel_by_ref(
        guild, value, field=field, kinds=(discord.CategoryChannel,), keys=keys
    )
    return channel


# ─────────────────────────────────────────────────────────────────────────────
#  Erzeugen
# ─────────────────────────────────────────────────────────────────────────────


async def create_channel(
    guild: discord.Guild,
    spec: Mapping[str, Any],
    *,
    keys: Optional[Mapping[str, Any]] = None,
    reason: Optional[str] = None,
    index: int = 0,
) -> Any:
    """
    Legt einen Kanal beliebigen Typs an und gibt das discord-Objekt zurück.

    ``spec``-Felder: ``name`` (Pflicht), ``type``, ``category``/``parent_id``,
    ``topic``, ``nsfw``, ``slowmode_delay``, ``position``, ``overwrites``,
    ``bitrate``, ``user_limit``, ``rtc_region``, ``video_quality_mode``,
    ``status``, ``default_auto_archive_duration``,
    ``default_thread_slowmode_delay``, ``default_sort_order``,
    ``default_layout``, ``default_reaction_emoji``, ``available_tags``, ``media``.
    """
    field_prefix = f"channels[{index}]" if index or keys else "channel"

    name = parse_str(spec.get("name"), field=f"{field_prefix}.name", min_length=1, max_length=100,
                     allow_empty=False)
    if not name:
        raise ApiError.bad_request(f"{field_prefix}.name fehlt — jeder Kanal braucht einen Namen.")

    channel_type = channel_type_from_name(spec.get("type"), field=f"{field_prefix}.type")
    category = resolve_category(
        guild, spec.get("category", spec.get("parent_id", spec.get("category_id"))),
        field=f"{field_prefix}.category", keys=keys,
    )
    overwrites = build_overwrites(
        guild, spec.get("overwrites"), field=f"{field_prefix}.overwrites", extra=keys
    )
    reason_text = reason or parse_str(spec.get("reason"), field=f"{field_prefix}.reason",
                                      max_length=400) or f"Kanal '{name}' angelegt (Arena AI)"

    options: Dict[str, Any] = {}

    position = parse_int(spec.get("position"), field=f"{field_prefix}.position", minimum=0, maximum=1000)
    if position is not None:
        options["position"] = position

    nsfw = parse_bool(spec.get("nsfw"), field=f"{field_prefix}.nsfw")
    if nsfw is not None:
        options["nsfw"] = nsfw

    common: Dict[str, Any] = {"reason": reason_text}
    if overwrites is not None:
        common["overwrites"] = overwrites
    if category is not None:
        common["category"] = category
    common.update(options)

    label = f"Kanal '{name}' ({channel_type.name}) anlegen"

    if channel_type is discord.ChannelType.category:
        created = await guard(
            guild.create_category(name, position=position if position is not None else MISSING,
                                  overwrites=overwrites or MISSING, reason=reason_text),
            action=label,
        )
        return created

    if channel_type in (discord.ChannelType.text, discord.ChannelType.news):
        topic = parse_str(spec.get("topic"), field=f"{field_prefix}.topic", max_length=1024)
        slowmode = _slowmode(spec, field_prefix)
        auto_archive = _auto_archive(spec, field_prefix)
        thread_slowmode = parse_int(spec.get("default_thread_slowmode_delay"),
                                    field=f"{field_prefix}.default_thread_slowmode_delay",
                                    minimum=0, maximum=21600)
        kwargs: Dict[str, Any] = dict(common)
        if topic is not None:
            kwargs["topic"] = topic
        if slowmode is not None:
            kwargs["slowmode_delay"] = slowmode
        if auto_archive is not None:
            kwargs["default_auto_archive_duration"] = auto_archive
        if thread_slowmode is not None:
            kwargs["default_thread_slowmode_delay"] = thread_slowmode
        kwargs["news"] = channel_type is discord.ChannelType.news
        return await guard(guild.create_text_channel(name, **kwargs), action=label)

    if channel_type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
        bitrate = parse_int(spec.get("bitrate"), field=f"{field_prefix}.bitrate",
                            minimum=8000, maximum=384000)
        user_limit = parse_int(spec.get("user_limit"), field=f"{field_prefix}.user_limit",
                               minimum=0, maximum=99)
        region = parse_str(spec.get("rtc_region"), field=f"{field_prefix}.rtc_region", max_length=64)
        if region is not None and region.strip().lower() in {"", "auto", "automatic", "none", "null"}:
            region = None
        quality = parse_enum(discord.VideoQualityMode, spec.get("video_quality_mode"),
                             field=f"{field_prefix}.video_quality_mode")
        status = parse_str(spec.get("status"), field=f"{field_prefix}.status", max_length=500)

        kwargs = dict(common)
        kwargs.pop("nsfw", None)  # nsfw wird unten explizit behandelt
        if bitrate is not None:
            kwargs["bitrate"] = bitrate
        if user_limit is not None:
            kwargs["user_limit"] = user_limit
        if region is not None:
            kwargs["rtc_region"] = region
        if quality is not None:
            kwargs["video_quality_mode"] = quality
        if nsfw is not None:
            kwargs["nsfw"] = nsfw

        if channel_type is discord.ChannelType.stage_voice:
            return await guard(guild.create_stage_channel(name, **kwargs), action=label)
        channel = await guard(guild.create_voice_channel(name, **kwargs), action=label)
        if status:
            try:
                await guard(channel.edit(status=status, reason=reason_text),
                            action=f"Voice-Status für '{name}' setzen")
            except ApiError:
                # Voice-Channel-Status braucht Boost-Stufe 1+; kein Abbruchgrund.
                pass
        return channel

    if channel_type in (discord.ChannelType.forum, discord.ChannelType.media):
        topic = parse_str(spec.get("topic"), field=f"{field_prefix}.topic", max_length=4096)
        slowmode = _slowmode(spec, field_prefix)
        auto_archive = _auto_archive(spec, field_prefix)
        thread_slowmode = parse_int(spec.get("default_thread_slowmode_delay"),
                                    field=f"{field_prefix}.default_thread_slowmode_delay",
                                    minimum=0, maximum=21600)
        kwargs = dict(common)
        if topic is not None:
            kwargs["topic"] = topic
        if slowmode is not None:
            kwargs["slowmode_delay"] = slowmode
        if auto_archive is not None:
            kwargs["default_auto_archive_duration"] = auto_archive
        if thread_slowmode is not None:
            kwargs["default_thread_slowmode_delay"] = thread_slowmode

        sort_order = parse_enum(discord.ForumOrderType, spec.get("default_sort_order"),
                                field=f"{field_prefix}.default_sort_order")
        if sort_order is not None:
            kwargs["default_sort_order"] = sort_order
        layout = parse_enum(discord.ForumLayoutType, spec.get("default_layout"),
                            field=f"{field_prefix}.default_layout")
        if layout is not None and channel_type is discord.ChannelType.forum:
            kwargs["default_layout"] = layout

        tags = spec.get("available_tags")
        if tags:
            if not isinstance(tags, list):
                raise ApiError.bad_request(f"{field_prefix}.available_tags muss eine Liste sein.")
            kwargs["available_tags"] = _forum_tags(guild, tags, field_prefix)

        kwargs["media"] = channel_type is discord.ChannelType.media
        return await guard(guild.create_forum(name, **kwargs), action=label)

    raise ApiError.bad_request(
        f"{field_prefix}.type '{channel_type.name}' kann nicht angelegt werden.",
        hint="Erlaubt: text, announcement, voice, stage, forum, media, category.",
        code="CHANNEL_TYPE_UNSUPPORTED",
    )


def _slowmode(spec: Mapping[str, Any], field_prefix: str) -> Optional[int]:
    value = spec.get("slowmode_delay", spec.get("slowmode"))
    if value is None:
        return None
    return parse_int(value, field=f"{field_prefix}.slowmode_delay", minimum=0, maximum=21600)


def _auto_archive(spec: Mapping[str, Any], field_prefix: str) -> Optional[int]:
    value = spec.get("default_auto_archive_duration", spec.get("auto_archive_duration"))
    if value is None:
        return None
    parsed = parse_int(value, field=f"{field_prefix}.default_auto_archive_duration")
    if parsed not in AUTO_ARCHIVE_DURATIONS:
        raise ApiError.bad_request(
            f"{field_prefix}.default_auto_archive_duration {parsed} ist ungültig.",
            hint=f"Discord erlaubt nur: {', '.join(map(str, AUTO_ARCHIVE_DURATIONS))} Minuten.",
            code="AUTO_ARCHIVE_INVALID",
        )
    return parsed


def _forum_tags(guild: discord.Guild, tags: List[Any], field_prefix: str) -> List[discord.ForumTag]:
    result: List[discord.ForumTag] = []
    for index, tag in enumerate(tags):
        where = f"{field_prefix}.available_tags[{index}]"
        if isinstance(tag, str):
            result.append(discord.ForumTag(name=tag[:20]))
            continue
        if not isinstance(tag, Mapping):
            raise ApiError.bad_request(f"{where}: erwartet String oder Objekt.")
        name = parse_str(tag.get("name"), field=f"{where}.name", max_length=20, allow_empty=False)
        if not name:
            raise ApiError.bad_request(f"{where}.name fehlt (max. 20 Zeichen).")
        emoji = tag.get("emoji")
        emoji_value: Any = MISSING
        if emoji:
            text = str(emoji).strip()
            if text.isdigit():
                found = guild.get_emoji(int(text))
                emoji_value = found if found else text
            else:
                emoji_value = text
        result.append(
            discord.ForumTag(
                name=name,
                emoji=emoji_value,
                moderated=bool(parse_bool(tag.get("moderated"), field=f"{where}.moderated", default=False)),
            )
        )
    if len(result) > 20:
        raise ApiError.bad_request(f"{field_prefix}.available_tags: Discord erlaubt maximal 20 Tags.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Bearbeiten
# ─────────────────────────────────────────────────────────────────────────────


def channel_edit_fields(
    guild: discord.Guild,
    channel: Any,
    spec: Mapping[str, Any],
    *,
    keys: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Übersetzt ein JSON-Objekt in ``channel.edit(**kwargs)``-Argumente."""
    kwargs: Dict[str, Any] = {}
    changes: List[str] = []

    if "name" in spec:
        name = parse_str(spec["name"], field="name", min_length=1, max_length=100, allow_empty=False)
        if not name:
            raise ApiError.bad_request("name muss 1–100 Zeichen haben.")
        kwargs["name"] = name
        changes.append(f"name → {name}")

    if "topic" in spec:
        topic = parse_str(spec["topic"], field="topic", max_length=4096)
        kwargs["topic"] = topic
        changes.append("topic")

    if "nsfw" in spec:
        kwargs["nsfw"] = bool(parse_bool(spec["nsfw"], field="nsfw"))
        changes.append(f"nsfw → {kwargs['nsfw']}")

    if "slowmode_delay" in spec or "slowmode" in spec:
        value = _slowmode(spec, "")
        if value is None:
            value = 0
        kwargs["slowmode_delay"] = value
        changes.append(f"slowmode_delay → {value}s")

    if "position" in spec:
        position = parse_int(spec["position"], field="position", minimum=0, maximum=1000)
        if position is not None:
            kwargs["position"] = position
            changes.append(f"position → {position}")

    if any(key in spec for key in ("category", "category_id", "parent_id")):
        raw = spec.get("category", spec.get("category_id", spec.get("parent_id")))
        category = resolve_category(guild, raw, keys=keys)
        kwargs["category"] = category
        changes.append(f"category → {category.name if category else 'keine'}")

    if "overwrites" in spec:
        overwrites = build_overwrites(guild, spec["overwrites"], extra=keys)
        if overwrites is None:
            raise ApiError.bad_request(
                "overwrites ist leer.",
                hint='Beispiel: [{"id": "@everyone", "deny": ["send_messages"]}]',
            )
        kwargs["overwrites"] = overwrites
        changes.append(f"overwrites ({len(overwrites)} Ziel(e))")

    if "bitrate" in spec:
        kwargs["bitrate"] = parse_int(spec["bitrate"], field="bitrate", minimum=8000, maximum=384000)
        changes.append(f"bitrate → {kwargs['bitrate']}")

    if "user_limit" in spec:
        kwargs["user_limit"] = parse_int(spec["user_limit"], field="user_limit", minimum=0, maximum=99)
        changes.append(f"user_limit → {kwargs['user_limit']}")

    if "rtc_region" in spec:
        region = parse_str(spec["rtc_region"], field="rtc_region", max_length=64)
        if region is not None and region.strip().lower() in {"", "auto", "automatic", "none", "null"}:
            region = None
        kwargs["rtc_region"] = region
        changes.append(f"rtc_region → {region or 'auto'}")

    if "video_quality_mode" in spec:
        kwargs["video_quality_mode"] = parse_enum(
            discord.VideoQualityMode, spec["video_quality_mode"], field="video_quality_mode"
        )
        changes.append("video_quality_mode")

    if "status" in spec and isinstance(channel, discord.VoiceChannel):
        kwargs["status"] = parse_str(spec["status"], field="status", max_length=500)
        changes.append("status")

    if "default_auto_archive_duration" in spec or "auto_archive_duration" in spec:
        value = _auto_archive(spec, "")
        if value is not None:
            kwargs["default_auto_archive_duration"] = value
            changes.append(f"default_auto_archive_duration → {value}m")

    if "default_thread_slowmode_delay" in spec:
        kwargs["default_thread_slowmode_delay"] = parse_int(
            spec["default_thread_slowmode_delay"], field="default_thread_slowmode_delay",
            minimum=0, maximum=21600,
        )
        changes.append("default_thread_slowmode_delay")

    if "default_sort_order" in spec:
        kwargs["default_sort_order"] = parse_enum(
            discord.ForumOrderType, spec["default_sort_order"], field="default_sort_order"
        )
        changes.append("default_sort_order")

    if "default_layout" in spec:
        kwargs["default_layout"] = parse_enum(
            discord.ForumLayoutType, spec["default_layout"], field="default_layout"
        )
        changes.append("default_layout")

    if "available_tags" in spec:
        tags = spec["available_tags"]
        if not isinstance(tags, list):
            raise ApiError.bad_request("available_tags muss eine Liste sein.")
        kwargs["available_tags"] = _forum_tags(guild, tags, "")
        changes.append(f"available_tags ({len(tags)})")

    if "archived" in spec and isinstance(channel, discord.Thread):
        kwargs["archived"] = bool(parse_bool(spec["archived"], field="archived"))
        changes.append(f"archived → {kwargs['archived']}")

    if "locked" in spec and isinstance(channel, discord.Thread):
        kwargs["locked"] = bool(parse_bool(spec["locked"], field="locked"))
        changes.append(f"locked → {kwargs['locked']}")

    if "invitable" in spec and isinstance(channel, discord.Thread):
        kwargs["invitable"] = bool(parse_bool(spec["invitable"], field="invitable"))
        changes.append(f"invitable → {kwargs['invitable']}")

    if "applied_tags" in spec and isinstance(channel, discord.Thread):
        raw = spec["applied_tags"]
        if not isinstance(raw, list):
            raise ApiError.bad_request("applied_tags muss eine Liste von Tag-IDs sein.")
        parent = getattr(channel, "parent", None)
        available = {t.id: t for t in getattr(parent, "available_tags", [])} if parent else {}
        chosen: List[discord.ForumTag] = []
        for item in raw:
            tag_id = as_id(item, field="applied_tags")
            if tag_id not in available:
                raise ApiError.not_found(
                    f"Forum-Tag {tag_id} existiert nicht in '{getattr(parent, 'name', '?')}'.",
                    hint="GET /api/v1/channels/{forum_id} zeigt available_tags.",
                    code="FORUM_TAG_NOT_FOUND",
                )
            chosen.append(available[tag_id])
        kwargs["applied_tags"] = chosen
        changes.append(f"applied_tags ({len(chosen)})")

    return {"kwargs": kwargs, "changes": changes}


async def edit_channel(
    ctx: Any, channel: Any, spec: Mapping[str, Any], *, reason: Optional[str] = None,
    keys: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Führt ``channel.edit`` aus und liefert Änderungen + frisches Objekt."""
    prepared = channel_edit_fields(ctx.guild, channel, spec, keys=keys)
    kwargs: Dict[str, Any] = prepared["kwargs"]
    if not kwargs:
        raise ApiError.bad_request(
            "Keine änderbaren Felder im Body.",
            hint="Unterstützt: name, topic, nsfw, slowmode_delay, position, category, "
                 "overwrites, bitrate, user_limit, rtc_region, video_quality_mode, status, "
                 "default_auto_archive_duration, default_thread_slowmode_delay, "
                 "default_sort_order, default_layout, available_tags, archived, locked, invitable.",
            code="NO_CHANGES",
        )
    kwargs["reason"] = reason or parse_str(spec.get("reason"), field="reason", max_length=400) \
        or f"Kanal '{getattr(channel, 'name', channel.id)}' aktualisiert (Arena AI)"

    updated = await guard(channel.edit(**kwargs), action=f"Kanal '{channel.name}' bearbeiten")
    await ctx.settle(0.45)
    return {"changes": prepared["changes"], "channel": updated or channel}
