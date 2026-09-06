"""
Neustart-Buchführung: Wie oft hat sich dieser Dienst wegen einer gesperrten
Ausgangs-IP schon selbst neu gestartet — und hat das je eine andere IP gebracht?

Warum das nötig ist
-------------------
Bei einer von Cloudflare gesperrten Ausgangs-IP beendet sich der Bot mit
Exit-Code 3, damit die Plattform einen frischen Container startet (Chance auf
eine andere Adresse aus dem geteilten Pool). Ohne Gedächtnis würde er das
**endlos** tun — auch wenn der komplette Pool verbrannt ist und jeder neue
Container dieselbe gesperrte Adresse zieht. Das verwirrt im Log, verbrennt
Instanz-Stunden und bringt nichts.

Dieses Modul merkt sich deshalb in ``DATA_DIR/restart_ledger.json``:

* wie viele Neustart-Zyklen es seit dem letzten erfolgreichen Login gab,
* welche Ausgangs-IPs dabei gesehen wurden (→ beantwortet die Frage
  „würfelt ein Neustart auf dieser Plattform überhaupt eine neue IP?"),
* wie oft nach einem Neustart **dieselbe** IP wiederkam.

Nach ``RESTART_MAX_CYCLES`` Zyklen (Default 5) hört der Bot auf zu würfeln,
probt nur noch in Ruhe weiter und sagt im Log und in ``GET /api/diagnostics``
klar: *Diese Plattform kommt nicht durch — eigene Ausgangs-IP nötig.*

Grenzen
-------
Auf Plattformen mit **flüchtigem** Dateisystem (Render Free) überlebt die Datei
einen Container-Neustart nicht — der Zähler beginnt dann jedes Mal bei 0. Der
Bot erkennt das nicht sicher (ein fehlendes Buch sieht aus wie ein erster
Start), weist aber in Log und Diagnose darauf hin. Auf einem VPS, in Docker mit
Volume oder mit einer Render-Disk funktioniert die Buchführung vollständig.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .util import iso, now_utc

log = logging.getLogger("relay.restarts")

__all__ = ("LEDGER_FILENAME", "RestartLedger")

#: Dateiname im ``DATA_DIR``.
LEDGER_FILENAME = "restart_ledger.json"
#: So viele Einträge behält die Historie (jüngste zuletzt).
HISTORY_LIMIT = 12
#: Älter als das ⇒ der Zählerstand ist bedeutungslos und wird verworfen.
DEFAULT_MAX_AGE_SECONDS = 24 * 3600.0


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    with contextlib.suppress(Exception):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


@dataclass(slots=True)
class RestartLedger:
    """
    Zählt bewusste Neustarts wegen IP-Sperre über Prozessgrenzen hinweg.

    ``path=None`` ⇒ rein im Speicher (Tests, ``PERSIST_SESSIONS=false``).
    Kein Aufruf wirft: Eine kaputte oder nicht schreibbare Datei darf den Bot
    niemals aufhalten — der Zustand steht dann eben in ``load_error`` /
    ``save_error``.
    """

    path: Optional[str] = None
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
    cycles: int = 0
    first_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_ip: Optional[str] = None
    ips: List[str] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    same_ip_streak: int = 0
    found_at_start: bool = False
    stale_at_start: bool = False
    load_error: Optional[str] = None
    save_error: Optional[str] = None
    saved_ok: bool = False

    # ── Erzeugen / Laden / Speichern ─────────────────────────────────────────
    @classmethod
    def open(cls, data_dir: Optional[str], *, filename: str = LEDGER_FILENAME,
             max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> "RestartLedger":
        """Öffnet (und lädt) das Buch aus ``data_dir`` — oder im Speicher, wenn leer."""
        path = os.path.join(data_dir, filename) if data_dir else None
        ledger = cls(path=path, max_age_seconds=max_age_seconds)
        ledger.load()
        return ledger

    def load(self) -> bool:
        """Liest die Datei. ``True``, wenn ein gültiger, nicht verjährter Stand geladen wurde."""
        if not self.path:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001 — kaputte Datei = wie keine Datei
            self.load_error = f"{type(exc).__name__}: {exc}"[:160]
            return False
        if not isinstance(data, dict):
            self.load_error = "kein JSON-Objekt"
            return False

        self.found_at_start = True
        updated = _parse_iso(data.get("updated_at"))
        age = (now_utc() - updated).total_seconds() if updated else None
        if age is None or age > self.max_age_seconds or age < -3600:
            # Uralter Stand (oder Uhr verstellt): verwerfen, aber merken, dass
            # das Dateisystem offenbar persistent ist.
            self.stale_at_start = True
            self._reset_fields()
            return False

        with contextlib.suppress(Exception):
            self.cycles = max(0, int(data.get("cycles") or 0))
        self.first_at = data.get("first_at") or None
        self.updated_at = data.get("updated_at") or None
        self.last_ip = data.get("last_ip") or None
        ips = data.get("ips") or []
        self.ips = [str(ip) for ip in ips if ip][:64] if isinstance(ips, list) else []
        history = data.get("history") or []
        self.history = [h for h in history if isinstance(h, dict)][-HISTORY_LIMIT:] \
            if isinstance(history, list) else []
        with contextlib.suppress(Exception):
            self.same_ip_streak = max(0, int(data.get("same_ip_streak") or 0))
        return True

    def save(self) -> bool:
        """Schreibt atomar (Temp-Datei + ``os.replace``). Nie eine Exception."""
        if not self.path:
            return False
        self.updated_at = iso(now_utc())
        payload = {
            "cycles": self.cycles,
            "first_at": self.first_at,
            "updated_at": self.updated_at,
            "last_ip": self.last_ip,
            "ips": self.ips,
            "same_ip_streak": self.same_ip_streak,
            "history": self.history[-HISTORY_LIMIT:],
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp = ""
        try:
            os.makedirs(directory, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=directory, prefix=".restart-", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self.saved_ok = True
            self.save_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.saved_ok = False
            self.save_error = f"{type(exc).__name__}: {exc}"[:160]
            log.debug("Neustart-Buch %s nicht schreibbar: %s", self.path, self.save_error)
            with contextlib.suppress(Exception):
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            return False

    def _reset_fields(self) -> None:
        self.cycles = 0
        self.first_at = None
        self.updated_at = None
        self.last_ip = None
        self.ips = []
        self.history = []
        self.same_ip_streak = 0

    def clear(self, *, reason: str = "") -> None:
        """Erfolgreicher Login ⇒ Buch zu. Datei wird entfernt, Zähler auf 0."""
        had = self.cycles
        self._reset_fields()
        if self.path:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.save_error = f"{type(exc).__name__}: {exc}"[:160]
        if had:
            log.info("Neustart-Zähler zurückgesetzt (%d Zyklen)%s.", had,
                     f" — {reason}" if reason else "")

    # ── Buchungen ────────────────────────────────────────────────────────────
    def note_boot_ip(self, egress_ip: Optional[str]) -> Optional[bool]:
        """
        Beim Start: aktuelle Ausgangs-IP mit der letzten vor dem Neustart vergleichen.

        Liefert ``True`` (IP hat gewechselt), ``False`` (dieselbe IP wie vor dem
        Neustart — Würfeln bringt hier offenbar nichts) oder ``None`` (keine
        Vergleichsbasis).
        """
        if not egress_ip:
            return None
        previous = self.last_ip
        changed: Optional[bool] = None
        if previous and self.cycles > 0:
            changed = previous != egress_ip
            self.same_ip_streak = 0 if changed else self.same_ip_streak + 1
        if egress_ip not in self.ips:
            self.ips.append(egress_ip)
            self.ips = self.ips[-64:]
        if self.cycles > 0:
            self.save()
        return changed

    def record_restart(self, *, egress_ip: Optional[str], cause: str = "") -> int:
        """Bucht einen bewussten Neustart und liefert die neue Zyklusnummer."""
        now = iso(now_utc())
        self.cycles += 1
        self.first_at = self.first_at or now
        if egress_ip:
            self.last_ip = egress_ip
            if egress_ip not in self.ips:
                self.ips.append(egress_ip)
                self.ips = self.ips[-64:]
        self.history.append({
            "cycle": self.cycles,
            "at": now,
            "ip": egress_ip,
            "cause": (cause or "")[:80],
        })
        self.history = self.history[-HISTORY_LIMIT:]
        self.save()
        return self.cycles

    # ── Auswertung ───────────────────────────────────────────────────────────
    def exhausted(self, max_cycles: int) -> bool:
        """``True`` ⇒ Limit erreicht: nicht mehr neu starten, nur noch proben."""
        return max_cycles > 0 and self.cycles >= max_cycles

    def remaining(self, max_cycles: int) -> Optional[int]:
        if max_cycles <= 0:
            return None
        return max(0, max_cycles - self.cycles)

    @property
    def persistent(self) -> Optional[bool]:
        """
        ``True`` = Datei hat nachweislich einen Prozess überlebt,
        ``False`` = Schreiben schlug fehl / kein Pfad,
        ``None`` = unbekannt (erster Start oder flüchtiges Dateisystem).
        """
        if not self.path or self.save_error:
            return False
        if self.found_at_start:
            return True
        return None

    def to_dict(self, max_cycles: int = 0) -> Dict[str, Any]:
        return {
            "cycles": self.cycles,
            "max_cycles": max_cycles if max_cycles > 0 else None,
            "remaining": self.remaining(max_cycles),
            "exhausted": self.exhausted(max_cycles),
            "first_at": self.first_at,
            "updated_at": self.updated_at,
            "last_ip_before_restart": self.last_ip,
            "ips_seen": list(self.ips),
            "distinct_ips": len(self.ips),
            "same_ip_streak": self.same_ip_streak,
            "history": list(self.history[-HISTORY_LIMIT:]),
            "ledger_file": self.path,
            "ledger_found_at_start": self.found_at_start,
            "ledger_stale_at_start": self.stale_at_start,
            "ledger_persistent": self.persistent,
            "ledger_error": self.load_error or self.save_error,
        }

    def summary(self, max_cycles: int = 0) -> str:
        """Eine Zeile fürs Log."""
        limit = f"/{max_cycles}" if max_cycles > 0 else ""
        ips = ", ".join(self.ips[-6:]) or "–"
        return (
            f"Neustart-Zyklus {self.cycles}{limit} · bisher gesehene Ausgangs-IPs: {ips}"
            + (f" · {self.same_ip_streak}× dieselbe IP nach Neustart" if self.same_ip_streak else "")
        )
