#!/usr/bin/env python3
"""
Test der Login-Wiederherstellung — **ohne Netzwerk, ohne Discord, ohne Wartezeit**.

Genau hier lag der Fehler, der den Bot dauerhaft offline hielt: Bei einem
Cloudflare-Block der ausgehenden IP (HTTP 429 / Error 1015) hat der Bot den
``Retry-After``-Wert der Blockseite geschluckt, bis zu **1800 s** gewartet und
dabei auch noch seinen eigenen Fehlerzähler zurückgesetzt („Fehlversuch 1 in
Folge" für immer). Ergebnis: ein Login-Versuch alle 30 Minuten, jeder davon
erneut in eine aktive Sperre — der Bot kam nie wieder hoch.

Dieser Test fährt die vier Fälle durch, die der neue Code unterscheiden muss:

A. IP-Sperre hebt sich      → tokenlose Proben, sofortiger Login danach
B. IP bleibt gesperrt       → ``RestartRequested`` (Exit-Code 3 = frische IP)
B2. Neustart deaktiviert    → endlos weiter probieren, kein Exit
C. IP frei, Login 429       → Token-Problem: lange warten, KEIN Neustart
D. Echtes Discord-Rate-Limit→ ``Retry-After`` wird gedeckelt (nie 1800 s)
D2. HTTP-Fehler             → Backoff eskaliert wieder (Zähler verfällt nicht)

Dazu kommen Unit-Prüfungen für die Erkennung selbst (``Via``-Header,
Cloudflare-HTML, Backoff-Rechnung, Konfigurations-Validierung, Proxy-Maskierung).

Die Zeit wird durch eine Fake-Uhr ersetzt: Der Test läuft in Millisekunden und
prüft trotzdem die echten Wartewerte.

Aufruf::

    python3 scripts/login_recovery_test.py            # alles
    python3 scripts/login_recovery_test.py -v         # mit Details

Exit-Code 0 = grün. Läuft in CI ohne Netzwerk und ohne Bot-Token.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time as _real_time
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISCORD_BOT_TOKEN", "recovery.test.token")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("PUBLIC_URL", "https://relay.example.com")

import discord  # noqa: E402

from bot import main as botmain  # noqa: E402
from bot.config import Config, ConfigError, load_config, mask_proxy_url  # noqa: E402
from bot.discord_bot import RelayClient  # noqa: E402
from bot.netcheck import (  # noqa: E402
    DISCORD_PROBE_URL,
    VERDICT_IP_BLOCKED,
    VERDICT_NETWORK_ERROR,
    VERDICT_OK,
    NetReport,
    ProbeResult,
    classify_response,
)
from bot.sessions import SessionStore  # noqa: E402
from bot.web.app import AppState  # noqa: E402

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

PASSED: List[str] = []
FAILED: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    """Verzeichnet eine einzelne Prüfung."""
    if condition:
        PASSED.append(name)
        if VERBOSE:
            print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  ❌ {name}{(' — ' + detail) if detail else ''}")
    return bool(condition)


# ─────────────────────────────────────────────────────────────────────────────
#  Test-Doubles
# ─────────────────────────────────────────────────────────────────────────────


class FakeResponse:
    """Minimaler ``aiohttp.ClientResponse``-Ersatz für discord.py-Exceptions."""

    def __init__(self, status: int, headers: Optional[Dict[str, str]] = None,
                 reason: str = "Too Many Requests") -> None:
        self.status = status
        self.headers = headers if headers is not None else {}
        self.reason = reason


CLOUDFLARE_BODY = (
    "<html><head><title>Access denied | discord.com used Cloudflare to restrict access</title>"
    "</head><body>Error 1015 — You are being rate limited</body></html>"
)


def cloudflare_429(retry_after: Optional[str] = "3600") -> discord.HTTPException:
    """429 *ohne* Via-Header + HTML-Seite = Cloudflare-Block (Error 1015)."""
    headers = {"CF-RAY": "76714c325ede2a1e-FRA", "Server": "cloudflare"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return discord.HTTPException(FakeResponse(429, headers), CLOUDFLARE_BODY)


def discord_429(retry_after: float = 12.0) -> discord.RateLimited:
    """Echtes Discord-Rate-Limit (429 *mit* Via-Header, JSON-Body)."""
    return discord.RateLimited(retry_after)


def server_error(status: int = 500) -> discord.HTTPException:
    """Beliebiger HTTP-Fehler mit Via-Header (also von Discord, nicht Cloudflare)."""
    return discord.HTTPException(FakeResponse(status, {"Via": "1.1 google"}), "boom")


def blocked_report(ip: str = "203.0.113.7", retry_after: Optional[float] = 120.0) -> NetReport:
    """
    Report: ausgehende IP wird von Cloudflare blockiert.

    ``retry_after`` klein (Default 120 s) ⇒ normales 10-Minuten-Fenster.
    Werte oberhalb von ``BAN_FAST_RESTART_ABOVE_SECONDS`` (600 s) lösen den
    verkürzten Schnell-Neustart aus — siehe Test B4.
    """
    return NetReport(
        verdict=VERDICT_IP_BLOCKED,
        egress_ip=ip,
        egress_source="https://api.ipify.org",
        discord=ProbeResult(url=DISCORD_PROBE_URL, status=429, blocked=True,
                            cloudflare_page=True, cf_ray="deadbeef-FRA",
                            retry_after=retry_after, ok=False),
        hint="Cloudflare blockt die ausgehende IP.",
        checked_at="2026-01-01T00:00:00Z",
    )


def ok_report(ip: str = "203.0.113.7") -> NetReport:
    """Report: discord.com ist ohne Token erreichbar."""
    return NetReport(
        verdict=VERDICT_OK,
        egress_ip=ip,
        egress_source="https://api.ipify.org",
        discord=ProbeResult(url=DISCORD_PROBE_URL, status=200, ok=True),
        hint="discord.com erreichbar.",
        checked_at="2026-01-01T00:00:00Z",
    )


def dead_report() -> NetReport:
    """Report: discord.com gar nicht erreichbar (DNS/TCP)."""
    return NetReport(
        verdict=VERDICT_NETWORK_ERROR,
        discord=ProbeResult(url=DISCORD_PROBE_URL, error="ClientConnectorError: cannot connect"),
        hint="nicht erreichbar",
    )


class FakeClock:
    """
    Deterministische Uhr: ersetzt ``time.monotonic`` in :mod:`bot.main`.

    Jeder „Schlaf" im Test dreht die Uhr weiter — so lassen sich Wartewerte,
    Eskalation und Beobachtungsfenster in Millisekunden prüfen.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(float(seconds), 3))
        self.now += float(seconds)

    def __getattr__(self, name: str) -> Any:
        """Alles außer ``monotonic`` unverändert vom echten ``time``-Modul."""
        return getattr(_real_time, name)


class Scenario:
    """Zustand + Patches + gescriptetes Verhalten für einen Testlauf."""

    def __init__(self, *, reports: List[NetReport], start_behavior: Callable[[int], Any],
                 config_overrides: Optional[Dict[str, Any]] = None) -> None:
        self.clock = FakeClock()
        self.reports = list(reports)
        self.start_behavior = start_behavior
        self.start_calls = 0
        self.probe_calls = 0
        self.status_seen: List[str] = []
        self.errors_seen: List[str] = []
        self.config = Config(**{
            "discord_token": "recovery.test.token",
            "ban_probe_interval_seconds": 30.0,
            "ban_watch_seconds": 600.0,
            "restart_delay_seconds": 30.0,
            "login_retry_base_seconds": 15.0,
            "login_retry_max_seconds": 600.0,
            "fatal_retry_seconds": 300.0,
            "netcheck_timeout_seconds": 8.0,
            **(config_overrides or {}),
        })
        self.store = SessionStore(path=None, persist=False)
        self.client = RelayClient(self.config, self.store, state=None)
        self.state = AppState(client=self.client, config=self.config, store=self.store)
        self.client.state = self.state
        self.stop = asyncio.Event()
        self._patches: List[Tuple[Any, str, Any]] = []
        self._client_patches: List[Tuple[str, Any]] = []

    # ── Patches ─────────────────────────────────────────────────────────────
    def __enter__(self) -> "Scenario":
        self._patch("time", self.clock)
        self._patch("_sleep_or_stop", self._fake_sleep)
        self._patch("run_netcheck", self._fake_netcheck)
        self._client_patch("start", self._fake_start)
        self._client_patch("close", self._fake_close)
        return self

    def __exit__(self, *exc: Any) -> None:
        for target, name, old in reversed(self._patches):
            setattr(target, name, old)
        for name, old in reversed(self._client_patches):
            setattr(RelayClient, name, old)
        self._patches.clear()
        self._client_patches.clear()

    def _patch(self, name: str, value: Any) -> None:
        self._patches.append((botmain, name, getattr(botmain, name)))
        setattr(botmain, name, value)

    def _client_patch(self, name: str, value: Any) -> None:
        self._client_patches.append((name, getattr(RelayClient, name)))
        setattr(RelayClient, name, value)

    async def _fake_sleep(self, stop: asyncio.Event, seconds: float) -> None:
        # Beim Warten ist der interessante Status gesetzt (wait_and_report bzw.
        # _watch_ip_ban setzen ihn unmittelbar vorher).
        self.status_seen.append(self.state.discord_status)
        self.errors_seen.append(self.state.discord_last_error or "")
        self.clock.sleep(seconds)

    async def _fake_netcheck(self, **kwargs: Any) -> NetReport:
        self.probe_calls += 1
        return self.reports[min(self.probe_calls - 1, len(self.reports) - 1)]

    async def _fake_start(self, token: str, *, reconnect: bool = True) -> None:
        self.start_calls += 1
        result = self.start_behavior(self.start_calls)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result()
        if asyncio.iscoroutine(result):
            await result

    async def _fake_close(self) -> None:
        return None

    def finish(self) -> Callable[[], Any]:
        """Rückgabe für ``start_behavior``: beendet den Loop wie ein sauberer Start."""
        async def _inner() -> None:
            self.stop.set()
        return _inner

    # ── Lauf ────────────────────────────────────────────────────────────────
    async def run(self, *, first_report: Optional[NetReport] = None,
                  timeout: float = 5.0) -> Optional[BaseException]:
        """Führt ``_connect_bot`` aus und liefert die Exception (oder None)."""
        if first_report is not None:
            self.state.set_net_report(first_report)
        else:
            self.state.net_ready().set()
        try:
            await asyncio.wait_for(
                botmain._connect_bot(self.config, self.store, self.state, self.stop),
                timeout=timeout,
            )
        except BaseException as exc:  # noqa: BLE001 — genau die wollen wir bewerten
            return exc
        return None

    @property
    def longest_wait(self) -> float:
        return max(self.clock.slept) if self.clock.slept else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  A: IP-Sperre hebt sich → Proben statt 30-Minuten-Schlaf
# ─────────────────────────────────────────────────────────────────────────────


async def test_ip_ban_lifts() -> None:
    print("\n── A: IP-Sperre hebt sich während der Beobachtung ───────────")

    scenario = Scenario(
        reports=[blocked_report(), blocked_report(), ok_report()],
        start_behavior=lambda call: cloudflare_429() if call == 1 else None,
    )
    scenario.start_behavior = lambda call: cloudflare_429() if call == 1 else scenario.finish()

    with scenario:
        exc = await scenario.run(first_report=ok_report())

    check("A: Loop endet ohne Fehler", exc is None, f"{exc!r}")
    check("A: nach der Sperre wurde sofort neu eingeloggt", scenario.start_calls == 2,
          f"start_calls={scenario.start_calls}")
    check("A: davor tokenlos probiert", scenario.probe_calls >= 3,
          f"probe_calls={scenario.probe_calls}")
    check("A: Beobachtungszähler gesetzt", scenario.state.ban_watches >= 1,
          f"ban_watches={scenario.state.ban_watches}")
    check("A: KEIN 1800-Sekunden-Schlaf mehr", scenario.longest_wait <= 60.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("A: kein Neustart verlangt", scenario.state.restart_requested is None)
    check("A: Status war während der Sperre ip_blocked",
          "ip_blocked" in scenario.status_seen, str(scenario.status_seen))


# ─────────────────────────────────────────────────────────────────────────────
#  B: IP bleibt gesperrt → frischer Container (neue IP) statt Endlos-Warten
# ─────────────────────────────────────────────────────────────────────────────


async def test_ip_ban_persists() -> None:
    print("\n── B: IP bleibt gesperrt → RestartRequested (Exit-Code 3) ───")

    scenario = Scenario(
        reports=[blocked_report()] * 40,
        start_behavior=lambda call: cloudflare_429(),
    )
    with scenario:
        exc = await scenario.run(first_report=blocked_report())

    check("B: RestartRequested geworfen", isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B: Exit-Code 3 (Plattform soll neu starten)",
          getattr(exc, "exit_code", None) == botmain.EXIT_CODE_RESTART,
          str(getattr(exc, "exit_code", None)))
    check("B: Beobachtungsfenster ausgeschöpft (20 Proben à 30 s)",
          scenario.probe_calls == 20, f"probe_calls={scenario.probe_calls}")
    check("B: niemals 1800 s geschlafen", scenario.longest_wait <= 60.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("B: Grund im Zustand hinterlegt",
          str(scenario.state.restart_requested or "").startswith("ip_blocked"),
          str(scenario.state.restart_requested))
    check("B: Status ip_blocked (für /api/health)",
          scenario.state.discord_status == "ip_blocked", scenario.state.discord_status)
    check("B: kein einziger Login-Versuch in die aktive Sperre",
          scenario.start_calls == 0, f"start_calls={scenario.start_calls}")


async def test_ip_ban_persists_no_restart() -> None:
    print("\n── B2: RESTART_ON_IP_BAN=false → weiter probieren, kein Exit ─")

    scenario = Scenario(
        reports=[blocked_report()] * 200,
        start_behavior=lambda call: cloudflare_429(),
        config_overrides={"restart_on_ip_ban": False},
    )
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 3 else cloudflare_429()
    )

    with scenario:
        exc = await scenario.run(first_report=blocked_report())

    check("B2: kein RestartRequested", not isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B2: mehrfach neu versucht", scenario.start_calls == 3,
          f"start_calls={scenario.start_calls}")
    check("B2: zwei Beobachtungsfenster durchprobiert", scenario.probe_calls >= 40,
          f"probe_calls={scenario.probe_calls}")
    check("B2: Neustart-Flag bleibt leer", scenario.state.restart_requested is None)
    check("B2: Abkühlphase statt Heißloop (≥ 120 s zwischen den Versuchen)",
          scenario.longest_wait >= 120.0, f"längste Wartezeit {scenario.longest_wait:.0f} s")


async def test_unlimited_watch() -> None:
    print("\n── B3: BAN_WATCH_SECONDS=0 → unbegrenzt probieren, nie neu starten ─")

    blocked, ok = blocked_report(), ok_report()
    scenario = Scenario(
        reports=[blocked] * 5 + [ok] + [blocked] * 2 + [ok] * 3,
        start_behavior=lambda call: None,
        config_overrides={"ban_watch_seconds": 0.0},
    )
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 2 else cloudflare_429()
    )

    with scenario:
        exc = await scenario.run(first_report=blocked)

    check("B3: kein RestartRequested", not isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B3: Loop endet sauber", exc is None, f"{exc!r}")
    check("B3: unbegrenzt probiert, bis die IP frei war", scenario.probe_calls == 9,
          f"probe_calls={scenario.probe_calls}")
    check("B3: danach sofort neuer Login", scenario.start_calls == 2,
          f"start_calls={scenario.start_calls}")
    check("B3: kein langer Blind-Schlaf", scenario.longest_wait <= 60.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")


async def test_fast_restart_on_long_retry_after() -> None:
    print("\n── B4: Retry-After 8034 s → verkürztes Fenster, schneller Neustart ─")

    # Exakt der gemessene Render-Fall: Cloudflare-Ray …-PDX, Retry-After 8034 s.
    long_ban = blocked_report(ip="74.220.48.143", retry_after=8034.0)
    scenario = Scenario(
        reports=[long_ban] * 40,
        start_behavior=lambda call: cloudflare_429("8034"),
    )
    with scenario:
        exc = await scenario.run(first_report=long_ban)

    check("B4: RestartRequested geworfen", isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B4: nur 3 Proben statt 20 (Fenster 90 s statt 600 s)",
          scenario.probe_calls == 3, f"probe_calls={scenario.probe_calls}")
    total = sum(scenario.clock.slept)
    check("B4: Gesamtwartezeit bis zum Neustart ≤ 150 s (90 s Proben + 30 s Delay)",
          total <= 150.0, f"{total:.0f} s")
    check("B4: kein Login-Versuch in die aktive Sperre", scenario.start_calls == 0,
          f"start_calls={scenario.start_calls}")
    check("B4: Neustart im Buch verzeichnet (Zyklus 1)",
          scenario.state.restart_ledger is not None and scenario.state.restart_ledger.cycles == 1,
          str(getattr(scenario.state.restart_ledger, "cycles", None)))
    check("B4: gesperrte IP im Buch",
          "74.220.48.143" in getattr(scenario.state.restart_ledger, "ips", []),
          str(getattr(scenario.state.restart_ledger, "ips", None)))

    # Gegenprobe: kurzer Retry-After ⇒ volles Fenster (kein Schnell-Neustart).
    short_ban = blocked_report(retry_after=300.0)
    scenario = Scenario(reports=[short_ban] * 40, start_behavior=lambda call: cloudflare_429("300"))
    with scenario:
        exc = await scenario.run(first_report=short_ban)
    check("B4: Retry-After 300 s ⇒ normales Fenster (20 Proben)",
          scenario.probe_calls == 20, f"probe_calls={scenario.probe_calls}")

    # Schnell-Neustart abgeschaltet ⇒ volles Fenster trotz langem Retry-After.
    scenario = Scenario(reports=[long_ban] * 40, start_behavior=lambda call: cloudflare_429("8034"),
                        config_overrides={"ban_fast_restart_above_seconds": 0.0})
    with scenario:
        await scenario.run(first_report=long_ban)
    check("B4: BAN_FAST_RESTART_ABOVE_SECONDS=0 ⇒ volles Fenster",
          scenario.probe_calls == 20, f"probe_calls={scenario.probe_calls}")


async def test_restart_cycle_limit() -> None:
    print("\n── B5: Neustart-Limit → aufhören zu würfeln, nur noch proben ────")
    import tempfile

    from bot.restarts import RestartLedger

    data_dir = tempfile.mkdtemp(prefix="adse-ledger-")
    ban = blocked_report(ip="74.220.48.143")
    cycles_seen: List[int] = []

    # Fünf „Container-Leben" hintereinander — jedes lädt das Buch vom letzten.
    for life in range(1, 6):
        scenario = Scenario(reports=[ban] * 40, start_behavior=lambda call: cloudflare_429(),
                            config_overrides={"restart_max_cycles": 5})
        scenario.state.restart_ledger = RestartLedger.open(data_dir)
        with scenario:
            exc = await scenario.run(first_report=ban)
        cycles_seen.append(scenario.state.restart_ledger.cycles)
        check(f"B5: Leben {life} endet mit RestartRequested",
              isinstance(exc, botmain.RestartRequested), f"{exc!r}")

    check("B5: Zyklen zählen über Prozessgrenzen hoch (1…5)", cycles_seen == [1, 2, 3, 4, 5],
          str(cycles_seen))
    check("B5: Buch liegt auf Platte",
          os.path.exists(os.path.join(data_dir, "restart_ledger.json")))

    # Sechstes Leben: Limit erreicht ⇒ KEIN Neustart mehr, nur Proben + Abkühlen.
    scenario = Scenario(reports=[ban] * 200, start_behavior=lambda call: cloudflare_429(),
                        config_overrides={"restart_max_cycles": 5})
    scenario.state.restart_ledger = RestartLedger.open(data_dir)
    ledger = scenario.state.restart_ledger
    check("B5: Buch beim Start geladen (5 Zyklen)", ledger.cycles == 5 and ledger.found_at_start,
          f"cycles={ledger.cycles} found={ledger.found_at_start}")
    check("B5: exhausted() bei 5/5", ledger.exhausted(5))

    # Zwei Beobachtungsfenster + Abkühlphasen durchlaufen lassen, dann stoppen.
    probes_before_stop = 45

    async def counting_netcheck(**kwargs: Any) -> NetReport:
        scenario.probe_calls += 1
        if scenario.probe_calls >= probes_before_stop:
            scenario.stop.set()
        return ban

    with scenario:
        botmain.run_netcheck = counting_netcheck  # type: ignore[assignment]
        exc = await scenario.run(first_report=ban)

    check("B5: nach dem Limit KEIN RestartRequested",
          not isinstance(exc, botmain.RestartRequested), f"{exc!r}")
    check("B5: Zähler bleibt bei 5 (kein weiterer Zyklus gebucht)", ledger.cycles == 5,
          str(ledger.cycles))
    check("B5: Plattform-Urteil gesetzt", bool(scenario.state.platform_verdict),
          str(scenario.state.platform_verdict)[:120])
    check("B5: Urteil nennt die IP und die Lösung",
          "74.220.48.143" in (scenario.state.platform_verdict or "")
          and "VPS" in (scenario.state.platform_verdict or ""),
          str(scenario.state.platform_verdict)[:200])
    check("B5: Abkühlphase ≥ 120 s zwischen den Fenstern", scenario.longest_wait >= 120.0,
          f"{scenario.longest_wait:.0f} s")
    check("B5: weiter probiert (≥ 40 Proben)", scenario.probe_calls >= 40,
          f"probe_calls={scenario.probe_calls}")
    check("B5: Status ip_blocked", scenario.state.discord_status == "ip_blocked",
          scenario.state.discord_status)
    check("B5: last_error nannte das Plattform-Limit während der Abkühlphase",
          any("Plattform-Limit" in e for e in scenario.errors_seen),
          str(scenario.errors_seen[-3:]))

    # Erfolgreicher Login ⇒ Buch wird gelöscht.
    ledger.clear(reason="Test")
    check("B5: clear() entfernt die Datei",
          not os.path.exists(os.path.join(data_dir, "restart_ledger.json")))
    check("B5: clear() setzt Zähler zurück", ledger.cycles == 0 and ledger.ips == [])

    # RESTART_MAX_CYCLES=0 ⇒ unbegrenzt.
    scenario = Scenario(reports=[ban] * 40, start_behavior=lambda call: cloudflare_429(),
                        config_overrides={"restart_max_cycles": 0})
    scenario.state.restart_ledger = RestartLedger(path=None, cycles=99)
    with scenario:
        exc = await scenario.run(first_report=ban)
    check("B5: RESTART_MAX_CYCLES=0 ⇒ trotz 99 Zyklen weiter neu starten",
          isinstance(exc, botmain.RestartRequested), f"{exc!r}")


def test_ledger_units() -> None:
    print("\n── Unit: Neustart-Buch (RestartLedger) ───────────────────────")
    import tempfile

    from bot.restarts import RestartLedger

    data_dir = tempfile.mkdtemp(prefix="adse-ledger-unit-")
    ledger = RestartLedger.open(data_dir)
    check("frisches Buch: 0 Zyklen, nicht gefunden", ledger.cycles == 0 and not ledger.found_at_start)
    check("frisches Buch: persistent unbekannt", ledger.persistent is None)
    check("note_boot_ip ohne Vergleichsbasis → None", ledger.note_boot_ip("198.51.100.1") is None)

    ledger.record_restart(egress_ip="198.51.100.1", cause="Test")
    check("record_restart zählt hoch", ledger.cycles == 1)
    check("record_restart schreibt Datei", ledger.saved_ok is True)

    reloaded = RestartLedger.open(data_dir)
    check("Reload: Zyklus 1 gelesen", reloaded.cycles == 1 and reloaded.found_at_start, str(reloaded.cycles))
    check("Reload: persistent = True", reloaded.persistent is True)
    check("Reload: letzte IP bekannt", reloaded.last_ip == "198.51.100.1")
    check("dieselbe IP nach Neustart → False", reloaded.note_boot_ip("198.51.100.1") is False)
    check("same_ip_streak = 1", reloaded.same_ip_streak == 1, str(reloaded.same_ip_streak))
    check("andere IP nach Neustart → True", reloaded.note_boot_ip("198.51.100.2") is True)
    check("streak zurück auf 0", reloaded.same_ip_streak == 0)
    check("beide IPs gemerkt", reloaded.ips == ["198.51.100.1", "198.51.100.2"], str(reloaded.ips))
    check("remaining(5) = 4", reloaded.remaining(5) == 4)
    check("remaining(0) = None (unbegrenzt)", reloaded.remaining(0) is None)
    check("exhausted(1) = True", reloaded.exhausted(1))
    data = reloaded.to_dict(5)
    check("to_dict enthält Kernfelder",
          {"cycles", "ips_seen", "history", "exhausted", "ledger_persistent"} <= set(data))
    check("summary nennt Zyklus und IPs",
          "Zyklus 1/5" in reloaded.summary(5) and "198.51.100.2" in reloaded.summary(5),
          reloaded.summary(5))

    # Verjährung: uralter Stand wird verworfen.
    import json as _json
    path = os.path.join(data_dir, "restart_ledger.json")
    with open(path, encoding="utf-8") as fh:
        raw = _json.load(fh)
    raw["updated_at"] = "2020-01-01T00:00:00Z"
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump(raw, fh)
    stale = RestartLedger.open(data_dir)
    check("verjährter Stand → 0 Zyklen, stale_at_start", stale.cycles == 0 and stale.stale_at_start)

    # Kaputte Datei darf nichts kaputt machen.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ kaputt")
    broken = RestartLedger.open(data_dir)
    check("kaputte Datei → 0 Zyklen + load_error", broken.cycles == 0 and bool(broken.load_error))

    # Ohne Pfad: rein im Speicher, save() = False, nie eine Exception.
    mem = RestartLedger(path=None)
    mem.record_restart(egress_ip=None, cause="x")
    check("Speicher-Buch zählt, speichert aber nicht", mem.cycles == 1 and mem.saved_ok is False)
    check("Speicher-Buch: persistent = False", mem.persistent is False)
    mem.clear()
    check("clear() im Speicher-Buch", mem.cycles == 0)


def test_egress_tracking_units() -> None:
    print("\n── Unit: Ausgangs-IP-Wechsel innerhalb eines Prozesses ──────")
    cfg = load_config()
    store = SessionStore(path=None, persist=False)
    client = RelayClient(cfg, store, state=None)
    state = AppState(client=client, config=cfg, store=store)

    check("erste Messung → None", state.set_net_report(blocked_report(ip="10.0.0.1")) is None)
    check("gleiche IP → False", state.set_net_report(blocked_report(ip="10.0.0.1")) is False)
    check("andere IP → True", state.set_net_report(blocked_report(ip="10.0.0.2")) is True)
    check("Wechsel gezählt", state.egress_ip_changes == 1, str(state.egress_ip_changes))
    check("beide IPs gesehen", state.egress_ips_seen == ["10.0.0.1", "10.0.0.2"],
          str(state.egress_ips_seen))
    no_ip = blocked_report(ip=None)  # type: ignore[arg-type]
    no_ip.egress_ip = None
    check("Probe ohne IP-Messung → None", state.set_net_report(no_ip) is None)
    check("… erbt die letzte bekannte IP (für /api/health)", state.egress_ip == "10.0.0.2",
          str(state.egress_ip))


# ─────────────────────────────────────────────────────────────────────────────
#  C: IP frei, Login trotzdem 429 → Token-Problem (keine neue IP nötig)
# ─────────────────────────────────────────────────────────────────────────────


async def test_token_level_rate_limit() -> None:
    print("\n── C: IP frei, Login 429 → Token-Problem, kein Neustart ─────")

    scenario = Scenario(reports=[ok_report()] * 10, start_behavior=lambda call: None)
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 2 else cloudflare_429("3600")
    )

    with scenario:
        exc = await scenario.run(first_report=ok_report())

    check("C: Loop endet ohne Fehler", exc is None, f"{exc!r}")
    check("C: KEIN Neustart (neue IP würde nichts bringen)",
          not isinstance(exc, botmain.RestartRequested) and scenario.state.restart_requested is None)
    check("C: Status rate_limited", "rate_limited" in scenario.status_seen,
          str(scenario.status_seen))
    check("C: lange Wartezeit statt Proben-Spam", scenario.longest_wait >= 300.0,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("C: Fehlermeldung erwähnt die erreichbare IP/das Token",
          "429" in (scenario.state.discord_last_error or ""),
          str(scenario.state.discord_last_error))


# ─────────────────────────────────────────────────────────────────────────────
#  D: Echtes Discord-Rate-Limit → Retry-After gedeckelt (alter 1800-s-Bug)
# ─────────────────────────────────────────────────────────────────────────────


async def test_real_ratelimit_is_capped() -> None:
    print("\n── D: Discord-429 mit Via-Header → Wartezeit gedeckelt ──────")

    scenario = Scenario(reports=[ok_report()] * 5, start_behavior=lambda call: None)
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 2 else discord_429(1795.0)
    )

    with scenario:
        await scenario.run(first_report=ok_report())

    check("D: riesiges Retry-After wird gedeckelt",
          scenario.longest_wait <= botmain.MAX_SINGLE_WAIT + 1,
          f"längste Wartezeit {scenario.longest_wait:.0f} s")
    check("D: MAX_SINGLE_WAIT ist nicht mehr 1800 s", botmain.MAX_SINGLE_WAIT <= 300.0,
          str(botmain.MAX_SINGLE_WAIT))
    check("D: Status rate_limited", "rate_limited" in scenario.status_seen,
          str(scenario.status_seen))


async def test_backoff_escalates() -> None:
    print("\n── D2: Backoff eskaliert wieder (Fehlerzähler verfällt nicht) ─")

    scenario = Scenario(reports=[dead_report()] * 10, start_behavior=lambda call: None)
    scenario.start_behavior = (
        lambda call: scenario.finish() if call >= 5 else server_error(500)
    )

    with scenario:
        await scenario.run(first_report=dead_report())

    waits = [w for w in scenario.clock.slept if w > 0]
    check("D2: vier Fehlversuche gezählt", scenario.state.login_failures == 4,
          str(scenario.state.login_failures))
    check("D2: Wartezeiten steigen", len(waits) >= 4 and waits[-1] > waits[0], f"{waits}")
    check("D2: BACKOFF_RESET_AFTER > MAX_SINGLE_WAIT (sonst verfällt der Zähler)",
          botmain.BACKOFF_RESET_AFTER > botmain.MAX_SINGLE_WAIT,
          f"{botmain.BACKOFF_RESET_AFTER} vs {botmain.MAX_SINGLE_WAIT}")
    check("D2: Status network_error/waiting gesetzt",
          scenario.state.discord_status in {"waiting", "network_error", "closed"},
          scenario.state.discord_status)


# ─────────────────────────────────────────────────────────────────────────────
#  E: End-to-End — kompletter Prozess inklusive Web-Server und Exit-Code
# ─────────────────────────────────────────────────────────────────────────────


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_amain_exit_code() -> None:
    """
    Fährt den **echten** Hauptablauf: Web-Server hoch, Start-Diagnose, Login-Versuch,
    Beobachtung, bewusster Neustart — und prüft den Exit-Code, den Render sieht.
    """
    print("\n── E: End-to-End über amain() → Exit-Code 3 ─────────────────")
    import logging
    import tempfile

    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="adse-recovery-")
    env_backup = {k: os.environ.get(k) for k in
                  ("PORT", "HOST", "DATA_DIR", "BAN_WATCH_SECONDS", "BAN_PROBE_INTERVAL_SECONDS",
                   "RESTART_DELAY_SECONDS", "LOG_LEVEL", "PERSIST_SESSIONS")}
    os.environ.update({
        "PORT": str(port), "HOST": "127.0.0.1", "DATA_DIR": data_dir,
        "BAN_WATCH_SECONDS": "600", "BAN_PROBE_INTERVAL_SECONDS": "30",
        "RESTART_DELAY_SECONDS": "30", "LOG_LEVEL": "CRITICAL",
        "PERSIST_SESSIONS": "false",
    })

    clock = FakeClock()
    patches: List[Tuple[Any, str, Any]] = []

    def patch(target: Any, name: str, value: Any) -> None:
        patches.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    health_holder = {"body": ""}

    async def fake_sleep(stop: asyncio.Event, seconds: float) -> None:
        clock.sleep(seconds)
        # Mitten in der Beobachtungsphase (3. Schlaf) den echten Web-Server
        # abfragen: Genau dann muss /api/health die Sperre ausweisen.
        if len(clock.slept) == 3 and not health_holder["body"]:
            import aiohttp

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}/api/health",
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as response:
                        health_holder["body"] = await response.text()
            except Exception:  # noqa: BLE001 — Server evtl. noch nicht bereit
                health_holder["body"] = ""

    async def fake_netcheck(**kwargs: Any) -> NetReport:
        return blocked_report()

    async def fake_start(self: Any, token: str, *, reconnect: bool = True) -> None:
        raise cloudflare_429()

    async def fake_close(self: Any) -> None:
        return None

    patch(botmain, "time", clock)
    patch(botmain, "_sleep_or_stop", fake_sleep)
    patch(botmain, "run_netcheck", fake_netcheck)
    patch(RelayClient, "start", fake_start)
    patch(RelayClient, "close", fake_close)

    logging.disable(logging.CRITICAL)
    exit_code: Any = None
    try:
        # Der Ablauf ist mit der Fake-Uhr in Millisekunden durch; /api/health
        # wird aus fake_sleep heraus abgefragt, also garantiert zur Laufzeit.
        exit_code = await asyncio.wait_for(botmain.amain(), timeout=15)
    except BaseException as exc:  # noqa: BLE001
        exit_code = exc
    finally:
        logging.disable(logging.NOTSET)
        for target, name, old_value in reversed(patches):
            setattr(target, name, old_value)
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    check("E: Exit-Code 3 für den Plattform-Neustart", exit_code == 3, str(exit_code))
    health_body = health_holder["body"]
    check("E: /api/health antwortete während der Sperre", '"status":"healthy"' in health_body,
          health_body[:160])
    check("E: Health nennt discord_status=ip_blocked", '"discord_status":"ip_blocked"' in health_body,
          health_body[:300])
    check("E: Health nennt discord_reachable=false", '"discord_reachable":false' in health_body,
          health_body[:300])
    check("E: Health nennt die gesperrte Ausgangs-IP", '"egress_ip":"203.0.113.7"' in health_body,
          health_body[:300])
    check("E: kein 1800-s-Schlaf im echten Ablauf",
          (max(clock.slept) if clock.slept else 0) <= 60.0,
          f"längste Wartezeit {max(clock.slept) if clock.slept else 0:.0f} s")


# ─────────────────────────────────────────────────────────────────────────────
#  Unit-Prüfungen
# ─────────────────────────────────────────────────────────────────────────────


def test_detection_units() -> None:
    print("\n── Unit: Cloudflare-Erkennung ───────────────────────────────")

    check("429 ohne Via-Header = Cloudflare",
          botmain._is_cloudflare_ban(cloudflare_429()) is True)
    with_via_html = discord.HTTPException(FakeResponse(429, {"Via": "1.1 google"}), CLOUDFLARE_BODY)
    check("429 mit Via, aber HTML-Body = Cloudflare/Proxy",
          botmain._is_cloudflare_ban(with_via_html) is True)
    json_429 = discord.HTTPException(
        FakeResponse(429, {"Via": "1.1 google"}),
        {"message": "You are being rate limited.", "retry_after": 12.5},
    )
    check("429 mit Via + JSON = echtes Discord-Limit (kein IP-Bann)",
          botmain._is_cloudflare_ban(json_429) is False)
    check("500er ist kein Cloudflare-Bann", botmain._is_cloudflare_ban(server_error()) is False)
    check("Retry-After aus dem Header gelesen",
          botmain._extract_retry_after(cloudflare_429("1795")) == 1795.0)
    check("Retry-After aus RateLimited gelesen",
          botmain._extract_retry_after(discord_429(42.0)) == 42.0)
    check("kein Retry-After → None", botmain._extract_retry_after(json_429) is None)

    blocked, cf_page, _ = classify_response(429, {"Server": "cloudflare"}, CLOUDFLARE_BODY)
    check("classify_response erkennt Blockseite", blocked and cf_page)
    blocked, cf_page, _ = classify_response(200, {"Via": "1.1 google"}, '{"url":"wss://x"}')
    check("classify_response: 200 ist nicht blockiert", not blocked and not cf_page)
    blocked, _, _ = classify_response(429, {"Via": "1.1 google"}, '{"message":"too many"}')
    check("classify_response: Discord-429 mit Via nicht als Block gewertet", not blocked)


def test_backoff_units() -> None:
    print("\n── Unit: Backoff-Rechnung ───────────────────────────────────")
    first = [botmain._backoff_delay(15.0, 600.0, 1) for _ in range(50)]
    check("erster Versuch wartet 7,5–15 s", all(7.4 <= v <= 15.1 for v in first),
          f"min={min(first):.1f} max={max(first):.1f}")
    tenth = [botmain._backoff_delay(15.0, 600.0, 10) for _ in range(50)]
    check("zehnter Versuch bleibt unter dem Maximum", all(v <= 600.0 for v in tenth),
          f"max={max(tenth):.1f}")
    check("Jitter sorgt für Streuung", len({round(v, 3) for v in first}) > 10)


def test_config_units() -> None:
    print("\n── Unit: Konfiguration & Maskierung ─────────────────────────")
    cfg = load_config()
    check("Proxy-Default leer", cfg.discord_proxy == "")
    check("Neustart bei IP-Sperre ist Standard", cfg.restart_on_ip_ban is True)
    check("Proben-Intervall 30 s", cfg.ban_probe_interval_seconds == 30.0)
    check("Beobachtungsfenster 600 s", cfg.ban_watch_seconds == 600.0)
    check("neue Optionen erscheinen im Start-Log",
          {"ban_watch_seconds", "restart_on_ip_ban", "discord_proxy"} <= set(cfg.masked()))

    os.environ["DISCORD_PROXY"] = "http://user:geheim@proxy.example.com:3128"
    os.environ["DISCORD_PROXY_PASSWORD"] = "geheim"
    try:
        cfg = load_config()
        masked = cfg.masked()
        check("Proxy-Passwort in der URL maskiert", "geheim" not in masked["discord_proxy"],
              masked["discord_proxy"])
        check("Proxy-Passwort-Feld maskiert", "geheim" not in str(masked["discord_proxy_password"]))
        check("mask_proxy_url behält Host",
              "proxy.example.com:3128" in mask_proxy_url(cfg.discord_proxy))

        os.environ["DISCORD_PROXY"] = "socks5://proxy.example.com:1080"
        try:
            load_config()
            check("SOCKS-Proxy wird abgelehnt (aiohttp kann das nicht)", False, "keine ConfigError")
        except ConfigError:
            check("SOCKS-Proxy wird abgelehnt (aiohttp kann das nicht)", True)

        os.environ["DISCORD_PROXY"] = "http://proxy.example.com:3128"
        os.environ["BAN_PROBE_INTERVAL_SECONDS"] = "1"
        try:
            load_config()
            check("zu kleines Proben-Intervall wird abgelehnt", False, "keine ConfigError")
        except ConfigError:
            check("zu kleines Proben-Intervall wird abgelehnt", True)
    finally:
        for key in ("DISCORD_PROXY", "DISCORD_PROXY_PASSWORD", "BAN_PROBE_INTERVAL_SECONDS"):
            os.environ.pop(key, None)


def test_state_units() -> None:
    print("\n── Unit: Zustand & Health-Felder ────────────────────────────")
    cfg = load_config()
    store = SessionStore(path=None, persist=False)
    client = RelayClient(cfg, store, state=None)
    state = AppState(client=client, config=cfg, store=store)

    check("ohne Diagnose: keine Angabe", state.net_dict() == {"verdict": "not_checked"})
    check("Alter ohne Diagnose = unendlich", state.net_report_age() == float("inf"))
    state.set_net_report(blocked_report())
    data = state.net_dict()
    check("Report landet im Zustand", data["verdict"] == VERDICT_IP_BLOCKED)
    check("ausgehende IP ablesbar", state.egress_ip == "203.0.113.7")
    check("discord_reachable=false", data["discord_reachable"] is False)
    check("net_ready-Event gesetzt", state.net_ready().is_set())
    check("Cloudflare-Ray im Report", data["discord_probe"]["cloudflare_ray"] == "deadbeef-FRA")


# ─────────────────────────────────────────────────────────────────────────────


async def arun() -> int:
    print("🔁 Test der Login-Wiederherstellung (Cloudflare 429 / Error 1015)")
    test_detection_units()
    test_backoff_units()
    test_config_units()
    test_state_units()
    test_ledger_units()
    test_egress_tracking_units()
    await test_ip_ban_lifts()
    await test_ip_ban_persists()
    await test_ip_ban_persists_no_restart()
    await test_unlimited_watch()
    await test_fast_restart_on_long_retry_after()
    await test_restart_cycle_limit()
    await test_token_level_rate_limit()
    await test_real_ratelimit_is_capped()
    await test_backoff_escalates()
    await test_amain_exit_code()

    print("\n" + "═" * 64)
    print(f"  ✅ {len(PASSED)} bestanden     ❌ {len(FAILED)} fehlgeschlagen")
    print("═" * 64)
    if FAILED:
        print("\nFehlgeschlagen:")
        for item in FAILED:
            print(f"  · {item}")
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(arun())
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
