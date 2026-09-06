"""
Session- & Token-Verwaltung.

Warum ein eigener Token und **nicht** der Bot-Token?
----------------------------------------------------
Der Bot-Token ist der Generalschlüssel: Wer ihn hat, steuert den Bot auf
*allen* Servern, kann den Status ändern und (bei Leak) muss er sofort
zurückgesetzt werden. Die KI bekommt ihn deshalb niemals.

Stattdessen erzeugt ``/connect`` ein **Sitzungs-Token**, das

* nur für genau einen Server gilt (``guild_id``),
* einen Ablaufzeitpunkt hat (TTL),
* genau zwei Modi kennt: **Lesen + Schreiben** oder **Nur lesen**,
* jederzeit per Button, ``/revoke`` oder API widerrufbar ist und
* nur als SHA-256-Hash gespeichert wird (Speicher-Dump ≠ Token-Leak).

Die Tokens leben im Prozess-Speicher und werden zusätzlich nach
``DATA_DIR/sessions.json`` geschrieben, damit ein Neustart (z. B. durch einen
Render-Deploy) laufende Sitzungen nicht killt. Auf Render **Free** ist die
Platte flüchtig — dann einfach ``/connect`` erneut ausführen.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from .util import human_duration, iso, now_utc
from .web_errors import ApiError

log = logging.getLogger("relay.sessions")

__all__ = (
    "SCOPE_LEVELS",
    "SCOPE_ORDER",
    "MODES",
    "DEFAULT_MODE",
    "normalize_mode",
    "Session",
    "SessionStore",
    "hash_token",
    "new_token",
)

#: Berechtigungs-Stufen der API-Routen (intern, aufsteigend). Routen im
#: Registry deklarieren, welche Stufe sie brauchen; ein Session-Scope muss
#: mindestens so hoch sein. Die Modi oben drüber sind die einzigen zwei,
#: die ein Nutzer wählen kann.
SCOPE_LEVELS: Dict[str, int] = {"read": 0, "write": 1, "manage": 2, "danger": 3}
SCOPE_ORDER: List[str] = ["read", "write", "manage", "danger"]

#: Die zwei Modi, die es gibt — mehr nicht.
MODES: Dict[str, Dict[str, Any]] = {
    "read_write": {
        "label": "Lesen + Schreiben",
        "emoji": "✍️",
        "scope": "danger",
        "description": "Alles erlaubt: ansehen, einrichten, moderieren (Kanäle, Rollen, Nachrichten, Kick/Ban).",
    },
    "read": {
        "label": "Nur lesen",
        "emoji": "👁️",
        "scope": "read",
        "description": "Server anschauen, nichts verändern.",
    },
}

#: Standard, wenn beim ``/connect`` kein Modus gewählt wurde.
DEFAULT_MODE = "read_write"

#: Modi älterer Versionen → aktueller Modus (für gespeicherte Sessions).
_LEGACY_MODES: Dict[str, str] = {
    "write": "read_write",
    "manage": "read_write",
    "danger": "read_write",
}


def normalize_mode(mode: Optional[str]) -> str:
    """Mappt beliebige/veraltete Modus-Angaben auf einen der zwei gültigen Modi."""
    mode = (mode or "").strip().lower()
    if mode in MODES:
        return mode
    return _LEGACY_MODES.get(mode, DEFAULT_MODE)

TOKEN_PREFIX = "adse_"


def new_token() -> str:
    """Erzeugt ein neues, eindeutig erkennbares Sitzungs-Token."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(33)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _short(token: str) -> str:
    """Anzeige-Prefix, z. B. ``adse_9Kd2…`` — niemals das komplette Token."""
    body = token[len(TOKEN_PREFIX):] if token.startswith(TOKEN_PREFIX) else token
    return f"{TOKEN_PREFIX}{body[:6]}…"


@dataclass
class Session:
    """Eine freigegebene KI-Sitzung für genau einen Discord-Server."""

    id: str
    guild_id: int
    guild_name: str
    token_hash: str
    token_prefix: str
    created_by: int
    created_by_name: str
    created_at: datetime
    expires_at: Optional[datetime]
    mode: str
    scope: str
    note: str = ""
    last_used_at: Optional[datetime] = None
    last_used_from: Optional[str] = None
    request_count: int = 0
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None

    # ── Zustand ──────────────────────────────────────────────────────────────
    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and now_utc() >= self.expires_at

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired

    @property
    def ttl_seconds(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        return max(0.0, (self.expires_at - now_utc()).total_seconds())

    @property
    def scope_level(self) -> int:
        return SCOPE_LEVELS.get(self.scope, 0)

    def allows(self, required: str) -> bool:
        return self.scope_level >= SCOPE_LEVELS.get(required, 0)

    # ── (De-)Serialisierung ──────────────────────────────────────────────────
    def to_public_dict(self) -> Dict[str, Any]:
        """Für API-/Console-Ausgabe — enthält niemals das Token."""
        return {
            "id": self.id,
            "guild_id": str(self.guild_id),
            "guild_name": self.guild_name,
            "token_prefix": self.token_prefix,
            "created_by": str(self.created_by),
            "created_by_name": self.created_by_name,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
            "expires_in_seconds": self.ttl_seconds,
            "expires_in": human_duration(self.ttl_seconds),
            "mode": self.mode,
            "mode_label": MODES.get(self.mode, {}).get("label", self.mode),
            "scope": self.scope,
            "note": self.note,
            "last_used_at": iso(self.last_used_at),
            "last_used_from": self.last_used_from,
            "request_count": self.request_count,
            "active": self.is_active,
            "revoked": self.is_revoked,
            "revoked_at": iso(self.revoked_at),
        }

    def to_storage(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in ("created_at", "expires_at", "last_used_at", "revoked_at"):
            data[key] = iso(getattr(self, key))
        return data

    @classmethod
    def from_storage(cls, data: Dict[str, Any]) -> "Session":
        def _dt(value: Any) -> Optional[datetime]:
            if not value:
                return None
            if isinstance(value, datetime):
                return value
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None

        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        session = cls(
            **{
                **{k: normalize_mode(v) if k == "mode" else v
                   for k, v in data.items() if k in known and k not in
                   {"created_at", "expires_at", "last_used_at", "revoked_at"}},
                "created_at": _dt(data.get("created_at")) or now_utc(),
                "expires_at": _dt(data.get("expires_at")),
                "last_used_at": _dt(data.get("last_used_at")),
                "revoked_at": _dt(data.get("revoked_at")),
            }
        )
        # Alte Modi (write/manage/danger) sauber auf die zwei aktuellen mappen;
        # der gespeicherte Scope bleibt die echte Berechtigungsstufe.
        session.mode = normalize_mode(session.mode)
        return session


@dataclass
class ActionRecord:
    """Ein Eintrag im Audit-Log der Relay-API."""

    ts: datetime
    session_id: str
    guild_id: int
    method: str
    path: str
    status: int
    duration_ms: float
    remote: Optional[str]
    ok: bool
    error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": iso(self.ts),
            "session_id": self.session_id,
            "guild_id": str(self.guild_id),
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "remote": self.remote,
            "ok": self.ok,
            "error_code": self.error_code,
        }


class SessionStore:
    """
    Verwaltet alle Sitzungen. Thread-/Task-sicher über ein ``asyncio.Lock``;
    Persistenz erfolgt atomar (Temp-File + ``os.replace``).
    """

    def __init__(
        self,
        *,
        default_ttl_hours: float = 24.0,
        max_per_guild: int = 5,
        path: Optional[str] = "data/sessions.json",
        persist: bool = True,
        action_log_size: int = 400,
    ) -> None:
        self.default_ttl = timedelta(hours=default_ttl_hours) if default_ttl_hours > 0 else None
        self.max_per_guild = max_per_guild
        self.path = path
        self.persist = persist and bool(path)
        self._by_hash: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._actions: Deque[ActionRecord] = deque(maxlen=action_log_size)
        self._dirty = False
        if self.persist:
            self.load()

    # ── Persistenz ───────────────────────────────────────────────────────────
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Session-Datei %s nicht lesbar (%s) — starte mit leerem Store.", self.path, exc)
            return
        sessions = raw.get("sessions", []) if isinstance(raw, dict) else raw
        restored = 0
        for item in sessions:
            try:
                session = Session.from_storage(item)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Überspringe beschädigte Session: %s", exc)
                continue
            if not session.is_active:
                continue
            self._by_hash[session.token_hash] = session
            restored += 1
        log.info("%d aktive Sitzung(en) aus %s wiederhergestellt.", restored, self.path)

    def save(self) -> None:
        if not self.persist or not self.path:
            return
        payload = {
            "version": 1,
            "updated_at": iso(now_utc()),
            "sessions": [s.to_storage() for s in self._by_hash.values() if s.is_active],
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=directory, prefix=".sessions-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                self._dirty = False
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            log.warning("Session-Datei konnte nicht geschrieben werden: %s", exc)

    # ── CRUD ─────────────────────────────────────────────────────────────────
    async def create(
        self,
        *,
        guild_id: int,
        guild_name: str,
        created_by: int,
        created_by_name: str,
        mode: str = DEFAULT_MODE,
        ttl_hours: Optional[float] = None,
        note: str = "",
    ) -> Tuple[Session, str]:
        """
        Legt eine Sitzung an und gibt ``(session, plaintext_token)`` zurück.
        Das Klartext-Token wird **einmal** zurückgegeben und danach verworfen.
        """
        mode = normalize_mode(mode)
        if ttl_hours is None:
            expires = now_utc() + self.default_ttl if self.default_ttl else None
        elif ttl_hours <= 0:
            expires = None
        else:
            expires = now_utc() + timedelta(hours=float(ttl_hours))

        token = new_token()
        session = Session(
            id=secrets.token_hex(8),
            guild_id=guild_id,
            guild_name=guild_name,
            token_hash=hash_token(token),
            token_prefix=_short(token),
            created_by=created_by,
            created_by_name=created_by_name,
            created_at=now_utc(),
            expires_at=expires,
            mode=mode,
            scope=MODES[mode]["scope"],
            note=note,
        )

        async with self._lock:
            self._prune_locked()
            self._enforce_guild_limit_locked(guild_id)
            self._by_hash[session.token_hash] = session
            self._dirty = True
            self.save()

        log.info(
            "Sitzung %s für Guild %s (%s) erstellt von %s — Modus %s, gültig bis %s",
            session.id, guild_id, guild_name, created_by_name, mode,
            iso(expires) or "unbegrenzt",
        )
        return session, token

    def _enforce_guild_limit_locked(self, guild_id: int) -> None:
        active = [s for s in self._by_hash.values() if s.guild_id == guild_id and s.is_active]
        while len(active) >= self.max_per_guild:
            oldest = min(active, key=lambda s: s.created_at)
            oldest.revoked_at = now_utc()
            oldest.revoked_by = "auto:limit"
            log.info("Sitzung %s automatisch widerrufen (Limit %d pro Server).", oldest.id, self.max_per_guild)
            active.remove(oldest)

    def _prune_locked(self) -> int:
        dead = [h for h, s in self._by_hash.items() if not s.is_active]
        for key in dead:
            del self._by_hash[key]
        # Widerrufene/expired Sessions noch 7 Tage als Leiche aufheben? Nein —
        # wir löschen sofort, damit die Datei klein bleibt.
        if dead:
            self._dirty = True
        return len(dead)

    def verify(self, token: str, *, remote: Optional[str] = None) -> Session:
        """
        Validiert ein Klartext-Token und liefert die Sitzung.

        Wirft :class:`ApiError` mit präziser Ursache — das ist absichtlich
        *kein* ``None``, damit die API konsistente 401er mit Erklärung liefert.
        """
        if not token or not isinstance(token, str):
            raise ApiError.unauthorized(
                "Token fehlt.",
                hint='Sende den Header: Authorization: Bearer <TOKEN>',
                code="TOKEN_MISSING",
            )
        token = token.strip()
        session = self._by_hash.get(hash_token(token))
        if session is None:
            hint = (
                "Das Token ist unbekannt. Mögliche Ursachen: (1) Server wurde neu gestartet und "
                "nutzt flüchtigen Speicher → /connect erneut ausführen; (2) Token wurde widerrufen; "
                "(3) Tippfehler beim Kopieren."
                if not token.startswith(TOKEN_PREFIX)
                else "Dieses Token ist unbekannt oder wurde widerrufen. Führe /connect erneut aus."
            )
            raise ApiError.unauthorized("Ungültiges oder unbekanntes Token.", hint=hint, code="TOKEN_INVALID")
        if session.is_revoked:
            raise ApiError.unauthorized(
                f"Dieses Token wurde widerrufen ({session.revoked_by or 'unbekannt'}).",
                hint="Führe /connect erneut aus, um ein neues Token zu erhalten.",
                code="TOKEN_REVOKED",
            )
        if session.is_expired:
            raise ApiError.unauthorized(
                "Dieses Token ist abgelaufen.",
                hint="Führe /connect erneut aus, um ein neues Token zu erhalten.",
                code="TOKEN_EXPIRED",
                details={"expired_at": iso(session.expires_at)},
            )

        session.request_count += 1
        session.last_used_at = now_utc()
        if remote:
            session.last_used_from = remote
        return session

    def touch(self, session: Session) -> None:
        session.request_count += 1
        session.last_used_at = now_utc()

    async def revoke(
        self,
        *,
        token: Optional[str] = None,
        session_id: Optional[str] = None,
        guild_id: Optional[int] = None,
        by: str = "unbekannt",
    ) -> List[Session]:
        """Widerruft per Token, Sitzungs-ID oder *alle* Sitzungen eines Servers."""
        targets: List[Session] = []
        async with self._lock:
            if token:
                session = self._by_hash.get(hash_token(token.strip()))
                if session:
                    targets.append(session)
            if session_id:
                targets.extend(s for s in self._by_hash.values() if s.id == session_id)
            if guild_id is not None:
                targets.extend(s for s in self._by_hash.values() if s.guild_id == guild_id)

            revoked: List[Session] = []
            stamp = now_utc()
            for session in targets:
                if session.revoked_at is None:
                    session.revoked_at = stamp
                    session.revoked_by = by
                    revoked.append(session)
            if revoked:
                self._dirty = True
                self.save()
            for session in revoked:
                log.info("Sitzung %s widerrufen durch %s.", session.id, by)
            return revoked

    def active_for_guild(self, guild_id: int) -> List[Session]:
        return sorted(
            (s for s in self._by_hash.values() if s.guild_id == guild_id and s.is_active),
            key=lambda s: s.created_at,
        )

    def all_active(self) -> List[Session]:
        return sorted((s for s in self._by_hash.values() if s.is_active), key=lambda s: s.created_at)

    def stats(self) -> Dict[str, Any]:
        active = self.all_active()
        return {
            "active_sessions": len(active),
            "guilds": len({s.guild_id for s in active}),
            "total_known": len(self._by_hash),
            "requests_total": sum(s.request_count for s in active),
        }

    # ── Action-Log ───────────────────────────────────────────────────────────
    def record_action(self, record: ActionRecord) -> None:
        self._actions.append(record)

    def actions(
        self, *, session_id: Optional[str] = None, guild_id: Optional[int] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        items: Iterable[ActionRecord] = self._actions
        if session_id:
            items = [a for a in items if a.session_id == session_id]
        if guild_id is not None:
            items = [a for a in items if a.guild_id == guild_id]
        ordered = sorted(items, key=lambda a: a.ts, reverse=True)[: max(1, min(limit, 400))]
        return [a.to_dict() for a in ordered]
