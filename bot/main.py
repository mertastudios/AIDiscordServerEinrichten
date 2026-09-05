"""
Einstiegspunkt: startet Discord-Client **und** Web-Server in einem Prozess.

Ablauf
------
1. Konfiguration laden & validieren (klare Fehlermeldung bei fehlendem Token).
2. Logging einrichten.
3. Session-Store öffnen (lädt persistente Tokens).
4. ``AppState`` + ``RelayClient`` bauen und miteinander verdrahten.
5. aiohttp-Server auf ``0.0.0.0:$PORT`` starten → ab jetzt antwortet
   ``/api/health`` und UptimeRobot kann den Dienst wach halten.
6. Bot mit Discord verbinden. Klappt das wegen nicht freigeschalteter
   **privilegierter Intents** nicht, wird automatisch ohne sie neu gestartet —
   der Bot bleibt also online, statt im Crash-Loop zu hängen. Schlägt der
   Login wegen eines **Rate-Limits / Cloudflare-Banns (429/1015)** fehl, wartet
   der Bot exponentiell länger (statt Discord zu fluten) und verbindet sich
   von selbst neu.
7. Auf ``SIGTERM``/``SIGINT`` warten (Render schickt das bei jedem Deploy) und
   sauber herunterfahren.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import sys
import time
from datetime import timedelta
from typing import Any, Optional

import discord

from . import __botname__, __version__
from .config import Config, ConfigError, load_config
from .discord_bot import RelayClient, build_intents
from .sessions import SessionStore
from .util import iso, now_utc
from .web.app import AppState, build_app, start_web_server, stop_web_server

log = logging.getLogger("relay.main")

__all__ = ("main", "amain")

BANNER = r"""
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║   A I D i s c o r d S e r v e r E i n r i c h t e n               ║
║                                                                   ║
║   Discord  ⇄  Arena AI · Relay-Bot mit Administrator-Rechten      ║
║   Ein Command:  /connect                                          ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
"""


def setup_logging(level: str) -> None:
    """Einheitliches, container-freundliches Logging (eine Zeile, UTC-Zeit)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s │ %(levelname)-7s │ %(name)-16s │ %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)

    # Lärm reduzieren
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("discord.state").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _build_store(config: Config) -> SessionStore:
    path = config.session_file if config.persist_sessions else None
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        except OSError as exc:
            log.warning("Datenverzeichnis %s nicht anlegbar (%s) — Sessions bleiben flüchtig.",
                        path, exc)
            path = None
    return SessionStore(
        default_ttl_hours=config.session_ttl_hours,
        max_per_guild=config.max_sessions_per_guild,
        path=path,
        persist=config.persist_sessions and bool(path),
    )


async def _housekeeping(state: AppState, stop: asyncio.Event) -> None:
    """
    Hintergrundaufgabe:

    * entfernt abgelaufene/widerrufene Sitzungen und schreibt sie weg,
    * protokolliert alle 5 Minuten einen Herzschlag (praktisch im Render-Log),
    * hält den Prozess beschäftigt, falls der Gateway kurz hängt.
    """
    last_beat = 0.0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass
        try:
            state.store._prune_locked()  # noqa: SLF001 — bewusst, gleiche Klasse
            state.maybe_save(min_interval=60.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("Housekeeping-Fehler: %s", exc)

        now = time.monotonic()
        if now - last_beat >= 300:
            last_beat = now
            client = state.client
            log.info(
                "❤ Uptime %dh%02dm · Server %d · Anfragen %d · Fehler %d · Sessions %d · Gateway %s",
                int(state.uptime_seconds() // 3600),
                int((state.uptime_seconds() % 3600) // 60),
                len(client.guilds) if client else 0,
                state.request_count, state.error_count,
                len(state.store.all_active()),
                f"{round(client.latency * 1000)} ms" if client and client.latency else "–",
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Login-Wiederholung mit Backoff (Schutz vor 429 / Cloudflare Error 1015)
# ─────────────────────────────────────────────────────────────────────────────
#
# Hintergrund: Schlägt ``client.start()`` beim Login fehl, weil Cloudflare die
# IP vorübergehend sperrt (Error 1015 → discord.py wirft sofort
# ``HTTPException: 429``, ganz ohne internen Wiederholungsversuch), dann
# verlängert JEDE weitere Anfrage während des Banns den Bann. Ein sturer
# „alle 10 Sekunden neu versuchen“-Loop hält den Bot deshalb *dauerhaft*
# offline — er flutet sich selbst in die Sperre. Genau das verhindern die
# Helfer unten: exponentiell wachsende Wartezeiten mit Zufalls-Jitter,
# Respekt vor dem ``Retry-After``-Header des Servers und eine echte
# Abkühlphase (mind. 60 s) nach jedem 429er.
#
# Zweite Lektion: Fatale Fehler (falscher Token, dauerhaft abgewiesene
# Verbindung) beenden den Prozess bewusst NICHT mehr. Ein Exit würde Render
# sofort neu starten lassen (Crash-Loop) — also Login-Spam, der ebenfalls in
# einem Cloudflare-Bann endet. Stattdessen bleiben Web-Server und /api/health
# erreichbar und der Login wird in großem Abstand erneut versucht.

#: Mindestwartezeit nach einem Cloudflare-Bann (Error 1015), egal beim wievielten Versuch.
CLOUDFLARE_MIN_WAIT = 60.0
#: Mindestwartezeit nach einem normalen Discord-Rate-Limit (429 mit Via-Header).
DISCORD_RATELIMIT_MIN_WAIT = 30.0
#: Harte Obergrenze für einen einzelnen Wartezyklus (schützt vor absurd großen
#: Retry-After-Werten und unbeabsichtigtem Dauer-Schlaf).
MAX_SINGLE_WAIT = 1800.0
#: Verjährung: Liegt der letzte Fehlversuch länger zurück, gilt der nächste
#: Ausfall wieder als Einzelfall und wird schnell neu versucht (statt mit der
#: über Stunden aufgebauten Maximalwartezeit).
BACKOFF_RESET_AFTER = 300.0


def _extract_retry_after(exc: BaseException) -> Optional[float]:
    """
    Liest einen vom Server gewünschten Wartewert aus einer Discord-Exception.

    Quellen: ``RateLimited.retry_after`` bzw. der ``Retry-After``-Header der
    HTTP-Antwort (Cloudflare/Discord). Liefert ``None``, wenn nichts dabei ist.
    """
    direct = getattr(exc, "retry_after", None)
    if isinstance(direct, (int, float)) and direct >= 0:
        return float(direct)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            raw = headers.get("Retry-After")
        except Exception:  # noqa: BLE001 — fremdes Header-Objekt, defensiv
            raw = None
        if raw:
            try:
                return max(0.0, float(str(raw).strip()))
            except (TypeError, ValueError):
                pass
    return None


def _is_cloudflare_ban(exc: discord.HTTPException) -> bool:
    """
    True, wenn der 429er von Cloudflare (z. B. Error 1015) statt von Discord kommt.

    discord.py wirft genau dann sofort (ohne internen Retry), wenn der
    429-Antwort der ``Via``-Header fehlt oder der Body kein JSON ist — beides
    ist bei einer Cloudflare-Fehlerseite der Fall. (Der ``error code: 0`` allein
    taugt NICHT als Kriterium: Auch echte Discord-429-JSONs enthalten oft kein
    ``code``-Feld. Entscheidend ist der fehlende ``Via``-Header.)
    """
    if getattr(exc, "status", 0) != 429:
        return False
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return True  # nichts inspizierbar → vorsichtig wie einen Bann behandeln
    try:
        return not bool(headers.get("Via"))
    except Exception:  # noqa: BLE001 — defensiv: im Zweifel wie ein Bann behandeln
        return True


def _backoff_delay(base: float, cap: float, failures: int) -> float:
    """
    Exponentielles Backoff mit Equal-Jitter: ``base * 2^(n-1)``, gedeckelt auf
    ``cap``, plus Zufall (Hälfte fest, Hälfte zufällig). Der Jitter verhindert,
    dass mehrere gleichzeitig gestartete Instanzen im Gleichtakt loshämmern.
    """
    exp = min(cap, base * (2.0 ** max(0, failures - 1)))
    return exp / 2.0 + random.uniform(0, exp / 2.0)


async def _connect_bot(
    config: Config, store: SessionStore, state: AppState, stop: asyncio.Event
) -> None:
    """
    Verbindet den Bot mit Discord — mit automatischem Fallback auf
    nicht-privilegierte Intents und Backoff bei Rate-Limits.

    Ohne den Intent-Fallback würde ein vergessener Schalter im Developer Portal
    (``SERVER MEMBERS INTENT``) zu einem endlosen Crash-Loop auf Render führen.
    Ohne das Backoff würde ein 429/Cloudflare-Bann durch sture Wiederholung
    endlos verlängert — der Bot käme nie wieder online.
    """
    privileged = config.enable_privileged_intents
    attempt = 0
    failures = 0
    last_failure = 0.0

    def note_failure() -> int:
        """Zählt einen Fehlversuch (mit Verjährung alter Ausfälle)."""
        nonlocal failures, last_failure
        now = time.monotonic()
        if now - last_failure > BACKOFF_RESET_AFTER:
            failures = 0
        failures += 1
        last_failure = now
        return failures

    def backoff() -> float:
        """Nächste Wartezeit aus Basis, Maximum und Fehlerzähler."""
        return _backoff_delay(
            config.login_retry_base_seconds,
            config.login_retry_max_seconds,
            note_failure(),
        )

    async def wait_and_report(delay: float, status: str, error: str) -> None:
        """Wartet (abbrechenbar) und spiegelt Grund + nächsten Versuch ins Health-API."""
        state.discord_status = status
        state.discord_last_error = error[:300]
        state.discord_retry_at = iso(now_utc() + timedelta(seconds=max(0.0, delay)))
        await _sleep_or_stop(stop, delay)

    while not stop.is_set():
        attempt += 1
        intents = build_intents(privileged)
        # WICHTIG: Ein ``client.close()`` beendet die interne aiohttp-Session
        # endgültig — derselbe Client kann danach NICHT erneut ``start()``en
        # (RuntimeError: "Session is closed"). Deshalb wird ab dem zweiten
        # Versuch immer ein frischer Client gebaut. Nur der allererste Durchlauf
        # nutzt den in amain() vorbereiteten, damit state.client nie None ist.
        client = state.client
        needs_fresh = (
            attempt > 1
            or not isinstance(client, RelayClient)
            or client.intents.value != intents.value
            or client.is_closed()
        )
        if needs_fresh:
            client = RelayClient(config, store, state, intents=intents)
            state.client = client

        state.discord_status = "connecting"
        state.discord_retry_at = None
        log.info("Verbinde Bot mit Discord (%s) …",
                 "mit privilegierten Intents" if privileged else "ohne privilegierte Intents")
        try:
            await client.start(config.discord_token, reconnect=True)
        except discord.PrivilegedIntentsRequired:
            with contextlib.suppress(Exception):
                await client.close()
            if privileged:
                log.error(
                    "❌ Discord verweigert die privilegierten Intents!\n"
                    "   Der Bot startet automatisch OHNE 'Server Members'/'Message Content' neu —\n"
                    "   er bleibt online, aber die Mitgliederliste ist eventuell unvollständig.\n"
                    "   ✅ Dauerhafte Lösung: https://discord.com/developers/applications\n"
                    "      → deine App → Bot → 'SERVER MEMBERS INTENT' aktivieren → speichern."
                )
                privileged = False
                continue
            # Sollte eigentlich nie passieren (ohne privilegierte Intents gibt es
            # nichts zu verweigern) — aber falls doch: kein Crash-Loop, sondern
            # in großem Abstand neu versuchen, Web-Server bleibt erreichbar.
            log.critical(
                "❌ Discord verweigert die Intents sogar ohne privilegierte Scopes. "
                "Neuer Versuch alle %.0f s — bitte Token und Developer-Portal prüfen.",
                config.fatal_retry_seconds,
            )
            await wait_and_report(
                config.fatal_retry_seconds, "connection_refused",
                "PrivilegedIntentsRequired trotz deaktivierter privilegierter Intents",
            )
            continue
        except discord.LoginFailure as exc:
            with contextlib.suppress(Exception):
                await client.close()
            # BEWUSST kein raise: Ein Exit würde Render sofort neu starten lassen
            # (Crash-Loop = Login-Spam = Cloudflare-Bann). Stattdessen bleibt der
            # Prozess mit grünem /api/health am Leben und versucht es später neu.
            log.critical(
                "❌ Login fehlgeschlagen: %s\n"
                "   DISCORD_BOT_TOKEN ist ungültig.\n"
                "   → https://discord.com/developers/applications → App → Bot → 'Reset Token'\n"
                "   → neues Token in Render unter Environment setzen → Save Changes.\n"
                "   Der Web-Server bleibt erreichbar, der Login wird alle %.0f s erneut versucht.",
                exc, config.fatal_retry_seconds,
            )
            await wait_and_report(
                config.fatal_retry_seconds, "invalid_token",
                f"LoginFailure: {exc}",
            )
            continue
        except discord.HTTPException as exc:
            with contextlib.suppress(Exception):
                await client.close()
            status = getattr(exc, "status", 0)
            retry_after = _extract_retry_after(exc)
            delay = backoff()
            if status == 429:
                # 429 beim Login = der kritische Fall: Jede weitere Anfrage kann
                # den Bann verlängern → echte Abkühlphase, kein Schnellschuss.
                floor = CLOUDFLARE_MIN_WAIT if _is_cloudflare_ban(exc) else DISCORD_RATELIMIT_MIN_WAIT
                delay = max(delay, floor)
            if retry_after is not None:
                delay = max(delay, retry_after + 5.0)
            delay = min(delay, MAX_SINGLE_WAIT)
            if status == 429 and _is_cloudflare_ban(exc):
                log.error(
                    "❌ Discord-Login rate-limitiert (HTTP 429, Cloudflare-Bann/Error 1015). "
                    "Die IP ist vorübergehend gesperrt — jeder weitere Versuch in dieser Zeit "
                    "würde den Bann VERLÄNGERN. Der Bot wartet jetzt automatisch %.0f s "
                    "(Fehlversuch %d in Folge) und verbindet sich dann neu. Bitte in dieser "
                    "Zeit NICHT manuell neu deployen/starten — das setzt die Wartezeit zurück! "
                    "Typische Ursachen: geteilte Render-IP (Free-Plan), derselbe Token in zwei "
                    "Prozessen (z. B. lokal + Render gleichzeitig) oder viele Restarts kurz "
                    "hintereinander.",
                    delay, failures,
                )
            elif status == 429:
                log.error(
                    "❌ Discord-Rate-Limit beim Login (HTTP 429). Neuer Versuch in %.0f s "
                    "(Fehlversuch %d in Folge).",
                    delay, failures,
                )
            else:
                log.error(
                    "❌ Discord meldete HTTP %s beim Login (%s). Neuer Versuch in %.0f s "
                    "(Fehlversuch %d in Folge).",
                    status, exc, delay, failures,
                )
            await wait_and_report(
                delay, "rate_limited" if status == 429 else "waiting",
                f"HTTP {status} beim Login: {exc}"[:300],
            )
            continue
        except discord.RateLimited as exc:
            with contextlib.suppress(Exception):
                await client.close()
            retry_after = _extract_retry_after(exc) or 0.0
            delay = min(max(backoff(), retry_after + 5.0), MAX_SINGLE_WAIT)
            log.error(
                "❌ Discord-Rate-Limit beim Login (Retry in %.0f s). Neuer Versuch in %.0f s.",
                retry_after, delay,
            )
            await wait_and_report(
                delay, "rate_limited",
                f"RateLimited: Retry in {retry_after:.0f} s",
            )
            continue
        except discord.GatewayNotFound:
            with contextlib.suppress(Exception):
                await client.close()
            delay = backoff()
            log.error("Discord-Gateway nicht erreichbar — neuer Versuch in %.0f s.", delay)
            await wait_and_report(delay, "waiting", "GatewayNotFound: Gateway nicht erreichbar")
            continue
        except discord.ConnectionClosed as exc:
            with contextlib.suppress(Exception):
                await client.close()
            if getattr(exc, "code", None) in {4004, 4010, 4011, 4012, 4013, 4014}:
                # BEWUSST kein raise (siehe LoginFailure): kein Crash-Loop.
                log.critical(
                    "❌ Discord hat die Verbindung dauerhaft geschlossen (Code %s: %s).\n"
                    "   4004/4010/4011/4012/4013/4014 = Token oder Intents sind falsch.\n"
                    "   Bitte Token prüfen und die Intents im Developer Portal kontrollieren.\n"
                    "   Der Web-Server bleibt erreichbar, neuer Versuch alle %.0f s.",
                    exc.code, exc.reason, config.fatal_retry_seconds,
                )
                await wait_and_report(
                    config.fatal_retry_seconds, "connection_refused",
                    f"Gateway dauerhaft geschlossen (Code {getattr(exc, 'code', '?')})",
                )
                continue
            delay = backoff()
            log.warning("Gateway-Verbindung getrennt (Code %s) — neuer Versuch in %.0f s.",
                        getattr(exc, "code", "?"), delay)
            await wait_and_report(
                delay, "waiting",
                f"Gateway getrennt (Code {getattr(exc, 'code', '?')})",
            )
            continue
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await client.close()
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await client.close()
            delay = backoff()
            log.exception("Unerwarteter Fehler beim Bot-Start (Versuch %d) — neuer Versuch in %.0f s.",
                          attempt, delay)
            await wait_and_report(delay, "waiting", "Unerwarteter Fehler beim Bot-Start (siehe Log)")
            continue

        # client.start() kehrt nur bei sauberem close() zurück
        state.discord_status = "closed"
        log.info("Bot-Verbindung beendet.")
        break


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def amain() -> int:
    """Asynchroner Hauptablauf. Liefert einen Exit-Code."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"\n❌ Konfigurationsfehler:\n{exc}\n", file=sys.stderr, flush=True)
        return 2

    setup_logging(config.log_level)
    log.info("%s v%s startet (Python %s, discord.py %s)",
             __botname__, __version__, sys.version.split()[0], discord.__version__)
    if os.getenv("_TOKEN_FORMAT_WARNING"):
        log.warning("DISCORD_BOT_TOKEN sieht ungewöhnlich aus — bitte prüfen.")

    log.info("Konfiguration:")
    for key, value in config.masked().items():
        log.info("  %-26s %s", key, value)

    store = _build_store(config)

    # Der Client wird gebaut, BEVOR der Web-Server startet. Er ist dann zwar noch
    # nicht mit Discord verbunden, aber state.client ist nie None — sonst würde
    # /api/health in den ersten Sekunden eines Deploys einen 500er liefern und
    # Render bzw. UptimeRobot melden den Dienst fälschlich als tot.
    #
    # Client und AppState verweisen aufeinander, deshalb wird der Verweis einmal
    # nach dem Erzeugen von beiden Seiten gesetzt (``wire``).
    client = RelayClient(config, store, state=None,
                         intents=build_intents(config.enable_privileged_intents))
    state = AppState(client=client, config=config, store=store)
    client.state = state

    app = build_app(state)
    runner: Optional[Any] = None
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass

    try:
        runner = await start_web_server(app, config.host, config.port)
        public = state.base_url()
        log.info("Öffentliche Basis-URL: %s", public)
        if "localhost" in public or "127.0.0.1" in public:
            log.warning(
                "⚠ Keine öffentliche URL erkannt — Arena AI kann den Link so NICHT erreichen.\n"
                "   Auf Render passiert das nicht (RENDER_EXTERNAL_URL wird automatisch gesetzt).\n"
                "   Lokal/anderswo: PUBLIC_URL=https://deine-domain.tld setzen."
            )
        log.info("  · Healthcheck : %s/api/health   ← hierhin UptimeRobot zeigen",
                 state.base_url())
        log.info("  · API-Doku    : %s/api/v1/capabilities", state.base_url())
        log.info("  · Console     : %s/console", state.base_url())

        housekeeping = asyncio.create_task(_housekeeping(state, stop), name="housekeeping")
        bot_task = asyncio.create_task(_connect_bot(config, store, state, stop), name="bot")

        waiter = asyncio.create_task(stop.wait(), name="shutdown-signal")
        done, _pending = await asyncio.wait(
            {bot_task, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter in done:
            log.info("Stop-Signal empfangen — fahre herunter …")
        if bot_task in done and not stop.is_set():
            exc = bot_task.exception()
            if exc is not None:
                log.critical("Bot-Task beendet mit Fehler: %r", exc)
                stop.set()
                housekeeping.cancel()
                if runner:
                    await stop_web_server(runner)
                return 1

        stop.set()
        housekeeping.cancel()
        bot_task.cancel()
        for task in (bot_task, housekeeping):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=10)
    except KeyboardInterrupt:  # pragma: no cover
        log.info("Strg+C — beende.")
    except Exception:
        log.exception("Schwerer Fehler im Hauptablauf.")
        return 1
    finally:
        if runner:
            await stop_web_server(runner)
        store.save()
        log.info("Auf Wiedersehen. 👋")

    return 0


def main() -> int:
    """Synchroner Einstiegspunkt (``python -m bot``)."""
    print(BANNER, flush=True)
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
