"""
Session- & Token-Verwaltung.

Warum ein eigener Token und **nicht** der Bot-Token?
----------------------------------------------------
Der Bot-Token ist der Generalschlüssel: Wer ihn hat, steuert den Bot auf
*allen* Servern, kann den Status ändern und (bei Leak) muss er sofort
zurückgesetzt werden. Die KI bekommt ihn deshalb niemals.

Stattdessen erzeugt der **Verbinden**-Button aus ``/connect`` ein
**Sitzungs-Token**, das

* nur für genau einen Server gilt (``guild_id``),
* einen Ablaufzeitpunkt hat (TTL),
* genau zwei Modi kennt: **Lesen + Schreiben** oder **Nur lesen**,
* jederzeit per „Verbindung trennen“-Button oder API widerrufbar ist,
* automatisch gestoppt wird, wenn es **24 Stunden lang nicht benutzt** wurde
  (:meth:`SessionStore.revoke_inactive` — ein verfügbares, aber ungenutztes
  Token ist ein unnötig offenes Tor), und
* nur als SHA-256-Hash gespeichert wird (Speicher-Dump ≠ Token-Leak).

Die Tokens leben im Prozess-Speicher und werden zusätzlich nach
``DATA_DIR/sessions.json`` geschrieben, damit ein Neustart (z. B. durch einen
Render-Deploy) laufende Sitzungen nicht killt. Auf Render **Free** ist die
Platte flüchtig — dann einfach ``/connect`` erneut ausführen.

Damit Arena AI bei einem ungültigen Token **präzise** Bescheid weiß (neues
Token generiert? getrennt? Inaktivitäts-Stopp? Neustart?), merkt sich der Store
gestoppte Verbindungen für begrenzte Zeit im Speicher — nur die Metadaten,
niemals der Klartext.
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

#: Wie lange (und wie viele) gestoppte Verbindungen im Speicher bleiben —
#: nur für **präzise 401er** („warum ist mein Token weg?“), nicht persistent
#: und ohne Klartext-Token: Nach einem Neustart fällt die Fehlermeldung auf
#: die generische Erklärung zurück.
_REVOKED_CACHE_TTL = timedelta(hours=48)
_REVOKED_CACHE_MAX = 256


def new_token() -> str:
    """Erzeugt ein neues, eindeutig erkennbares Sitzungs-Token."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(33)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _short(token: str) -> str:
    """Anzeige-Prefix, z. B. ``adse_9Kd2…`` — niemals das komplette Token."""
    body = token[len(TOKEN_PREFIX):] if token.startswith(TOKEN_PREFIX) else token
    return f"{TOKEN_PREFIX}{body[:6]}…"


#: Die „so bist du in einer Minute wieder verbunden“-Anleitung, die in jedem
#: Token-401er an Arena AI geht. Die KI soll sie dem Server-Admin weitergeben.
_RECONNECT_STEPS = (
    "**Schnell wieder verbinden:** Der Admin führt auf dem Discord-Server /connect aus, "
    "klickt auf **Verbinden** und schickt dir den neuen Prompt (URL + Token) — dann "
    "weiterarbeiten wie gehabt. Bis dahin kann der Server nicht bearbeitet werden; sag "
    "das dem Admin bitte, statt es weiter zu versuchen."
)


def _reconnect_hint(cause: str) -> str:
    """Baut den Hint für einen 401er: Ursache + schnelle Wiederanbindung."""
    return cause + "\n\n" + _RECONNECT_STEPS


def _stopped_reason_text(session: Session) -> str:
    """Menschliche Erklärung, **warum** eine Verbindung gestoppt wurde."""
    by = (session.revoked_by or "").lower()
    if by.startswith("auto:inactivity"):
        return ("die Verbindung wurde automatisch gestoppt, weil das Token länger als "
                "24 Stunden nicht benutzt wurde")
    if by.startswith("auto:limit"):
        return ("die Verbindung wurde automatisch widerrufen, weil das Limit gleichzeitiger "
                "Tokens pro Server erreicht war")
    if by.startswith("api:regenerate"):
        return "es wurde ein neues Token generiert (Button oder API) — damit ist das alte ungültig"
    if by.startswith("api:"):
        return "die Verbindung wurde über die API beendet"
    if by.startswith("discord:") or by.startswith("button:"):
        return "der Server-Admin hat die Verbindung getrennt"
    return "die Verbindung wurde beendet"


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
            "revoked_by": self.revoked_by,
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
        # Hash → kürzlich gestoppte Session (nur Metadaten). Dient allein dazu,
        # Arena AI bei einem 401 zu sagen, WARUM das Token weg ist — siehe
        # ``_remember_revoked`` und ``_unknown_token_error``.
        self._revoked_cache: Dict[str, Session] = {}
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
            self._remember_revoked(oldest)
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
        Die Meldungen richten sich an **Arena AI** (den Aufrufer): Sie sagen
        der KI, was der Server-Admin vermutlich getan hat und wie die
        Verbindung schnell wieder aufgebaut wird.
        """
        if not token or not isinstance(token, str):
            raise ApiError.unauthorized(
                "Token fehlt.",
                hint='Sende den Header: Authorization: Bearer <TOKEN> — der Server-Admin '
                     "bekommt es auf Discord über /connect (Button „Verbinden“).",
                code="TOKEN_MISSING",
            )
        token = token.strip()
        token_hash = hash_token(token)
        session = self._by_hash.get(token_hash)
        if session is None:
            raise self._unknown_token_error(token_hash)
        if session.is_revoked:
            reason = _stopped_reason_text(session)
            raise ApiError.unauthorized(
                f"Dieses Token ist nicht mehr gültig: {reason}.",
                hint=_reconnect_hint(f"Sag dem Server-Admin, was passiert ist: {reason}."),
                code="TOKEN_REVOKED",
                details={
                    "revoked_by": session.revoked_by,
                    "revoked_at": iso(session.revoked_at),
                },
            )
        if session.is_expired:
            raise ApiError.unauthorized(
                "Dieses Token ist abgelaufen.",
                hint=_reconnect_hint(
                    "Sag dem Server-Admin: Die Gültigkeit des Tokens ist abgelaufen "
                    "(Standard: 24 Stunden)."
                ),
                code="TOKEN_EXPIRED",
                details={"expired_at": iso(session.expires_at)},
            )

        session.request_count += 1
        session.last_used_at = now_utc()
        if remote:
            session.last_used_from = remote
        return session

    def _unknown_token_error(self, token_hash: str) -> ApiError:
        """
        Der 401er für ein Token, das der Store nicht (mehr) kennt.

        Ist die Verbindung kürzlich **bekannt** gewesen, steht im
        ``_revoked_cache`` noch, warum sie gestoppt wurde — dann gibt es eine
        präzise Meldung (z. B. Inaktivitäts-Stopp). Sonst listet die Meldung
        die wahrscheinlichen Ursachen: neues Token generiert, Verbindung
        getrennt/zurückgesetzt, 24 Stunden Inaktivität oder ein Neustart mit
        flüchtigem Speicher.
        """
        dead = self._revoked_cache.get(token_hash)
        if dead is not None and dead.revoked_at is not None:
            reason = _stopped_reason_text(dead)
            return ApiError.unauthorized(
                f"Dieses Token ist nicht mehr gültig: {reason}.",
                hint=_reconnect_hint(f"Sag dem Server-Admin, was passiert ist: {reason}."),
                code="TOKEN_REVOKED",
                details={
                    "stopped_by": dead.revoked_by,
                    "revoked_at": iso(dead.revoked_at),
                    "session_id": dead.id,
                },
            )
        return ApiError.unauthorized(
            "Ungültiges oder unbekanntes Token.",
            hint=(
                "Du (Arena AI) arbeitest mit einem Token, das die Bridge nicht (mehr) kennt. "
                "Sag dem Server-Admin, was er vermutlich getan hat — und wie er die Verbindung "
                "schnell wieder aufbaut. Wahrscheinliche Ursachen:\n"
                "• Er hat ein **neues Token generiert** — damit wurde das alte sofort ungültig.\n"
                "• Er hat die **Verbindung getrennt oder das Token zurückgesetzt** "
                "(„Verbindung trennen“ oder die API).\n"
                "• Es wurde **zu lange nichts gemacht**: Wird ein Token 24 Stunden lang nicht "
                "benutzt, stoppt die Bridge die Verbindung automatisch.\n"
                "• Oder der Bot wurde neu gestartet und verliert dabei Tokens auf flüchtigem "
                "Speicher (Render Free).\n\n"
                + _RECONNECT_STEPS
            ),
            code="TOKEN_INVALID",
        )

    def _remember_revoked(self, session: Session) -> None:
        """
        Merkt sich eine gestoppte Verbindung (Hash → Metadaten) für bessere 401er.

        Bewusst klein und vergänglich: höchstens ``_REVOKED_CACHE_MAX`` Einträge,
        nach ``_REVOKED_CACHE_TTL`` fallen sie weg. Nach einem Neustart ist der
        Cache leer — dann gilt die generische Erklärung.
        """
        self._revoked_cache[session.token_hash] = session
        cutoff = now_utc() - _REVOKED_CACHE_TTL
        expired = [
            key for key, value in self._revoked_cache.items()
            if (value.revoked_at or value.created_at) < cutoff
        ]
        for key in expired:
            del self._revoked_cache[key]
        if len(self._revoked_cache) > _REVOKED_CACHE_MAX:
            ordered = sorted(
                self._revoked_cache.items(),
                key=lambda item: item[1].revoked_at or item[1].created_at,
            )
            for key, _value in ordered[: len(self._revoked_cache) - _REVOKED_CACHE_MAX]:
                self._revoked_cache.pop(key, None)

    async def revoke_inactive(self, max_idle: Optional[timedelta]) -> List[Session]:
        """
        Stoppt Verbindungen, die zu lange ungenutzt waren — automatisch.

        „Zu lange“ = ``max_idle`` seit der letzten Nutzung; wurde ein Token nie
        benutzt, zählt die Erstellung. Der reguläre Ablauf (TTL) trennt eine
        Verbindung ohnehin — hier geht es um den Fall „Token vorhanden, aber
        24 Stunden lang macht niemand etwas damit“.

        Aufgerufen vom Housekeeping-Loop (``bot.main``) alle ~15 Sekunden;
        ``max_idle <= 0`` oder ``None`` deaktiviert den Stopp.
        """
        if max_idle is None or max_idle.total_seconds() <= 0:
            return []
        now = now_utc()
        stopped: List[Session] = []
        async with self._lock:
            for session in self._by_hash.values():
                if not session.is_active:
                    continue
                reference = session.last_used_at or session.created_at
                if now - reference < max_idle:
                    continue
                session.revoked_at = now
                session.revoked_by = "auto:inactivity"
                self._remember_revoked(session)
                stopped.append(session)
            if stopped:
                self._dirty = True
                self.save()
        for session in stopped:
            log.info(
                "Sitzung %s (Guild %s, Token %s) automatisch gestoppt: %.1f Stunden ohne Nutzung.",
                session.id, session.guild_id, session.token_prefix,
                (now - (session.last_used_at or session.created_at)).total_seconds() / 3600.0,
            )
        return stopped

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
                    self._remember_revoked(session)
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
