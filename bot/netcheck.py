"""
Netz-Diagnose: Kommt dieser Container überhaupt zu Discord durch?

Warum dieses Modul existiert
----------------------------
Ein Discord-Login, der mit ``429`` und einer Cloudflare-Fehlerseite
(``Error 1015``) beantwortet wird, ist praktisch nie ein Fehler im Bot-Code.
Cloudflare sperrt in dem Fall die **ausgehende IP-Adresse** des Containers.
Auf PaaS-Free-Plänen (Render, Railway, Replit, …) teilen sich hunderte Kunden
wenige Ausgangs-IPs: Wenn ein anderer Mieter Discord flutet, ist die IP für
*alle* gesperrt — und zwar so lange, bis das Zeitfenster der Sperre abläuft
(oder, bei Dauer-Spam eines Nachbarn, faktisch dauerhaft).

Ohne Diagnose sieht das im Render-Log aus wie ein kaputter Bot, und blindes
„30 Minuten schlafen und erneut versuchen" trifft genau dann wieder zu, wenn
die Sperre noch aktiv ist. Deshalb misst dieses Modul die Fakten:

* **Ausgehende IP** des Containers (Render vergibt eine Adresse aus einem
  *geteilten* CIDR-Bereich — sie ändert sich bei jedem Neustart).
* **Erreichbarkeit von discord.com**, gemessen mit einem **unauthentifizierten**
  Aufruf (``GET /api/v10/gateway``). Der braucht kein Token, erzeugt keine
  „invalid request" im Sinne von Discords 10.000er-Limit und zeigt trotzdem
  sofort, ob die IP gesperrt ist.
* **Cloudflare-Ray-ID**, ``Via``- und ``Retry-After``-Header der Antwort.

Daraus ergibt sich eine saubere Unterscheidung — genau die fehlt, wenn man nur
auf die Login-Fehlermeldung schaut:

============================  =================================================
Unauth-Probe ``/gateway``     Bedeutung / was zu tun ist
============================  =================================================
``200``, Login trotzdem 429   Sperre hängt am **Token** bzw. Account →
                              doppelte Prozesse stoppen, Token zurücksetzen.
                              Eine neue IP bringt hier nichts.
``429``/``403`` + Cloudflare  Sperre hängt an der **IP** → neue IP besorgen
(HTML, kein ``Via``)          (Neustart des Containers, anderer Host, Proxy).
DNS-/TCP-Fehler               Netzwerk- oder DNS-Problem des Containers.
============================  =================================================

Eigenständig nutzbar (z. B. in der Render-Shell)::

    python -m bot.netcheck
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .config import mask_proxy_url
from .util import iso, now_utc

log = logging.getLogger("relay.netcheck")

__all__ = (
    "DISCORD_PROBE_URL",
    "NetReport",
    "ProbeResult",
    "VERDICT_IP_BLOCKED",
    "VERDICT_NETWORK_ERROR",
    "VERDICT_OK",
    "VERDICT_UNKNOWN",
    "classify_response",
    "detect_egress_ip",
    "probe_discord",
    "proxy_auth_from",
    "run_netcheck",
)

#: Unauthentifizierter Discord-Endpoint: liefert ``{"url": "wss://gateway.discord.gg"}``
#: und wird von derselben Cloudflare-Regel bewacht wie der Login — perfekter
#: Späher, weil er kein Token braucht und keine ungültige Anfrage erzeugt.
DISCORD_PROBE_URL = "https://discord.com/api/v10/gateway"

#: Dienste zur Ermittlung der ausgehenden IP (in dieser Reihenfolge probiert).
EGRESS_IP_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("https://api.ipify.org", "plain"),
    ("https://ifconfig.me/ip", "plain"),
    ("https://checkip.amazonaws.com", "plain"),
    ("https://icanhazip.com", "plain"),
    ("https://www.cloudflare.com/cdn-cgi/trace", "trace"),
)

VERDICT_OK = "ok"
VERDICT_IP_BLOCKED = "ip_blocked"
VERDICT_NETWORK_ERROR = "network_error"
VERDICT_UNKNOWN = "unknown"

_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F:]{2,39}:[0-9a-fA-F:]{2,39}\b")





def _user_agent() -> str:
    """Derselbe User-Agent, den discord.py schickt — kein „leerer" Client."""
    try:
        import discord  # noqa: PLC0415 — bewusst lokal, kein Import-Zwang

        version = discord.__version__
    except Exception:  # noqa: BLE001 — discord.py fehlt nur in kaputten Umgebungen
        version = "2.x"
    import sys

    return (
        f"DiscordBot (https://github.com/Rapptz/discord.py {version}) "
        f"Python/{sys.version_info[0]}.{sys.version_info[1]} aiohttp/{aiohttp.__version__}"
    )


@dataclass(slots=True)
class ProbeResult:
    """Ergebnis eines einzelnen HTTP-Probenaufrufs."""

    url: str
    ok: bool = False
    status: Optional[int] = None
    blocked: bool = False
    cloudflare_page: bool = False
    cf_ray: Optional[str] = None
    via: Optional[str] = None
    retry_after: Optional[float] = None
    error: Optional[str] = None
    body_excerpt: str = ""
    duration_ms: float = 0.0
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "url": self.url,
            "ok": self.ok,
            "blocked": self.blocked,
            "checked_at": self.checked_at,
            "duration_ms": round(self.duration_ms, 1),
        }
        if self.status is not None:
            data["status"] = self.status
        if self.cf_ray:
            data["cloudflare_ray"] = self.cf_ray
        if self.via:
            data["via"] = self.via
        if self.retry_after is not None:
            data["retry_after_seconds"] = round(self.retry_after, 1)
        if self.cloudflare_page:
            data["cloudflare_block_page"] = True
        if self.error:
            data["error"] = self.error
        if self.body_excerpt:
            data["body_excerpt"] = self.body_excerpt
        return data


@dataclass(slots=True)
class NetReport:
    """Gesamtbild: ausgehende IP + Discord-Erreichbarkeit + Bewertung."""

    verdict: str = VERDICT_UNKNOWN
    egress_ip: Optional[str] = None
    egress_source: Optional[str] = None
    egress_error: Optional[str] = None
    proxy: Optional[str] = None
    discord: Optional[ProbeResult] = None
    hint: str = ""
    checked_at: str = ""
    duration_ms: float = 0.0

    # ── Bewertung ───────────────────────────────────────────────────────────
    @property
    def ip_blocked(self) -> bool:
        return self.verdict == VERDICT_IP_BLOCKED

    @property
    def discord_reachable(self) -> bool:
        return bool(self.discord and self.discord.ok)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "verdict": self.verdict,
            "discord_reachable": self.discord_reachable,
            "egress_ip": self.egress_ip,
            "checked_at": self.checked_at,
            "duration_ms": round(self.duration_ms, 1),
        }
        if self.proxy:
            data["proxy"] = self.proxy
        if self.egress_source:
            data["egress_ip_source"] = self.egress_source
        if self.egress_error:
            data["egress_ip_error"] = self.egress_error
        if self.discord is not None:
            data["discord_probe"] = self.discord.to_dict()
        if self.hint:
            data["hint"] = self.hint
        return data

    def summary(self) -> List[str]:
        """Menschenlesbare Zeilen fürs Log (ohne Token, ohne Geheimnisse)."""
        lines: List[str] = []
        ip = self.egress_ip or f"unbekannt ({self.egress_error or 'kein IP-Dienst erreichbar'})"
        lines.append(f"Ausgehende IP : {ip}" + (f"  (via {self.proxy})" if self.proxy else ""))
        probe = self.discord
        if probe is None:
            lines.append("discord.com   : nicht geprüft")
        elif probe.ok:
            lines.append(f"discord.com   : erreichbar (HTTP {probe.status} in {probe.duration_ms:.0f} ms)")
        elif probe.status is not None:
            lines.append(
                f"discord.com   : GESPERRT — HTTP {probe.status}"
                + (f", Cloudflare-Ray {probe.cf_ray}" if probe.cf_ray else "")
                + (f", Retry-After {probe.retry_after:.0f} s" if probe.retry_after else "")
            )
        else:
            lines.append(f"discord.com   : nicht erreichbar — {probe.error}")
        if self.hint:
            lines.append(f"Bewertung     : {self.hint}")
        return lines


# ─────────────────────────────────────────────────────────────────────────────
#  Auswertung einer Antwort
# ─────────────────────────────────────────────────────────────────────────────


def classify_response(status: int, headers: Any, body: str) -> Tuple[bool, bool, str]:
    """
    Erkennt eine Cloudflare-Sperre an der Antwort — dieselben Kriterien, die
    discord.py intern anlegt (429 **ohne** ``Via``-Header = Cloudflare, nicht
    Discord), zusätzlich abgesichert über den Seiteninhalt.

    Liefert ``(blocked, cloudflare_page, excerpt)``.
    """
    excerpt = (body or "").strip()
    looks_html = excerpt[:1] == "<"
    lowered = excerpt.lower()
    cloudflare_page = looks_html and (
        "cloudflare" in lowered
        or "error 1015" in lowered
        or "you are being rate limited" in lowered
        or "access denied" in lowered
    )
    via = ""
    retry_after = ""
    with contextlib.suppress(Exception):
        via = str(headers.get("Via") or "")
        retry_after = str(headers.get("Retry-After") or "")
    blocked = (
        status in (403, 429, 503)
        and (not via or cloudflare_page)
    )
    short = re.sub(r"\s+", " ", excerpt)[:180]
    return blocked, cloudflare_page, short


def _retry_after(headers: Any) -> Optional[float]:
    with contextlib.suppress(Exception):
        raw = headers.get("Retry-After")
        if raw:
            return max(0.0, float(str(raw).strip()))
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Einzelproben
# ─────────────────────────────────────────────────────────────────────────────


async def _get(session: aiohttp.ClientSession, url: str, *, timeout: float,
               proxy: Optional[str] = None, proxy_auth: Optional[aiohttp.BasicAuth] = None,
               headers: Optional[Dict[str, str]] = None) -> Tuple[Optional[int], Any, str, float, Optional[str]]:
    """Ein GET mit harter Zeitgrenze. Liefert ``(status, headers, body, ms, error)``."""
    started = time.perf_counter()
    kwargs: Dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=timeout)}
    if proxy:
        kwargs["proxy"] = proxy
    if proxy_auth is not None:
        kwargs["proxy_auth"] = proxy_auth
    if headers:
        kwargs["headers"] = headers
    try:
        async with session.get(url, **kwargs) as response:
            body = await response.text(errors="replace")
            return response.status, response.headers, body[:4000], (time.perf_counter() - started) * 1000, None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — jede Netzwerkpanne ist hier eine Info
        return None, None, "", (time.perf_counter() - started) * 1000, f"{type(exc).__name__}: {exc}"


def proxy_auth_from(config: Any) -> Optional[aiohttp.BasicAuth]:
    """
    ``aiohttp.BasicAuth`` aus der Konfiguration — oder ``None``.

    Wird von :mod:`bot.main` (für den Discord-Client) und von
    ``GET /api/diagnostics`` (für die Nachmessung) benutzt.
    """
    user = str(getattr(config, "discord_proxy_user", "") or "").strip()
    if not user:
        return None
    return aiohttp.BasicAuth(user, str(getattr(config, "discord_proxy_password", "") or ""))


async def probe_discord(session: aiohttp.ClientSession, *, timeout: float = 8.0,
                        url: str = DISCORD_PROBE_URL,
                        proxy: Optional[str] = None,
                        proxy_auth: Optional[aiohttp.BasicAuth] = None) -> ProbeResult:
    """
    Prüft, ob ``discord.com`` von hier aus erreichbar ist — **ohne Token**.

    ``GET /api/v10/gateway`` ist öffentlich und liefert im Normalfall 200.
    Kommt stattdessen 429/403 mit einer Cloudflare-Seite, ist die ausgehende IP
    gesperrt — und zwar unabhängig davon, ob der Bot-Token in Ordnung ist.
    """
    result = ProbeResult(url=url, checked_at=iso(now_utc()) or "")
    status, headers, body, ms, error = await _get(
        session, url, timeout=timeout, proxy=proxy, proxy_auth=proxy_auth,
        headers={"User-Agent": _user_agent(), "Accept": "application/json"},
    )
    result.duration_ms = ms
    result.checked_at = iso(now_utc()) or ""
    result.status = status
    if error is not None:
        result.error = error
        return result
    with contextlib.suppress(Exception):
        result.cf_ray = str(headers.get("CF-RAY") or headers.get("Cf-Ray") or "") or None
        result.via = str(headers.get("Via") or "") or None
    result.retry_after = _retry_after(headers)
    blocked, cf_page, excerpt = classify_response(status or 0, headers, body)
    result.cloudflare_page = cf_page
    result.blocked = blocked
    result.ok = bool(status and 200 <= status < 300)
    if not result.ok:
        result.body_excerpt = excerpt
    return result


def _extract_ip(text: str, kind: str) -> Optional[str]:
    if kind == "trace":
        for line in text.splitlines():
            if line.lower().startswith("ip="):
                return line.split("=", 1)[1].strip() or None
        return None
    candidate = text.strip()
    if candidate and "\n" not in candidate and len(candidate) <= 45:
        return candidate
    match = _IP_RE.search(text)
    return match.group(0) if match else None


async def detect_egress_ip(session: aiohttp.ClientSession, *, timeout: float = 6.0,
                           proxy: Optional[str] = None,
                           proxy_auth: Optional[aiohttp.BasicAuth] = None,
                           ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Ermittelt die öffentliche, ausgehende IP-Adresse dieses Containers.

    Liefert ``(ip, quelle, fehler)``. Mehrere Dienste werden der Reihe nach
    probiert — fällt einer aus, sagt das nichts über Discord aus.
    """
    errors: List[str] = []
    for url, kind in EGRESS_IP_SOURCES:
        host = url.split("/")[2]
        status, _headers, body, _ms, error = await _get(
            session, url, timeout=timeout, proxy=proxy, proxy_auth=proxy_auth,
            headers={"User-Agent": _user_agent()},
        )
        if error is not None:
            # Nur den Fehlertyp merken — die aiohttp-Meldung ist lang und würde
            # das Start-Log zubauen.
            errors.append(f"{host}: {error.split(':')[0]}")
            continue
        if not status or status >= 300:
            errors.append(f"{host}: HTTP {status}")
            continue
        ip = _extract_ip(body, kind)
        if ip:
            return ip, url, None
        errors.append(f"{host}: keine IP in der Antwort")
    if len(errors) > 2:
        errors = errors[:2] + [f"+{len(errors) - 2} weitere"]
    return None, None, "; ".join(errors)[:200] or "keine Quelle erreichbar"


# ─────────────────────────────────────────────────────────────────────────────
#  Gesamtprüfung
# ─────────────────────────────────────────────────────────────────────────────


async def run_netcheck(*, proxy: Optional[str] = None,
                       proxy_auth: Optional[aiohttp.BasicAuth] = None,
                       timeout: float = 8.0,
                       with_egress_ip: bool = True) -> NetReport:
    """
    Führt die komplette Diagnose aus und bewertet sie.

    Beide Proben laufen parallel; jede hat ihre eigene, harte Zeitgrenze. Der
    Aufruf kann also höchstens ``timeout`` Sekunden dauern und wirft nie — im
    Zweifel steht das Ergebnis im Report.
    """
    started = time.perf_counter()
    report = NetReport(proxy=mask_proxy_url(proxy) if proxy else None, checked_at=iso(now_utc()) or "")
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
    session = aiohttp.ClientSession(
        connector=connector, cookie_jar=aiohttp.DummyCookieJar()
    )
    try:
        discord_probe = probe_discord(session, timeout=timeout, proxy=proxy, proxy_auth=proxy_auth)
        if with_egress_ip:
            ip_probe = detect_egress_ip(session, timeout=min(timeout, 6.0), proxy=proxy,
                                        proxy_auth=proxy_auth)
            probe, ip_result = await asyncio.gather(discord_probe, ip_probe)
            report.egress_ip, report.egress_source, report.egress_error = ip_result
        else:
            probe = await discord_probe
        report.discord = probe
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — Diagnose darf den Bot nie crashen
        report.discord = ProbeResult(url=DISCORD_PROBE_URL, error=f"{type(exc).__name__}: {exc}",
                                     checked_at=iso(now_utc()) or "")
    finally:
        with contextlib.suppress(Exception):
            await session.close()

    report.duration_ms = (time.perf_counter() - started) * 1000
    report.checked_at = iso(now_utc()) or ""
    probe = report.discord
    if probe is not None and probe.ok:
        report.verdict = VERDICT_OK
        report.hint = "discord.com ist von diesem Container aus erreichbar."
    elif probe is not None and probe.blocked:
        report.verdict = VERDICT_IP_BLOCKED
        report.hint = (
            "Cloudflare blockiert die ausgehende IP dieses Containers "
            "(Error 1015 / 'You are being rate limited'). Das liegt NICHT am "
            "Token und nicht am Bot-Code: Auf Free-Plänen teilen sich viele "
            "Kunden eine Ausgangs-IP. Hilft: neue IP (Container-Neustart / "
            "anderer Host) oder DISCORD_PROXY."
        )
    else:
        report.verdict = VERDICT_NETWORK_ERROR
        report.hint = (
            "discord.com war nicht erreichbar (DNS/TCP/TLS). Prüfe ausgehenden "
            "Netzverkehr, Firewall und ggf. DISCORD_PROXY."
        )
    return report


# ─────────────────────────────────────────────────────────────────────────────
#  CLI:  python -m bot.netcheck
# ─────────────────────────────────────────────────────────────────────────────


async def _amain() -> int:
    proxy = (os.getenv("DISCORD_PROXY") or "").strip() or None
    auth: Optional[aiohttp.BasicAuth] = None
    user = (os.getenv("DISCORD_PROXY_USER") or "").strip()
    if user:
        auth = aiohttp.BasicAuth(user, os.getenv("DISCORD_PROXY_PASSWORD", ""))
    report = await run_netcheck(proxy=proxy, proxy_auth=auth)
    print("─" * 68)
    for line in report.summary():
        print("  " + line)
    print("─" * 68)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.verdict == VERDICT_OK else 1


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
