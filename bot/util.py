"""
Kleine, aber feine Helfer: Snowflakes, Farben, Permissions, Enums, JSON.

Alles hier ist *strikt*: falsche Eingaben werfen einen :class:`ApiError` mit
einer Meldung, die eine KI (oder ein Mensch) sofort versteht und korrigieren
kann. Das ist Absicht — die API soll selbsterklärend fehlschlagen.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Type, TypeVar, Union

import discord

from .web_errors import ApiError

__all__ = (
    "extract_bearer_token",
    "ApiError",
    "now_utc",
    "iso",
    "sf",
    "as_id",
    "as_id_list",
    "parse_color",
    "parse_bool",
    "parse_int",
    "parse_str",
    "parse_enum",
    "parse_timedelta",
    "parse_datetime",
    "permission_names",
    "normalize_permission",
    "parse_permissions",
    "build_overwrites",
    "resolve_target",
    "clamp",
    "human_duration",
    "CHANNEL_TYPES",
    "channel_type_from_name",
)

E = TypeVar("E", bound=discord.enums.Enum)

# ─────────────────────────────────────────────────────────────────────────────
#  Zeit
# ─────────────────────────────────────────────────────────────────────────────


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def human_duration(seconds: Optional[float]) -> str:
    """90 → '1m 30s'; None/negativ → 'unbegrenzt'."""
    if seconds is None or seconds < 0:
        return "unbegrenzt"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def parse_datetime(value: Any, *, field: str = "value") -> Optional[datetime]:
    """ISO-8601-String, Unix-Timestamp (int/float) oder None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lower().endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ApiError.bad_request(
                f"{field}: '{value}' ist kein gültiges ISO-8601-Datum "
                "(z. B. '2026-09-10T18:00:00Z')."
            ) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ApiError.bad_request(f"{field}: erwartet String (ISO-8601) oder Zahl (Unix-Timestamp).")


def parse_timedelta(value: Any, *, field: str = "duration") -> Optional[timedelta]:
    """
    Akzeptiert Sekunden (``300``), ISO-artige Strings (``"5m"``, ``"2h30m"``,
    ``"1d"``) oder ISO-8601-Datetimes (→ Differenz zu jetzt).
    """
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # ISO-Datetime?
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            moment = parse_datetime(text, field=field)
            assert moment is not None
            return moment - now_utc()
        total = 0.0
        found = False
        for amount, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s*(ms|s|m|h|d|w)?", text.lower()):
            amount = amount.replace(",", ".")
            unit = unit or "s"
            factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
            total += float(amount) * factor
            found = True
        if found and total > 0:
            return timedelta(seconds=total)
    raise ApiError.bad_request(
        f"{field}: erwartet Sekunden (z. B. 300) oder Dauer-String ('5m', '2h30m', '7d', '1w')."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  IDs / Snowflakes
# ─────────────────────────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"^<[@#][!&]?(\d{15,25})>$")


def sf(value: Any) -> Optional[str]:
    """Snowflake → String (JSON-sicher, kein Präzisionsverlust in JS)."""
    if value is None:
        return None
    return str(value)


def as_id(value: Any, *, field: str = "id", required: bool = True) -> Optional[int]:
    """
    Wandelt ``"123"``, ``123``, ``"<@123>"``, ``"<#123>"``, ``"<@&123>"`` in ein int.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError.bad_request(f"{field}: fehlt (erwartet eine Discord-ID).")
        return None
    if isinstance(value, bool):
        raise ApiError.bad_request(f"{field}: bool ist keine Discord-ID.")
    if isinstance(value, int):
        if value <= 0:
            raise ApiError.bad_request(f"{field}: muss eine positive Discord-ID sein.")
        return value
    if isinstance(value, discord.Object):
        return value.id
    if hasattr(value, "id") and isinstance(getattr(value, "id"), int):
        return value.id
    if isinstance(value, str):
        text = value.strip()
        match = _MENTION_RE.match(text)
        if match:
            return int(match.group(1))
        if re.fullmatch(r"\d{15,25}", text):
            return int(text)
    raise ApiError.bad_request(
        f"{field}: '{value}' ist keine gültige Discord-ID (15–25 Ziffern, z. B. '123456789012345678')."
    )


def as_id_list(
    values: Any, *, field: str = "ids", required: bool = False, limit: Optional[int] = None
) -> Optional[List[int]]:
    if values is None:
        if required:
            raise ApiError.bad_request(f"{field}: fehlt (erwartet eine Liste von IDs).")
        return None
    if isinstance(values, (str, int)):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        raise ApiError.bad_request(f"{field}: erwartet eine Liste.")
    result = [as_id(v, field=f"{field}[{i}]") for i, v in enumerate(values)]
    if limit is not None and len(result) > limit:
        raise ApiError.bad_request(f"{field}: maximal {limit} Einträge erlaubt ({len(result)} erhalten).")
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Skalare
# ─────────────────────────────────────────────────────────────────────────────


def parse_bool(value: Any, *, field: str = "value", default: Optional[bool] = None) -> Optional[bool]:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "ja", "wahr"}:
            return True
        if text in {"0", "false", "no", "n", "off", "nein", "falsch"}:
            return False
    raise ApiError.bad_request(f"{field}: erwartet true/false (Wert: {value!r}).")


def parse_int(
    value: Any, *, field: str = "value", default: Optional[int] = None,
    minimum: Optional[int] = None, maximum: Optional[int] = None,
) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError.bad_request(f"{field}: erwartet eine ganze Zahl (Wert: {value!r}).") from exc
    if minimum is not None and result < minimum:
        raise ApiError.bad_request(f"{field}: muss >= {minimum} sein (Wert: {result}).")
    if maximum is not None and result > maximum:
        raise ApiError.bad_request(f"{field}: muss <= {maximum} sein (Wert: {result}).")
    return result


def parse_str(
    value: Any, *, field: str = "value", default: Optional[str] = None,
    max_length: Optional[int] = None, min_length: int = 0,
    allow_empty: bool = True,
) -> Optional[str]:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ApiError.bad_request(f"{field}: erwartet einen String (Wert: {type(value).__name__}).")
    text = value.strip()
    if not text and not allow_empty:
        raise ApiError.bad_request(f"{field}: darf nicht leer sein.")
    if min_length and len(text) < min_length:
        raise ApiError.bad_request(f"{field}: muss mindestens {min_length} Zeichen haben.")
    if max_length is not None and len(text) > max_length:
        raise ApiError.bad_request(
            f"{field}: zu lang ({len(text)} Zeichen, maximal {max_length})."
        )
    return text if text or allow_empty else default


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_CSS_COLORS: Dict[str, int] = {
    "rot": 0xE74C3C, "red": 0xE74C3C, "grün": 0x2ECC71, "green": 0x2ECC71,
    "blau": 0x3498DB, "blue": 0x3498DB, "gelb": 0xF1C40F, "yellow": 0xF1C40F,
    "orange": 0xE67E22, "lila": 0x9B59B6, "purple": 0x9B59B6, "violett": 0x9B59B6,
    "pink": 0xE91E63, "türkis": 0x1ABC9C, "turkis": 0x1ABC9C, "teal": 0x1ABC9C,
    "cyan": 0x1ABC9C, "weiß": 0xFFFFFF, "white": 0xFFFFFF, "weiss": 0xFFFFFF,
    "schwarz": 0x000000, "black": 0x000000, "grau": 0x95A5A6, "gray": 0x95A5A6,
    "grey": 0x95A5A6, "gold": 0xF1C40F, "blurple": 0x5865F2, "discord": 0x5865F2,
    "navy": 0x34495E, "mint": 0x2ECC71, "rose": 0xE91E63, "none": 0,
}


def parse_color(value: Any, *, field: str = "color", default: Optional[int] = None) -> Optional[int]:
    """``'#5865F2'``, ``0x5865F2``, ``5793266`` oder ein Farbname wie ``'blurple'``."""
    if value is None or value == "":
        return default
    if isinstance(value, discord.Colour):
        return value.value
    if isinstance(value, bool):
        raise ApiError.bad_request(f"{field}: bool ist keine Farbe.")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFF:
            raise ApiError.bad_request(f"{field}: muss zwischen 0 und 16777215 liegen (Wert: {value}).")
        return value
    if isinstance(value, str):
        text = value.strip()
        match = _HEX_RE.match(text)
        if match:
            return int(match.group(1), 16)
        lowered = text.lower().replace("-", "").replace("_", "")
        if lowered in _CSS_COLORS:
            return _CSS_COLORS[lowered]
        if re.fullmatch(r"\d+", text):
            return parse_color(int(text), field=field)
    raise ApiError.bad_request(
        f"{field}: erwartet '#RRGGBB', eine Zahl (0–16777215) oder einen Farbnamen "
        f"(z. B. 'blurple', 'rot', 'teal'). Erhalten: {value!r}"
    )


def parse_enum(enum_cls: Type[E], value: Any, *, field: str = "value",
               default: Optional[E] = None, extra: Optional[Mapping[str, Any]] = None) -> Optional[E]:
    """
    Enum-Mitglied aus Name, Wert oder Alias. ``extra`` erlaubt zusätzliche
    Klartext-Aliase (z. B. ``{'streng': VerificationLevel.highest}``).
    """
    if value is None or value == "":
        return default
    if isinstance(value, enum_cls):
        return value
    aliases: Dict[str, Any] = {}
    for member in enum_cls:
        aliases[str(member.name).lower().replace("_", "-")] = member
        aliases[str(member.name).lower()] = member
        aliases[str(member.value)] = member
    if extra:
        for key, member in extra.items():
            aliases[key.lower().replace("_", "-")] = member
            aliases[key.lower()] = member

    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        lookup = aliases.get(str(value))
        if lookup is None:
            raise ApiError.bad_request(
                f"{field}: {value} ist kein gültiger Wert für {enum_cls.__name__}. "
                f"Erlaubt: {sorted({str(m.value) for m in enum_cls})}"
            )
        return lookup
    if isinstance(value, str):
        key = value.strip().lower().replace("_", "-")
        lookup = aliases.get(key)
        if lookup is None:
            close = difflib.get_close_matches(key, list(aliases), n=5, cutoff=0.5)
            hint = f" Meintest du: {', '.join(sorted(set(close)))}?" if close else ""
            raise ApiError.bad_request(
                f"{field}: '{value}' ist ungültig für {enum_cls.__name__}. "
                f"Erlaubt: {', '.join(sorted({str(m.name) for m in enum_cls}))}.{hint}"
            )
        return lookup
    raise ApiError.bad_request(f"{field}: erwartet Name oder Zahl für {enum_cls.__name__}.")


# ─────────────────────────────────────────────────────────────────────────────
#  Permissions
# ─────────────────────────────────────────────────────────────────────────────

#: Häufige Schreibweisen → kanonischer discord.py-Flag-Name.
PERMISSION_ALIASES: Dict[str, str] = {
    "view_channels": "view_channel",
    "view-messages": "view_channel",
    "read_channel": "view_channel",
    "text_channel_read": "view_channel",
    "timeout_members": "moderate_members",
    "timeout": "moderate_members",
    "member_timeout": "moderate_members",
    "use_slash_commands": "use_application_commands",
    "slash_commands": "use_application_commands",
    "use_commands": "use_application_commands",
    "manage_stickers": "manage_expressions",
    "manage_emoji": "manage_expressions",
    "manage_emojis": "manage_expressions",
    "voice_connect": "connect",
    "voice_speak": "speak",
    "voice_stream": "stream",
    "voice_video": "stream",
    "voice_mute_members": "mute_members",
    "mute": "mute_members",
    "voice_deafen_members": "deafen_members",
    "deafen": "deafen_members",
    "voice_move_members": "move_members",
    "move": "move_members",
    "voice_priority_speaker": "priority_speaker",
    "priority": "priority_speaker",
    "voice_use_voice_activation": "use_voice_activation",
    "use_vad": "use_voice_activation",
    "attach_file": "attach_files",
    "files": "attach_files",
    "embed_link": "embed_links",
    "embeds": "embed_links",
    "read_history": "read_message_history",
    "history": "read_message_history",
    "view_audit_logs": "view_audit_log",
    "audit_log": "view_audit_log",
    "manage_permissions": "manage_roles",
    "kick": "kick_members",
    "ban": "ban_members",
    "channels": "manage_channels",
    "roles": "manage_roles",
    "guild": "manage_guild",
    "messages": "manage_messages",
    "threads": "manage_threads",
    "webhooks": "manage_webhooks",
    "events": "manage_events",
    "nicknames": "manage_nicknames",
    "invites": "create_instant_invite",
    "create_invite": "create_instant_invite",
    "reactions": "add_reactions",
    "external_emoji": "use_external_emojis",
    "external_emojis": "use_external_emojis",
    "external_sticker": "use_external_stickers",
    "create_poll": "send_polls",
    "polls": "send_polls",
    "insights": "view_guild_insights",
    "creator_analytics": "view_creator_monetization_analytics",
    "activities": "use_embedded_activities",
    "soundboard": "use_soundboard",
    "external_sounds": "use_external_sounds",
    "voice_messages": "send_voice_messages",
    "tts": "send_tts_messages",
    "everyone": "mention_everyone",
    "mention_all": "mention_everyone",
    "bypass_slowmodes": "bypass_slowmode",
    "request-to-speak": "request_to_speak",
    "stage": "request_to_speak",
    "private_threads": "create_private_threads",
    "public_threads": "create_public_threads",
    "thread_messages": "send_messages_in_threads",
    "send_messages_in_thread": "send_messages_in_threads",
}

_VALID_FLAGS = set(discord.Permissions.VALID_FLAGS)


def normalize_permission(name: Any) -> str:
    if not isinstance(name, str):
        raise ApiError.bad_request(
            f"Permission muss ein String sein (erhalten: {type(name).__name__}). "
            f"Beispiel: 'manage_channels'."
        )
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key in _VALID_FLAGS:
        return key
    mapped = PERMISSION_ALIASES.get(key)
    if mapped and mapped in _VALID_FLAGS:
        return mapped
    close = difflib.get_close_matches(key, sorted(_VALID_FLAGS), n=6, cutoff=0.55)
    hint = f" Meintest du: {', '.join(close)}?" if close else ""
    raise ApiError.bad_request(f"Unbekannte Permission '{name}'.{hint}")


def permission_names(permissions: discord.Permissions) -> List[str]:
    """Alle aktivierten Flags als lesbare Liste (kanonische Namen)."""
    result: List[str] = []
    for flag, enabled in permissions:
        if not enabled:
            continue
        canonical = PERMISSION_ALIASES.get(flag, flag)
        if canonical not in result:
            result.append(canonical)
    # Discord liefert z. B. 'read_messages' UND 'view_channel' → deduplizieren
    seen: List[str] = []
    for name in result:
        if name not in seen:
            seen.append(name)
    return sorted(seen)


def parse_permissions(
    value: Any, *, field: str = "permissions",
    default: Optional[discord.Permissions] = None,
    base: Optional[discord.Permissions] = None,
) -> Optional[discord.Permissions]:
    """
    Sehr toleranter Permission-Parser.

    Erlaubt:
      * ``int``            → 8 (Bitmaske)
      * ``"all"``          → alles
      * ``"none"``         → nichts
      * ``["manage_channels", "view_channel"]``  → allow-Liste
      * ``{"allow": [...], "deny": [...]}``
      * ``{"administrator": true, "kick_members": false}``  → Flag-Dict
    """
    if value is None:
        return default

    result = discord.Permissions(base.value if base is not None else 0)

    if isinstance(value, discord.Permissions):
        return value

    if isinstance(value, bool):
        return discord.Permissions.all() if value else discord.Permissions.none()

    if isinstance(value, int):
        if not 0 <= value <= discord.Permissions.all().value:
            raise ApiError.bad_request(f"{field}: Bitmaske außerhalb des gültigen Bereichs.")
        return discord.Permissions(value)

    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"all", "alle", "*", "everything", "admin", "administrator"}:
            return discord.Permissions.all() if text != "administrator" else discord.Permissions(
                administrator=True
            )
        if text in {"none", "keine", "0", "empty"}:
            return discord.Permissions.none()
        value = [part for part in re.split(r"[,;|\s]+", text) if part]

    if isinstance(value, (list, tuple, set)):
        for name in value:
            if isinstance(name, str) and name.startswith("!"):
                setattr_flag(result, normalize_permission(name[1:]), False)
            else:
                setattr_flag(result, normalize_permission(name), True)
        return result

    if isinstance(value, Mapping):
        allow = value.get("allow")
        deny = value.get("deny")
        if allow is None and deny is None:
            for key, enabled in value.items():
                setattr_flag(result, normalize_permission(key), bool(enabled))
            return result
        for name in allow or []:
            setattr_flag(result, normalize_permission(name), True)
        for name in deny or []:
            setattr_flag(result, normalize_permission(name), False)
        return result

    raise ApiError.bad_request(
        f"{field}: erwartet Zahl, 'all'/'none', Liste von Permission-Namen "
        f"oder Objekt mit allow/deny. Erhalten: {type(value).__name__}."
    )


def setattr_flag(perm: discord.Permissions, name: str, enabled: bool) -> None:
    try:
        setattr(perm, name, enabled)
    except Exception as exc:  # pragma: no cover - defensive
        raise ApiError.bad_request(f"Permission '{name}' konnte nicht gesetzt werden: {exc}") from exc


# ── Overwrites ───────────────────────────────────────────────────────────────

_SPECIAL_TARGETS = {"@everyone", "everyone", "@all", "all"}


def resolve_role(guild: discord.Guild, value: Any) -> discord.Role:
    """Rolle per ID, ``role:<id>``, ``@&<id>`` oder Name (case-insensitive)."""
    if value is None:
        raise ApiError.bad_request("Rollen-Angabe fehlt.")
    if isinstance(value, discord.Role):
        return value
    if isinstance(value, int):
        role = guild.get_role(value)
        if role is None:
            raise ApiError.not_found(f"Rolle mit ID {value} existiert nicht auf diesem Server.")
        return role
    text = str(value).strip()
    if not text:
        raise ApiError.bad_request("Rollen-Angabe ist leer.")

    lowered = text.lower()
    if lowered in _SPECIAL_TARGETS or lowered == "@everyone":
        return guild.default_role

    match = _MENTION_RE.match(text)
    if match:
        role = guild.get_role(int(match.group(1)))
        if role is None:
            raise ApiError.not_found(f"Rolle {match.group(1)} nicht gefunden.")
        return role
    if text.startswith("role:"):
        return resolve_role(guild, text[5:])
    if re.fullmatch(r"\d{15,25}", text):
        return resolve_role(guild, int(text))

    for role in guild.roles:
        if role.name.lower() == lowered:
            return role
    for role in guild.roles:
        if lowered in role.name.lower():
            return role
    raise ApiError.not_found(
        f"Rolle '{text}' auf Server '{guild.name}' nicht gefunden. "
        f"Vorhanden: {', '.join(r.name for r in guild.roles[:40]) or '(keine)'}"
    )


def resolve_member(guild: discord.Guild, value: Any) -> discord.Member:
    """Mitglied per ID, ``member:<id>``, ``<@id>`` oder Name."""
    if value is None:
        raise ApiError.bad_request("Mitglieder-Angabe fehlt.")
    if isinstance(value, discord.Member):
        return value
    if isinstance(value, int):
        member = guild.get_member(value)
        if member is None:
            raise ApiError.not_found(
                f"Mitglied {value} ist nicht im Cache. Nutze erst GET /api/v1/members/{value} "
                "(lädt es nach) oder GET /api/v1/members?query=..."
            )
        return member
    text = str(value).strip()
    match = _MENTION_RE.match(text)
    if match:
        return resolve_member(guild, int(match.group(1)))
    if text.startswith(("member:", "user:")):
        return resolve_member(guild, text.split(":", 1)[1])
    if re.fullmatch(r"\d{15,25}", text):
        return resolve_member(guild, int(text))
    lowered = text.lower()
    for member in guild.members:
        if member.name.lower() == lowered or (member.nick or "").lower() == lowered:
            return member
        if member.display_name.lower() == lowered:
            return member
    raise ApiError.not_found(f"Mitglied '{text}' nicht gefunden. Nutze GET /api/v1/members?query={text}")


def resolve_target(guild: discord.Guild, spec: Any) -> Union[discord.Role, discord.Member, discord.Object]:
    """
    Übersetzt eine Overwrite-Zielangabe in ein discord-Objekt.

    Formate::

        "@everyone"                  → Default-Rolle
        {"type": "role",  "id": …}
        {"type": "member","id": …}
        "role:123"  /  "role:Mods"
        "member:123"
        "123456789012345678"         → automatisch Rolle ODER Mitglied
    """
    if isinstance(spec, Mapping):
        kind = str(spec.get("type") or spec.get("kind") or "").strip().lower()
        ident = spec.get("id") or spec.get("target") or spec.get("value")
        if spec.get("role") is not None:
            kind, ident = kind or "role", spec["role"]
        elif spec.get("member") is not None:
            kind, ident = kind or "member", spec["member"]
        elif spec.get("user") is not None:
            kind, ident = kind or "member", spec["user"]
        if kind in {"role", "roles"}:
            return resolve_role(guild, ident)
        if kind in {"member", "user", "members"}:
            return resolve_member(guild, ident)
        if isinstance(ident, str) and ident.lower() in _SPECIAL_TARGETS:
            return guild.default_role
        if ident is not None:
            # automatischer Versuch: erst Rolle, dann Mitglied
            try:
                return resolve_role(guild, ident)
            except ApiError:
                return resolve_member(guild, ident)
        raise ApiError.bad_request(f"Overwrite-Ziel unverständlich: {dict(spec)!r}")

    text = str(spec).strip()
    if text.lower() in _SPECIAL_TARGETS:
        return guild.default_role
    if text.startswith(("role:", "member:", "user:")):
        return resolve_target(guild, {"type": text.split(":", 1)[0], "id": text.split(":", 1)[1]})
    if _MENTION_RE.match(text):
        mention_type = text[1]
        ident = _MENTION_RE.match(text).group(1)
        return resolve_target(guild, {"type": "member" if mention_type == "@" else "role", "id": ident})
    try:
        return resolve_role(guild, text)
    except ApiError:
        return resolve_member(guild, text)


# Schlüssel, die das ZIEL einer Overwrite beschreiben und keine Berechtigung
# sind. Ohne diese Liste würde {"role_key": "mod", "allow": [...]} — das im
# Setup-Wizard dokumentierte Format — als unbekannte Permission abgelehnt.
OVERWRITE_META_KEYS = frozenset({
    "allow", "deny", "type", "id", "role", "member", "user", "target", "value",
    "kind", "role_key", "key", "reason", "channel", "channel_id", "name",
})


def _overwrite_from_payload(payload: Any, *, field: str) -> discord.PermissionOverwrite:
    overwrite = discord.PermissionOverwrite()
    if payload is None:
        return overwrite
    if isinstance(payload, discord.PermissionOverwrite):
        return payload

    if isinstance(payload, str):
        payload = {"allow": [p for p in re.split(r"[,;\s]+", payload) if p]}

    if isinstance(payload, (list, tuple, set)):
        payload = {"allow": list(payload)}

    if not isinstance(payload, Mapping):
        raise ApiError.bad_request(
            f"{field}: erwartet Objekt mit 'allow'/'deny' oder eine Liste von Permissions."
        )

    allow = payload.get("allow")
    deny = payload.get("deny")
    if allow is None and deny is None:
        for key, enabled in payload.items():
            if enabled is None or key in OVERWRITE_META_KEYS:
                continue
            setattr(overwrite, normalize_permission(key), bool(enabled))
        return overwrite

    for name in allow or []:
        setattr(overwrite, normalize_permission(name), True)
    for name in deny or []:
        setattr(overwrite, normalize_permission(name), False)
    for key, value in payload.items():
        if key in OVERWRITE_META_KEYS:
            continue
        if value is None:
            continue
        setattr(overwrite, normalize_permission(key), bool(value))
    return overwrite


def build_overwrites(
    guild: discord.Guild,
    data: Any,
    *,
    field: str = "overwrites",
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[Union[discord.Role, discord.Member], discord.PermissionOverwrite]]:
    """
    Baut das ``overwrites``-Mapping für ``create_*``/``edit()``.

    ``extra`` erlaubt dem Setup-Wizard, eigene Schlüssel (``role_key``) über eine
    bereits aufgelöste ID-Tabelle zu übersetzen.

    Akzeptierte Formate::

        [ {"id": "@everyone", "deny": ["send_messages"]},
          {"type": "role", "id": "123", "allow": ["manage_messages"]} ]

        { "@everyone": {"deny": ["view_channel"]},
          "role:Mods":  {"allow": ["manage_messages"]} }
    """
    if data is None:
        return None
    if isinstance(data, Mapping) and not any(
        k in data for k in ("allow", "deny", "type", "id", "role", "member")
    ):
        items: List[Any] = [{"__key__": k, **({"allow": v} if isinstance(v, (str, list)) else dict(v or {}))}
                            for k, v in data.items()]
    elif isinstance(data, Mapping):
        items = [data]
    elif isinstance(data, (list, tuple)):
        items = list(data)
    else:
        raise ApiError.bad_request(f"{field}: erwartet Liste oder Objekt.")

    result: Dict[Union[discord.Role, discord.Member], discord.PermissionOverwrite] = {}
    for index, entry in enumerate(items):
        if not isinstance(entry, Mapping):
            raise ApiError.bad_request(f"{field}[{index}]: erwartet ein Objekt, erhalten {type(entry).__name__}.")

        key = entry.get("__key__")
        role_key = entry.get("role_key") or entry.get("key")
        if key is not None:
            target = resolve_target(guild, key)
        elif role_key is not None:
            resolved = (extra or {}).get(str(role_key))
            if resolved is None:
                raise ApiError.not_found(
                    f"{field}[{index}]: role_key '{role_key}' wurde im Setup nicht definiert."
                )
            target = resolve_target(guild, resolved)
        else:
            target = resolve_target(guild, entry)

        if not isinstance(target, (discord.Role, discord.Member)):
            raise ApiError.bad_request(f"{field}[{index}]: Ziel muss Rolle oder Mitglied sein.")

        overwrite = _overwrite_from_payload(entry, field=f"{field}[{index}]")
        if target in result:
            merged = result[target]
            for name, value in overwrite:
                if value is not None:
                    setattr(merged, name, value)
        else:
            result[target] = overwrite
    return result or None


# ─────────────────────────────────────────────────────────────────────────────
#  Kanäle
# ─────────────────────────────────────────────────────────────────────────────

CHANNEL_TYPES: Dict[str, discord.ChannelType] = {
    "text": discord.ChannelType.text,
    "news": discord.ChannelType.news,
    "announcement": discord.ChannelType.news,
    "announce": discord.ChannelType.news,
    "ankündigung": discord.ChannelType.news,
    "ankuendigung": discord.ChannelType.news,
    "voice": discord.ChannelType.voice,
    "sprach": discord.ChannelType.voice,
    "sprachkanal": discord.ChannelType.voice,
    "stage": discord.ChannelType.stage_voice,
    "bühne": discord.ChannelType.stage_voice,
    "buehne": discord.ChannelType.stage_voice,
    "forum": discord.ChannelType.forum,
    "media": discord.ChannelType.media,
    "medien": discord.ChannelType.media,
    "category": discord.ChannelType.category,
    "kategorie": discord.ChannelType.category,
}


def channel_type_from_name(value: Any, *, field: str = "type") -> discord.ChannelType:
    if value is None:
        return discord.ChannelType.text
    if isinstance(value, discord.ChannelType):
        return value
    if isinstance(value, int):
        try:
            return discord.ChannelType(value)
        except ValueError as exc:
            raise ApiError.bad_request(f"{field}: {value} ist kein gültiger Channel-Typ.") from exc
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in CHANNEL_TYPES:
        return CHANNEL_TYPES[text]
    close = difflib.get_close_matches(text, list(CHANNEL_TYPES), n=4, cutoff=0.5)
    hint = f" Meintest du: {', '.join(close)}?" if close else ""
    raise ApiError.bad_request(
        f"{field}: '{value}' ist unbekannt. Erlaubt: {', '.join(sorted(CHANNEL_TYPES))}.{hint}"
    )


def parse_sequence(value: Any, *, field: str = "items") -> Optional[List[Any]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ApiError.bad_request(f"{field}: erwartet eine Liste.")


def pick(data: Mapping[str, Any], *keys: str) -> Any:
    """Erster vorhandener Key — praktisch für Alias-Felder (``color``/``colour``)."""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def only(data: Mapping[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    return {k: data[k] for k in keys if k in data}


def extract_bearer_token(request: Any) -> Optional[str]:
    """
    Fischt das Sitzungs-Token aus einem Request — Header, Query oder Cookie.

    Bewusst großzügig: Arena AI arbeitet mit ``curl``, und nicht jede Umgebung
    kann eigene Header setzen. Reihenfolge:

    1. ``Authorization: Bearer <token>`` (oder ``Token <token>`` / nackter Wert)
    2. ``X-Api-Token`` / ``X-Auth-Token`` / ``X-Token``
    3. Query ``?token=`` / ``?t=`` / ``?api_token=`` / ``?access_token=``
    4. Cookie ``relay_token``

    Der Parameter ist duck-typed (``.headers``/``.query``/``.cookies``), damit
    die Funktion sowohl von aiohttp als auch von Tests genutzt werden kann.
    """
    headers = getattr(request, "headers", None) or {}
    header = headers.get("Authorization", "") if hasattr(headers, "get") else ""
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token"}:
            return parts[1].strip() or None
        if len(parts) == 1 and parts[0].strip():
            return parts[0].strip()
    for source in ("X-Api-Token", "X-Auth-Token", "X-Token"):
        value = headers.get(source) if hasattr(headers, "get") else None
        if value:
            return value.strip()
    query = getattr(request, "query", None) or {}
    for source in ("token", "t", "api_token", "access_token"):
        value = query.get(source) if hasattr(query, "get") else None
        if value:
            return value.strip()
    cookies = getattr(request, "cookies", None) or {}
    value = cookies.get("relay_token") if hasattr(cookies, "get") else None
    if value:
        return value.strip()
    return None


def utcnow_timestamp() -> float:
    return now_utc().timestamp()
