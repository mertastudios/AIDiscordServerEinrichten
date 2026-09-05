# AIDiscordServerEinrichten

> **Ein Discord-Bot, der Arena AI die Schlüssel zu deinem Server gibt.**
> Du führst `/connect` aus, kopierst Link + Token in Arena AI — und die KI richtet
> deinen kompletten Discord-Server ein: Rollen, Kanäle, Kategorien, Berechtigungen,
> AutoMod, Welcome-Screen, Onboarding, Nachrichten. Alles per natürlicher Sprache.

[![CI](https://github.com/mertastudios/AIDiscordServerEinrichten/actions/workflows/ci.yml/badge.svg)](https://github.com/mertastudios/AIDiscordServerEinrichten/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.7.1-5865F2?logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Render](https://img.shields.io/badge/Deploy-auf%20Render-46E3B7?logo=render&logoColor=black)](#3-auf-render-deployen)

---

## Inhaltsverzeichnis

1. [Wie es funktioniert](#wie-es-funktioniert)
2. [Schnellstart (≈ 15 Minuten)](#schnellstart)
3. [Der `/connect`-Command](#der-connect-command)
4. [Sicherheitsmodell](#sicherheitsmodell)
5. [Die REST-API](#die-rest-api)
6. [Die Console](#die-console)
7. [UptimeRobot — Bot dauerhaft online halten](#uptimerobot)
8. [Konfiguration](#konfiguration)
9. [Lokal entwickeln](#lokal-entwickeln)
10. [Tests](#tests)
11. [Fehlerbehebung](#fehlerbehebung)
12. [Projektstruktur](#projektstruktur)

---

## Wie es funktioniert

Das Problem: Arena AI kann nicht direkt mit Discord sprechen — und dein Bot-Token
solltest du niemals an einen Dritten weitergeben.

Die Lösung: **ein Relay**. Der Bot läuft auf Render, ist mit Discord verbunden und
öffnet zusätzlich eine REST-API. `/connect` erzeugt ein kurzlebiges, auf deinen
Server beschränktes Sitzungs-Token. Genau dieses Token bekommt Arena AI — nicht
dein Bot-Token.

```
┌──────────────┐   /connect    ┌───────────────────────────────────────┐
│    Discord   │ ────────────▶ │  AIDiscordServerEinrichten (Render)   │
│    Server    │               │                                       │
│              │ ◀──────────── │  ┌─────────────┐   ┌───────────────┐  │
└──────────────┘  ephemeral:   │  │ discord.py  │   │  aiohttp API  │  │
                  Link+Token   │  │   Client    │◀──│  113 Endpoints│  │
                               │  └──────┬──────┘   └───────▲───────┘  │
                               │         │                  │          │
                               │    gleicher Prozess, gleicher Loop    │
                               └─────────┼──────────────────┼──────────┘
                                         │                  │
                            Bot-Token    │                  │  Sitzungs-Token
                          (bleibt hier!) │                  │  (adse_…)
                                         ▼                  │
                                  ┌────────────┐    ┌───────┴────────┐
                                  │  Discord   │    │    Arena AI    │
                                  │  Gateway   │    │  (curl / REST) │
                                  └────────────┘    └────────────────┘
```

**Entscheidend:** Der Bot und die API laufen in *einem* Prozess auf *einem*
Event-Loop. Kein zweiter Dienst, keine Datenbank, keine Webhooks — ein Container.

---

## Schnellstart

### 1. Discord-Application anlegen

1. <https://discord.com/developers/applications> → **New Application**
2. Name z. B. `AIDiscordServerEinrichten` → **Create**
3. Links **Bot** → **Reset Token** → Token kopieren (wird gleich gebraucht)
4. Auf derselben Seite **Privileged Gateway Intents**:
   - ✅ `SERVER MEMBERS INTENT`
   - ✅ `MESSAGE CONTENT INTENT`

   > Vergessen? Kein Abbruchgrund. Der Bot startet automatisch ohne sie weiter
   > und bleibt online — nur die Mitgliederliste ist dann eventuell lückenhaft.
   > Im Render-Log steht in dem Fall eine klare Anleitung.

5. Links **OAuth2** → **OAuth2 URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: **Administrator**
   - Die erzeugte URL öffnet den Bot auf deinem Server

   > Der Bot **muss** Administrator sein. `/connect` prüft das und verweigert
   > sich sonst mit einer Schritt-für-Schritt-Anleitung.

### 2. Auf deinem Server

1. Bot über die URL aus Schritt 1.5 einladen
2. **Servereinstellungen → Rollen**: die Bot-Rolle auf *Administrator* und
   **ganz nach oben** schieben

   > Ganz nach oben ist wichtig: Discord lässt einen Bot keine Rolle verwalten,
   > die auf gleicher oder höherer Position liegt. Sonst kann die KI keine
   > Admin-Rolle anlegen.

### 3. Auf Render deployen

**Variante A — Blueprint (empfohlen)**

1. Dieses Repo auf GitHub pushen (bzw. forken)
2. Render Dashboard → **New +** → **Blueprint**
3. Repo auswählen — Render findet [`render.yaml`](render.yaml) automatisch
4. Im Wizard `DISCORD_BOT_TOKEN` eintragen → **Apply**

**Variante B — manuell**

1. **New +** → **Web Service** → Repo verbinden
2. Runtime: **Docker** (das [`Dockerfile`](Dockerfile) wird automatisch erkannt)
3. Environment: `DISCORD_BOT_TOKEN` = dein Token
4. **Health Check Path**: `/api/health`
5. Plan: **Free** reicht
6. **Create Web Service**

**Wichtig:** `numInstances: 1` (in `render.yaml` bereits gesetzt). Die
Sitzungs-Tokens leben im Speicher der Instanz — bei mehreren Instanzen würde ein
Token zufällig „unbekannt" sein.

Nach dem Deploy steht im Render-Log:

```
Öffentliche Basis-URL: https://aidiscordservereinrichten.onrender.com
  · Healthcheck : https://…/api/health   ← hierhin UptimeRobot zeigen
  · Console     : https://…/console
```

### 4. Loslegen

Auf Discord `/connect` ausführen → Prompt kopieren → bei Arena AI einfügen →
sagen, was die KI bauen soll. Fertig.

---

## Der `/connect`-Command

Der eine Command, um den es geht.

```
/connect [dauer] [modus]
```

| Option  | Werte | Standard |
| ------- | ----- | -------- |
| `dauer` | 1 h · 6 h · **24 h** · 3 T · 7 T · 30 T · unbegrenzt | 24 Stunden |
| `modus` | 🔥 Voller Zugriff · 🛡️ Moderation · 🛠️ Einrichten · 👁️ Nur lesen | Voller Zugriff |

**Was du bekommst — und wer es sieht:**

Die Antwort ist **ephemeral**. Nur du siehst sie; für alle anderen im Channel ist
sie unsichtbar, und sie taucht in keinem Log auf.

- **Nachricht 1** — Zugangsdaten als Embed: Server, Modus, Gültigkeit,
  API-Basis-URL, Token (zusätzlich als Spoiler markiert), Console-Link
- **Drei Buttons** — `🖥️ Console öffnen` · `🔄 Neues Token` · `⛔ Alle widerrufen`
  (persistent: funktionieren auch nach einem Render-Deploy noch)
- **Nachricht 2** — der fertige Prompt im Codeblock zum Kopieren, plus
  `arena-prompt.md` als Anhang mit der ausführlichen Fassung samt API-Referenz

**Zwei Command-Brüder für den Notfall:**

| Command | Zweck |
| ------- | ----- |
| `/status` | Rechte-Check: Ist der Bot Administrator? Was fehlt? Wie viele Tokens sind aktiv? |
| `/revoke` | Entzieht **sofort** allen aktiven KI-Zugriffen auf diesem Server die Gültigkeit |

**Beide Prüfungen, beide Richtungen.** `/connect` verweigert sich, wenn

- **du** kein Administrator bist → `⛔ Administrator-Rechte erforderlich`
- **der Bot** kein Administrator ist → Anleitung, wie du die Rolle hochstufst,
  inklusive einem frischen Invite-Link mit gesetztem Administrator-Recht

Jeder abgelehnte Versuch wird im Render-Log protokolliert.

---

## Sicherheitsmodell

| Maßnahme | Umsetzung |
| -------- | --------- |
| **Bot-Token bleibt privat** | Arena AI sieht ausschließlich das Sitzungs-Token. Der Bot-Token verlässt den Container nie. |
| **Token werden nie im Klartext gespeichert** | Nur SHA-256-Hashes. Selbst ein Diebstahl von `data/sessions.json` liefert keine nutzbaren Tokens. |
| **Token sind server-gebunden** | Ein Token für Server A funktioniert auf Server B nicht (`409 GUILD_UNAVAILABLE`). |
| **Token laufen ab** | Standard 24 h, konfigurierbar. `ttl_hours=0` für unbegrenzt ist bewusst möglich, aber nicht empfohlen. |
| **Sofortiger Widerruf** | `/revoke`, der `⛔`-Button, die Console oder `DELETE /api/v1/session`. |
| **Sitzungs-Limit** | Maximal 5 aktive Tokens pro Server; das älteste wird automatisch widerrufen. |
| **Berechtigungs-Stufen** | `read` · `write` · `manage` · `danger` — jeder Endpoint hat einen Minimal-Scope. |
| **Rate-Limiting** | 240 Anfragen pro Token und Minute, sliding window. Bei `429` kommt `retry_after`. |
| **Ephemeral + Spoiler** | Die Discord-Antwort sieht nur der Aufrufer; das Token ist zusätzlich gespoilert. |
| **Kein Versions-Leak** | `Server: AIDiscordServerEinrichten` statt `Python/3.x aiohttp/y.z`. |
| **Maskierte Logs** | `config.masked()` kürzt den Token auf `inval…test (28 Zeichen)`. Log-Screenshots sind damit unkritisch. |
| **CORS einschränkbar** | `ALLOWED_ORIGIN` statt `*`, wenn du es enger willst. |

> **Was das Modell bewusst *nicht* leistet:** Im Modus `danger` hat die KI
> Administrator-Macht über deinen Server — genau das ist der Zweck. Sie kann
> Kanäle löschen, Mitglieder bannen und Einstellungen ändern. Nutze `🛠️
> Einrichten` oder `👁️ Nur lesen`, wenn du erst zuschauen willst, und entzieh
> den Zugriff mit `/revoke`, sobald du fertig bist.

---

## Die REST-API

**113 Endpoints.** Alles unter `/api/v1/*`, Authentifizierung per
`Authorization: Bearer <TOKEN>`.

Die API ist **selbstbeschreibend** — das ist der wichtigste Designentscheid:

```bash
curl -s "https://DEINE-URL/api/v1/capabilities" \
     -H "Authorization: Bearer $TOKEN"
```

liefert jeden Endpoint mit Methode, Pfad, Beschreibung, benötigtem Scope,
Query-Parametern, Body-Feldern und Beispielen — plus Konventionen,
curl-Templates, Rate-Limits und die Liste der Setup-Vorlagen. Arena AI muss
deshalb nichts vorher wissen und nichts auswendig lernen.

### Konventionen

- Erfolg: `{"ok": true, "data": …}`
- Fehler: `{"ok": false, "error": {"code", "message", "hint", "status", "request_id"}}`
  — `hint` sagt der KI konkret, was sie als Nächstes tun soll
- **IDs sind immer Strings.** Snowflakes sind 19-stellig und verlieren in
  JavaScript/JSON als Zahl Präzision.
- Zeitstempel in ISO-8601 UTC
- Permissions als Namen (`"manage_channels"`), Farben als `"#RRGGBB"`
- Fast jeder schreibende Endpoint akzeptiert `"reason"` fürs Audit-Log

### Die wichtigsten Endpoints

| Endpoint | Zweck |
| -------- | ----- |
| `GET /api/health` | Öffentlich. Für UptimeRobot und den Render Health Check. |
| `GET /api/v1/capabilities` | Komplette API-Beschreibung. **Immer zuerst aufrufen.** |
| `GET /api/v1/guild/snapshot` | Der gesamte Ist-Zustand in **einem** Aufruf. |
| `GET /api/v1/me` | Wer bin ich, wo bin ich, was darf ich? |
| `GET /api/v1/channels/tree` | Kanalstruktur als Baum. |
| `POST /api/v1/setup` | **Komplettes Server-Setup in einem Aufruf.** |
| `POST /api/v1/setup/preview` | Denselben Plan validieren, ohne etwas zu ändern. |
| `GET /api/v1/setup/templates` | Fünf fertige deutsche Server-Vorlagen. |
| `GET /api/v1/prompt` | Den Arena-AI-Prompt (`short` / `long` / `system`). |

Dazu: Rollen, Kanäle (Text, Voice, Stage, Forum, Media, Kategorien, Threads,
Positionen, Overwrites), Mitglieder (Timeout, Kick, Ban, Prune, Nicknames,
Voice-Moves, Massen-Timeouts), Nachrichten (Senden, Bearbeiten, Bulk-Delete,
Purge, Pins, Reaktionen, Embeds, Polls, Komponenten), Server-Einstellungen
(Community, Verification Level, Welcome Screen, Onboarding, Widget, MFA, Vanity),
AutoMod, Emojis & Sticker, Webhooks, Invites, Scheduled Events, Audit-Logs,
Voice-States, Action-Log.

### `POST /api/v1/setup` — der Kraftmeier

Ein einziger Aufruf, der einen kompletten Server aufbaut:

```json
{
  "template": "gaming-community",
  "roles":     [{"key": "mod", "name": "🛡️ Mod", "preset": "moderator", "position": 16}],
  "categories": [{"key": "info", "name": "📌 INFO", "position": 0,
                  "channels": [{"key": "regeln", "name": "regeln", "type": "text",
                                "topic": "Bitte lesen",
                                "overwrites": [{"role_key": "mod", "allow": ["send_messages"]}]}]}],
  "messages":  [{"channel": "regeln",
                 "embeds": [{"title": "📜 Regeln", "color": "#5865F2", "description": "…"}]}],
  "settings":  {"system_channel": "welcome", "rules_channel": "regeln",
                "afk_channel": "AFK", "afk_timeout": 900, "community": true},
  "automod":   [{"name": "Werbung", "trigger_type": "keyword",
                 "regex_patterns": ["discord\\.(gg|com)/\\w+"],
                 "actions": [{"type": "block_message"}]}],
  "invites":   [{"channel": "welcome", "max_age": 0}],
  "reason":    "Server-Setup durch Arena AI"
}
```

Der Executor arbeitet in der richtigen Reihenfolge (Rollen **vor** Kanälen,
damit Overwrites die neuen Rollen schon referenzieren können; Server-Einstellungen
**nach** Kanälen, damit `afk_channel` existiert), bündelt Rollenpositionen in
einen einzigen API-Call, übersetzt `key`-Referenzen über eine interne Tabelle und
meldet am Ende **jeden einzelnen Schritt** mit OK/Fehler:

```json
{"ok": true, "steps_total": 41, "steps_ok": 41, "steps_failed": 0,
 "report": […], "keys": {"mod": "1234…", "regeln": "5678…"},
 "created": {"roles": […], "channels": […]}, "next_steps": […]}
```

**Fünf fertige Vorlagen** — alle deutsch, alle validiert, alle direkt ausführbar:

| Vorlage | Inhalt |
| ------- | ------ |
| `gaming-community` | Voice-Lounges, LFG, Turnier-Kanäle, Team-Bereich |
| `creator-streamer` | Stream-Ankündigungen, Subscriber-Bereich, Clip-Forum |
| `lerngruppe` | Fächerkanäle, Ressourcen-Forum, Lernräume, Tutoren-Rollen |
| `business-support` | Support-Kategorien, Ticket-Forum, Wissensdatenbank |
| `freunde` | Klein und gemütlich — ohne Overhead |

`POST /api/v1/setup/preview` validiert denselben Plan **ohne** etwas zu ändern —
ideal, bevor die KI etwas Destruktives tut.

---

## Die Console

Unter `https://DEINE-URL/console` läuft ein dunkles Dashboard ohne Build-Schritt
und ohne eigene Geheimnisse — eine einzige, in sich geschlossene HTML-Datei, die
alles über die REST-API holt.

- **Token-Eingabe** — automatisch übernommen aus `?t=…` (so kommt der Button im
  Discord-Embed daher)
- **Prompt in drei Varianten** — `short` (passt in Discord), `long` (mit
  API-Referenz), `system` — jeweils mit *Kopieren* und *Als `.md` speichern*
- **Verbindungsstatus** — Server, Modus, Gültigkeit, Aufruf-Statistik
- **Server-Snapshot** — der komplette Ist-Zustand zum Aufklappen
- **Live-Action-Log** — pollt `/api/v1/actions` und zeigt jeden API-Aufruf der KI
- **API-Tester** — Endpoints direkt im Browser ausprobieren
- **Token widerrufen / erneuern** — zwei Buttons, ohne zurück zu Discord

Mit `CONSOLE_ENABLED=false` lässt sie sich komplett abschalten; `/console`
antwortet dann mit einem JSON-404.

---

## UptimeRobot

Auf Render Free schläft ein Web Service nach 15 Minuten ohne Anfrage ein — und
mit ihm der Bot. Ein externer Ping hält ihn wach.

**Ziel-URL:**

```
https://DEINE-RENDER-URL.onrender.com/api/health
```

**Einrichten:**

1. <https://uptimerobot.com> → kostenloser Account
2. **Add New Monitor**
   - Monitor Type: **HTTP(s)**
   - Friendly Name: `AIDiscordServerEinrichten`
   - URL: die Health-URL von oben
   - Monitoring Interval: **5 minutes** (Minimum im Free-Plan)
3. **Create Monitor**

`/api/health` ist **öffentlich** (kein Token nötig), antwortet in wenigen
Millisekunden und liefert:

```json
{"ok": true, "data": {
  "status": "healthy", "bot": "connected", "guilds": 3,
  "gateway_latency_ms": 42.1, "uptime_seconds": 8123.4,
  "active_sessions": 1, "requests_served": 118,
  "version": "1.0.0", "timestamp": "2026-09-05T18:33:15Z"
}}
```

> **Bewusst so gebaut:** `status` ist schon `"healthy"`, während der Bot noch
> mit Discord verbindet. Der Healthcheck darf nie einen 500er liefern — sonst
> meldet Render das Deploy als fehlgeschlagen und UptimeRobot schlägt grundlos
> Alarm. Der Discord-Status steht separat im Feld `bot`.

**Alternativen,** falls du UptimeRobot nicht magst: derselbe Endpoint funktioniert
mit Cron-job.org, Healthchecks.io, besserping oder einem GitHub-Action-Cron.
Trage die URL zusätzlich in Render unter *Settings → Health Check Path* ein.

---

## Konfiguration

Alles über Umgebungsvariablen. Nur `DISCORD_BOT_TOKEN` ist Pflicht.
Eine kommentierte Vorlage liegt in [`.env.example`](.env.example).

| Variable | Default | Bedeutung |
| -------- | ------- | --------- |
| `DISCORD_BOT_TOKEN` | — | **Pflicht.** Bot-Token aus dem Developer Portal. |
| `PUBLIC_URL` | *(leer)* | Erzwingt die öffentliche Basis-URL. Auf Render nicht nötig. |
| `HOST` | `0.0.0.0` | Bind-Adresse. Nicht ändern — Render braucht `0.0.0.0`. |
| `PORT` | `8080` | Wird von Render automatisch gesetzt. |
| `COMMAND_NAME` | `connect` | Name des Slash-Commands. |
| `BOT_NAME` | `AIDiscordServerEinrichten` | Anzeigename in Antworten. |
| `ACTIVITY_TEXT` | `/connect · KI-Steuerung …` | Presence-Text des Bots. |
| `PRESENCE_STATUS` | `online` | `online` / `idle` / `dnd` / `invisible`. |
| `SESSION_TTL_HOURS` | `24` | Standard-Gültigkeit eines Tokens. `0` = unbegrenzt. |
| `MAX_SESSIONS_PER_GUILD` | `5` | Aktive Tokens pro Server, bevor das älteste widerrufen wird. |
| `PERSIST_SESSIONS` | `true` | Tokens in `DATA_DIR/sessions.json` sichern. |
| `DATA_DIR` | `data` | Datenverzeichnis (auf Render Free flüchtig). |
| `API_RATE_LIMIT` | `240` | Anfragen pro Token … |
| `API_RATE_WINDOW` | `60` | … pro diesem Fenster in Sekunden. |
| `ALLOWED_ORIGIN` | `*` | CORS. Kommagetrennte Liste für mehr Enge. |
| `CONSOLE_ENABLED` | `true` | Dashboard unter `/console` an/aus. |
| `MAX_BODY_BYTES` | `8388608` | Maximale Request-Größe (Base64-Icons!). |
| `MAX_MESSAGE_HISTORY` | `100` | Obergrenze beim Nachrichten-Lesen. |
| `MAX_MEMBER_FETCH` | `1000` | Obergrenze beim Mitglieder-Abruf. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `PRIVILEGED_INTENTS` | `true` | Members- und Message-Content-Intent anfordern. |
| `SETTLE_SCALE` | `1.0` | Faktor für Cache-Wartezeiten. `0` nur für Tests. |

---

## Lokal entwickeln

```bash
git clone https://github.com/mertastudios/AIDiscordServerEinrichten.git
cd AIDiscordServerEinrichten

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Token eintragen
export $(grep -v '^#' .env | xargs)

python -m bot
```

Der Bot lauscht dann auf `http://localhost:8080`:

- `http://localhost:8080/console` — Dashboard
- `http://localhost:8080/api/health` — Healthcheck
- `http://localhost:8080/api/v1/capabilities` — API-Beschreibung

**Lokal vor Arena AI sichtbar machen:** Arena AI erreicht `localhost` nicht.
Nutze einen Tunnel und setze `PUBLIC_URL` darauf:

```bash
cloudflared tunnel --url http://localhost:8080
export PUBLIC_URL=https://irgendwas.trycloudflare.com
```

Slash-Commands brauchen nach der ersten Registrierung bis zu einer Stunde, bis
sie in allen Clients erscheinen (`/connect` auf bestehenden Servern meist sofort).

---

## Tests

```bash
python scripts/smoke_test.py          # ~200 Prüfungen, ohne Discord-Verbindung
python scripts/smoke_test.py -v       # jede einzelne Prüfung anzeigen
python scripts/smoke_test.py auth read write   # nur ausgewählte Gruppen
```

Der Smoke-Test startet den **echten** Web-Server gegen einen simulierten
Discord-Client ([`scripts/_fake_discord.py`](scripts/_fake_discord.py)) und prüft:

| Gruppe | Inhalt |
| ------ | ------ |
| `public` | Healthcheck, Landingpage, Console, robots.txt, Capabilities ohne Token |
| `auth` | 401-Pfade, Token-Format, Hash-Speicherung, `/me`, Prompt-Varianten |
| `read` | Guild, Kanalbaum, Rollen, Mitglieder, Suche, Permissions, Snapshot, Audit-Log |
| `scopes` | `read` darf nicht schreiben, `write` darf nicht moderieren |
| `errors` | JSON statt HTML bei 404/405, ungültiges JSON, fremder Server |
| `setup` | Alle fünf Vorlagen validieren; fehlerhafte Pläne werden abgewiesen |
| `lifecycle` | Token erneuern, widerrufen, Ablauf |
| `ratelimit` | `429` mit `retry_after` und `Retry-After`-Header, Budget pro Token |
| `write` | **Alle fünf Vorlagen werden wirklich ausgeführt** — Rollen, Kategorien, Kanäle, Nachrichten, Invites, AutoMod |
| `limit` | Sitzungs-Limit pro Server, Auto-Revoke des ältesten Tokens |
| `security` | Kein Token-Leak in Antworten, Logs oder der Console; CORS; Header |

Die Test-Double sind **echte Unterklassen** von `discord.Role`,
`discord.TextChannel`, `discord.Member` usw. — damit die `isinstance`-Prüfungen
der API tatsächlich greifen und der Test nicht an Attrappen vorbeiläuft.

Der GitHub-Actions-Workflow liegt in [`ci/ci.yml`](ci/ci.yml) und prüft bei jedem
Push und Pull Request Python **3.11 und 3.12**, baut zusätzlich das Docker-Image
und verlangt `/api/health` im laufenden Container.

> **Einmalig aktivieren** — die Datei liegt in `ci/`, weil der Deploy-Bot keine
> `workflows`-Berechtigung hat und GitHub den Push sonst komplett ablehnt:
>
> ```bash
> mkdir -p .github/workflows && git mv ci/ci.yml .github/workflows/ci.yml
> git commit -m "ci: GitHub-Actions-Workflow aktivieren" && git push
> ```
>
> Details in [`ci/README.md`](ci/README.md). Das Badge oben wird erst sichtbar,
> sobald die Datei unter `.github/workflows/` liegt.

---

## Fehlerbehebung

**`/connect` erscheint nicht im Discord-Menü**
Slash-Commands brauchen nach der Registrierung bis zu einer Stunde. Prüfe im
Render-Log `Slash-Commands global registriert`. Falls der Bot nicht auf dem
Server ist: neu einladen (der Invite-Link steht in `/status`).

**`⛔ Der Bot braucht Administrator-Rechte`**
Servereinstellungen → Rollen → Bot-Rolle → *Administrator* aktivieren **und die
Rolle ganz nach oben** schieben. Danach `/connect` erneut ausführen.

**`409 GUILD_UNAVAILABLE`**
Der Bot ist noch nicht mit dem Gateway synchronisiert (typisch direkt nach einem
Cold-Start) oder wurde vom Server entfernt. 5–10 Sekunden warten, erneut
versuchen; sonst `/connect` neu ausführen.

**`401 TOKEN_MISSING` / `TOKEN_INVALID` / `TOKEN_REVOKED`**
Token fehlt, ist abgelaufen oder wurde widerrufen. `/connect` erneut ausführen.
Jede dieser Antworten enthält einen `hint` mit genau dieser Anleitung.

**Bot startet nicht: `LoginFailure`**
`DISCORD_BOT_TOKEN` ist ungültig. Im Developer Portal **Reset Token**, neues
Token in Render unter *Environment* eintragen, **Save Changes**.

**Bot startet nicht: `PrivilegedIntentsRequired`**
Der Bot startet automatisch ohne privilegierte Intents neu und bleibt online.
Dauerhafte Lösung: Developer Portal → Bot → `SERVER MEMBERS INTENT` aktivieren.

**Token ist nach einem Deploy weg**
Auf Render Free ist das Dateisystem flüchtig. `/connect` erneut ausführen. Für
Persistenz einen bezahlten Plan mit Disk nutzen — in [`render.yaml`](render.yaml)
ist der Block bereits auskommentiert vorbereitet.

**Arena AI meldet `429`**
Das Rate-Limit greift. Die Antwort enthält `error.details.retry_after`. Für
Massenänderungen die Sammel-Endpoints nutzen (`POST /api/v1/setup`,
`/api/v1/channels/bulk`, `/api/v1/roles/bulk`) — die zählen als ein Aufruf.

**Arena AI erreicht die URL nicht**
`PUBLIC_URL` muss gesetzt sein oder `RENDER_EXTERNAL_URL` von Render kommen. Im
Log steht beim Start die erkannte Basis-URL; bei `localhost` erscheint eine
ausdrückliche Warnung.

---

## Projektstruktur

```
AIDiscordServerEinrichten/
├── bot/
│   ├── main.py              Einstiegspunkt: Bot + Web-Server in einem Loop
│   ├── __main__.py          python -m bot
│   ├── discord_bot.py       /connect, /status, /revoke, Buttons, Rechteprüfungen
│   ├── config.py            Umgebungsvariablen, Validierung, maskierte Ausgabe
│   ├── sessions.py          Token erzeugen, hashen, verifizieren, widerrufen
│   ├── prompt.py            Die drei Prompt-Varianten für Arena AI
│   ├── serializers.py       discord.py-Objekte → JSON-sichere Dicts
│   ├── util.py              Parsing: IDs, Farben, Permissions, Enums, Overwrites
│   ├── web_errors.py        ApiError mit code/message/hint/status
│   └── web/
│       ├── app.py           aiohttp-App, Middleware, Rate-Limit, CORS
│       ├── console.py       Das Dashboard (eine HTML-Datei, kein Build)
│       ├── context.py       Pro-Request-Kontext aller Handler
│       ├── registry.py      @route-Dekorator + Endpoint-Verzeichnis
│       ├── channel_ops.py   Kanäle anlegen/bearbeiten/auflösen
│       ├── message_ops.py   Nachrichten, Embeds, Komponenten
│       ├── images.py        Icons/Banner aus URL, Base64 oder Emoji
│       └── routes/
│           ├── meta.py      health, capabilities, me, session, prompt, snapshot
│           ├── guild.py     Server-Einstellungen, Welcome, Onboarding, Widget
│           ├── channels.py  Kanäle, Kategorien, Threads, Positionen
│           ├── roles.py     Rollen, Positionen, Vorlagen
│           ├── members.py   Mitglieder, Suche, Permissions
│           ├── moderation.py Timeout, Kick, Ban, Prune, AutoMod
│           ├── messages.py  Senden, Bearbeiten, Purge, Pins, Reaktionen
│           ├── expressions.py Emojis und Sticker
│           ├── invites.py   Invites auflisten, anlegen, aufräumen
│           ├── events.py    Scheduled Events
│           └── setup.py     Der Setup-Wizard + fünf Vorlagen
├── scripts/
│   ├── smoke_test.py        ~200 Prüfungen ohne Discord-Verbindung
│   └── _fake_discord.py     Echte discord.py-Subklassen als Test-Double
├── ci/
│   ├── ci.yml               GitHub-Actions-Workflow (siehe ci/README.md)
│   └── README.md            So wird er aktiviert
├── Dockerfile               Production-Image (tini als PID 1, non-root)
├── render.yaml              Render-Blueprint
├── requirements.txt
├── .env.example             Kommentierte Konfigurationsvorlage
└── README.md
```

---

## Hinweis

**Kurz gesagt:** Dieses Projekt gibt einer KI Administrator-Rechte über deinen
Discord-Server. Lies den Abschnitt [Sicherheitsmodell](#sicherheitsmodell), bevor
du es auf einem Server einsetzt, der dir wichtig ist — und nutze `/revoke`, wenn
du fertig bist.
