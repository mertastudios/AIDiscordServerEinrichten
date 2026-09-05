"""Zentrale Konfiguration — alles läuft über Umgebungsvariablen (12-Factor)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ("Config", "load_config", "ConfigError")

_TRUE = {"1", "true", "yes", "y", "on", "ja", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "nein", "disable", "disabled"}


class ConfigError(RuntimeError):
    """Wird geworfen, wenn eine Pflicht-Umgebungsvariable fehlt/ungültig ist."""


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _int(key: str, default: int) -> int:
    raw = _str(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Umgebungsvariable {key} muss eine ganze Zahl sein (Wert: {raw!r}).") from None


def _float(key: str, default: float) -> float:
    raw = _str(key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"Umgebungsvariable {key} muss eine Zahl sein (Wert: {raw!r}).") from None


def _bool(key: str, default: bool) -> bool:
    raw = _str(key, "").lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"Umgebungsvariable {key} muss true/false sein (Wert: {raw!r}).")


def _list(key: str, default: Optional[List[str]] = None) -> List[str]:
    raw = _str(key, "")
    if not raw:
        return list(default or [])
    return [part.strip() for part in re.split(r"[,;\s]+", raw) if part.strip()]


def _clean_url(url: str) -> str:
    """ trailing Slash entfernen, http(s) sicherstellen. """
    url = url.strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url


@dataclass(slots=True)
class Config:
    # ── Discord ────────────────────────────────────────────────────────────
    discord_token: str
    bot_name: str = "AIDiscordServerEinrichten"
    command_name: str = "connect"
    activity_text: str = "/connect · KI-Steuerung für deinen Server"
    status_presence: str = "online"

    # ── Web-Server ─────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str = ""            # leer ⇒ wird zur Laufzeit aus Render/Request ermittelt
    allowed_origin: str = "*"
    console_enabled: bool = True

    # ── Sessions ───────────────────────────────────────────────────────────
    session_ttl_hours: float = 24.0
    max_sessions_per_guild: int = 5
    data_dir: str = "data"
    persist_sessions: bool = True

    # ── Sicherheit / Limits ────────────────────────────────────────────────
    api_rate_limit: int = 240     # Anfragen pro Token ...
    api_rate_window: int = 60     # ... pro diesem Zeitfenster (Sekunden)
    max_body_bytes: int = 8 * 1024 * 1024
    max_message_history: int = 100
    max_member_fetch: int = 1000

    # ── Betrieb ────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    enable_privileged_intents: bool = True
    extra_scopes: List[str] = field(default_factory=list)
    login_retry_base_seconds: float = 15.0
    """
    Basis für das exponentielle Backoff nach fehlgeschlagenen Discord-Logins.

    Der n-te Fehlversuch in Folge wartet ca. ``base * 2^(n-1)`` Sekunden
    (mit Zufalls-Jitter, gedeckelt auf ``login_retry_max_seconds``). So flutet
    der Bot Discord nach einem 429/Cloudflare-Bann nicht mit Login-Versuchen —
    jeder Versuch während des Banns würde ihn nämlich verlängern.
    """
    login_retry_max_seconds: float = 600.0
    """Obergrenze eines einzelnen Wartezyklus nach fehlgeschlagenem Login."""
    fatal_retry_seconds: float = 300.0
    """
    Wartezeit nach *fatalen* Login-Fehlern (ungültiger Token, dauerhaft vom
    Gateway abgewiesene Verbindung).

    Der Prozess beendet sich bewusst NICHT (kein Crash-Loop → kein Login-Spam
    → kein Cloudflare-Bann); Web-Server und /api/health bleiben erreichbar,
    der Login wird in diesem Abstand erneut versucht.
    """
    settle_scale: float = 1.0
    """
    Faktor für alle Cache-Wartezeiten nach Schreibaktionen.

    Nach ``create_role``/``create_text_channel`` wartet das Relay kurz, bis
    discord.py das zugehörige Gateway-Event verarbeitet hat — sonst würde ein
    sofort folgendes ``GET`` den alten Zustand liefern. ``0`` deaktiviert die
    Pausen (nur für Tests sinnvoll), ``1.0`` ist der Normalbetrieb.
    """

    # ───────────────────────────────────────────────────────────────────────
    def masked(self) -> Dict[str, Any]:
        """
        Alle Einstellungen **ohne** Geheimnisse — fürs Start-Log und den Support.

        Der Bot-Token wird auf die ersten 6 und letzten 4 Zeichen gekürzt; alles
        Weitere bleibt lesbar. So ist eine Fehlkonfiguration im Render-Log
        erkennbar, ohne dass ein Log-Screenshot das Token preisgibt.
        """
        secret_fields = {"discord_token"}
        values: Dict[str, Any] = {}
        for name in sorted(dir(self)):
            if name.startswith("_"):
                continue
            try:
                value = getattr(self, name)
            except Exception:  # noqa: BLE001
                continue
            if callable(value):
                continue
            if name in secret_fields and isinstance(value, str) and value:
                values[name] = f"{value[:6]}…{value[-4:]} ({len(value)} Zeichen)"
            else:
                values[name] = value
        return values

    @property
    def session_file(self) -> str:
        return os.path.join(self.data_dir, "sessions.json")

    def resolved_base_url(self, request_url: Optional[str] = None) -> str:
        """
        Liefert die öffentlich erreichbare Basis-URL.

        Priorität:
          1. explizit konfiguriertes ``PUBLIC_URL``
          2. ``RENDER_EXTERNAL_URL`` (setzt Render automatisch)
          3. aus dem laufenden Request abgeleitet (X-Forwarded-* / Host)
        """
        if self.base_url:
            return _clean_url(self.base_url)

        render_url = _clean_url(os.getenv("RENDER_EXTERNAL_URL", ""))
        if render_url:
            return render_url

        render_host = _clean_url(os.getenv("RENDER_SERVICE_HOST", "") or os.getenv("RENDER_HOST", ""))
        if render_host:
            return render_host

        if request_url:
            return _clean_url(request_url)

        # Letzter Ausweg: die eigene Bind-Adresse. Ein Wildcard-Host
        # (0.0.0.0 / ::) ist aber keine brauchbare URL — weder für einen
        # Browser noch für Arena AI. Deshalb auf localhost normalisieren.
        host = self.host.strip()
        if host in {"0.0.0.0", "::", "", "*"}:
            host = "localhost"
        return f"http://{host}:{self.port}"


def load_config() -> Config:
    """Liest die Umgebung aus und validiert die Pflichtwerte."""
    token = _str("DISCORD_BOT_TOKEN", "") or _str("BOT_TOKEN", "") or _str("TOKEN", "")
    if not token:
        raise ConfigError(
            "DISCORD_BOT_TOKEN fehlt!\n"
            "  → https://discord.com/developers/applications → deine App → Bot → 'Reset Token'\n"
            "  → Token kopieren und als Umgebungsvariable DISCORD_BOT_TOKEN setzen."
        )
    if len(token) < 20 or "." not in token:
        # Kein harter Fehler (Discord könnte das Format ändern), aber eine Warnung.
        os.environ.setdefault("_TOKEN_FORMAT_WARNING", "1")

    cfg = Config(
        discord_token=token,
        bot_name=_str("BOT_NAME", "AIDiscordServerEinrichten"),
        command_name=_str("COMMAND_NAME", "connect").lstrip("/").lower()[:32] or "connect",
        activity_text=_str("ACTIVITY_TEXT", "/connect · KI-Steuerung für deinen Server"),
        status_presence=_str("PRESENCE_STATUS", "online").lower(),
        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 8080),
        base_url=_clean_url(_str("PUBLIC_URL", "")),
        allowed_origin=_str("ALLOWED_ORIGIN", "*"),
        console_enabled=_bool("CONSOLE_ENABLED", True),
        session_ttl_hours=_float("SESSION_TTL_HOURS", 24.0),
        max_sessions_per_guild=_int("MAX_SESSIONS_PER_GUILD", 5),
        data_dir=_str("DATA_DIR", "data"),
        persist_sessions=_bool("PERSIST_SESSIONS", True),
        api_rate_limit=_int("API_RATE_LIMIT", 240),
        api_rate_window=_int("API_RATE_WINDOW", 60),
        max_body_bytes=_int("MAX_BODY_BYTES", 8 * 1024 * 1024),
        max_message_history=_int("MAX_MESSAGE_HISTORY", 100),
        max_member_fetch=_int("MAX_MEMBER_FETCH", 1000),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        enable_privileged_intents=_bool("PRIVILEGED_INTENTS", True),
        extra_scopes=_list("EXTRA_SCOPES"),
        settle_scale=_float("SETTLE_SCALE", 1.0),
        login_retry_base_seconds=_float("LOGIN_RETRY_BASE_SECONDS", 15.0),
        login_retry_max_seconds=_float("LOGIN_RETRY_MAX_SECONDS", 600.0),
        fatal_retry_seconds=_float("FATAL_RETRY_SECONDS", 300.0),
    )

    if cfg.session_ttl_hours < 0:
        raise ConfigError("SESSION_TTL_HOURS darf nicht negativ sein (0 = unbegrenzt).")
    if cfg.max_sessions_per_guild < 1:
        raise ConfigError("MAX_SESSIONS_PER_GUILD muss mindestens 1 sein.")
    if not 0.0 <= cfg.settle_scale <= 10.0:
        raise ConfigError("SETTLE_SCALE muss zwischen 0 und 10 liegen (0 = keine Wartezeiten).")
    if cfg.login_retry_base_seconds <= 0:
        raise ConfigError("LOGIN_RETRY_BASE_SECONDS muss größer als 0 sein.")
    if cfg.login_retry_max_seconds < cfg.login_retry_base_seconds:
        raise ConfigError("LOGIN_RETRY_MAX_SECONDS darf nicht kleiner als LOGIN_RETRY_BASE_SECONDS sein.")
    if cfg.login_retry_max_seconds > 3600:
        raise ConfigError("LOGIN_RETRY_MAX_SECONDS darf höchstens 3600 sein.")
    if cfg.fatal_retry_seconds <= 0:
        raise ConfigError("FATAL_RETRY_SECONDS muss größer als 0 sein.")
    if cfg.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        cfg.log_level = "INFO"

    return cfg
