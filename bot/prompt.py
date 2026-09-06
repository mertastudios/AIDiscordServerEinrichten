"""
Erzeugt den **fertigen Prompt** für Arena AI.

Es gibt drei Varianten:

``short``
    Passt in einen Discord-Codeblock (< 1900 Zeichen). Zum direkten Kopieren.
``long``
    Vollständige Version mit Endpoint-Referenz & Workflow. Wird auf der
    Console angezeigt und als ``arena-prompt.md`` an die Nachricht angehängt.
``system``
    Optionaler Systemprompt-Baustein, falls Arena AI ein separates
    System-Prompt-Feld hat.

Der Prompt ist bewusst auf Deutsch, imperativ und mit konkreten ``curl``-
Beispielen formuliert: Je weniger Interpretationsspielraum, desto zuverlässiger
arbeitet die KI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = ("PromptContext", "build_prompt", "short_prompt", "long_prompt", "system_prompt")

DISCORD_MESSAGE_LIMIT = 2000
EMBED_FIELD_LIMIT = 1024


@dataclass(slots=True)
class PromptContext:
    base_url: str
    token: str
    guild_name: str
    guild_id: int
    mode_label: str
    scope: str
    expires_label: str
    session_id: str
    member_count: Optional[int] = None
    console_url: Optional[str] = None

    def as_dict(self, *, include_token: bool = True) -> Dict[str, Any]:
        data = {
            "base_url": self.base_url,
            "guild_name": self.guild_name,
            "guild_id": str(self.guild_id),
            "mode_label": self.mode_label,
            "scope": self.scope,
            "expires_label": self.expires_label,
            "session_id": self.session_id,
            "member_count": self.member_count,
            "console_url": self.console_url,
        }
        if include_token:
            data["token"] = self.token
        return data


def _header(ctx: PromptContext) -> str:
    return (
        f"DISCORD-RELAY-ZUGANG · Server: {ctx.guild_name}\n"
        f"API-BASIS: {ctx.base_url}\n"
        f"TOKEN: {ctx.token}"
    )


def short_prompt(ctx: PromptContext) -> str:
    """Kompakter Prompt für den Discord-Codeblock (Copy & Paste nach Arena AI)."""
    members = f" · {ctx.member_count} Mitglieder" if ctx.member_count else ""
    return f"""{_header(ctx)}

Du (Arena AI) steuerst den Discord-Server "{ctx.guild_name}"{members} als Bot mit Administrator-Rechten über dessen REST-API. Modus: {ctx.mode_label}. Gültig bis: {ctx.expires_label}.

SETZE ZUERST für alle weiteren Befehle:
BASE="{ctx.base_url}"
AUTH="Authorization: Bearer {ctx.token}"

Pflicht-Start (in dieser Reihenfolge):
1) curl -s "$BASE/api/v1/capabilities" -H "$AUTH"
2) curl -s "$BASE/api/v1/guild/snapshot" -H "$AUTH"
Danach liest du, was vorhanden ist, und setzt um, was ich dir sage. Arbeit ausschließlich mit curl über diese API.

REGELN:
• Bei POST/PUT/PATCH zusätzlich: -H "Content-Type: application/json" -d '{{...}}'
• IDs immer als Strings in Anführungszeichen ("123456789012345678").
• Antwort prüfen: "ok":true = Erfolg. Bei "ok":false → error.message + error.hint lesen und korrigieren, nicht raten.
• Erst GET (Ist-Zustand), dann gezielt PATCH/POST/DELETE.
• Komplettes Server-Setup in EINEM Aufruf: POST /api/v1/setup — Permissions als Namen, Farben als "#RRGGBB", Kanaltypen "text|voice|category|forum|stage|announcement".
• Nichts löschen, außer ich sage es ausdrücklich. Bei 429 kurz warten und wiederholen.
• Am Ende: kurze deutsche Zusammenfassung (was erledigt, welche IDs neu, was offen ist).

Jetzt: führe Schritt 1 und 2 aus und sage mir in 3 Sätzen, wie der Server aktuell aussieht. Dann warte auf meine Anweisungen."""


def long_prompt(ctx: PromptContext) -> str:
    """Ausführlicher Prompt mit Endpoint-Referenz — für Console & .md-Anhang."""
    members = f" · {ctx.member_count} Mitglieder" if ctx.member_count else ""
    return f"""# Arena AI ⇄ Discord-Server: {ctx.guild_name}

Du bist über die Relay-API des Bots **AIDiscordServerEinrichten** mit einem echten
Discord-Server verbunden. Der Bot besitzt dort Administrator-Rechte; du steuerst ihn
per HTTPS — ohne jemals den Bot-Token zu sehen.

```
SERVER   : {ctx.guild_name}  (ID {ctx.guild_id}){members}
API-BASIS: {ctx.base_url}
TOKEN    : {ctx.token}
MODUS    : {ctx.mode_label}
GÜLTIG   : {ctx.expires_label}
SESSION  : {ctx.session_id}
CONSOLE  : {ctx.console_url or ctx.base_url + "/"}
```

## 1. Grundregeln (immer beachten)

* **Authentifizierung** bei jedem Aufruf: `-H "Authorization: Bearer {ctx.token}"`
* **Body** bei POST/PUT/PATCH: `-H "Content-Type: application/json" -d '{{...}}'`
* **IDs sind Strings** (`"123456789012345678"`), niemals nackte Zahlen — JSON verliert
  sonst Präzision.
* **Antwortformat**: `{{"ok": true, "data": …}}` oder `{{"ok": false, "error": {{"code","message","hint"}}}}`.
  Bei einem Fehler: `error.message` und `error.hint` lesen, Aufruf korrigieren, erneut versuchen.
  Niemals blind andere Werte raten.
* **Erst lesen, dann schreiben.** Vor jeder Änderung den Ist-Zustand per GET holen.
* **Löschaktionen nur auf ausdrückliche Anweisung** des Nutzers.
* Bei HTTP **429**: `error.details.retry_after` Sekunden warten, dann wiederholen.
* Bei HTTP **401**: Token abgelaufen/widerrufen → Nutzer bitten, `/connect` erneut auszuführen.
* Alles auf **Deutsch** dokumentieren und am Ende zusammenfassen.

## 2. Standard-Workflow

```bash
BASE="{ctx.base_url}"
AUTH="Authorization: Bearer {ctx.token}"
CT="Content-Type: application/json"

# 1) Was kann die API? (selbstbeschreibend, immer aktuell)
curl -s "$BASE/api/v1/capabilities" -H "$AUTH"

# 2) Kompletter Ist-Zustand in EINEM Aufruf
curl -s "$BASE/api/v1/guild/snapshot" -H "$AUTH"

# 3) Struktur als Baum (Kategorien → Kanäle → Threads)
curl -s "$BASE/api/v1/channels/tree" -H "$AUTH"

# 4) Rollen verstehen (wichtig für Permissions/Overwrites)
curl -s "$BASE/api/v1/roles" -H "$AUTH"

# 5) Jetzt ändern — Beispiel: Kategorie + 3 Kanäle + 2 Rollen in einem Rutsch
curl -s -X POST "$BASE/api/v1/setup" -H "$AUTH" -H "$CT" -d @setup.json
```

## 3. Endpoint-Übersicht

| Zweck | Endpoints |
|---|---|
| Meta | `GET /api/v1/me` · `GET /api/v1/capabilities` · `GET /api/v1/session` · `DELETE /api/v1/session` · `GET /api/v1/actions` · `GET /api/v1/guild/snapshot` |
| Server | `GET/PATCH /api/v1/guild` · `GET /api/v1/guild/preview` · `GET/PATCH /api/v1/guild/widget` · `GET/PATCH /api/v1/guild/welcome-screen` · `GET/PATCH /api/v1/guild/onboarding` · `PATCH /api/v1/guild/mfa` · `GET /api/v1/guild/audit-logs` · `GET/POST /api/v1/guild/templates` · `GET /api/v1/guild/voice-states` · `GET /api/v1/guild/prune/count` · `POST /api/v1/guild/prune` |
| Kanäle | `GET /api/v1/channels` · `GET /api/v1/channels/tree` · `POST /api/v1/channels` · `POST /api/v1/channels/bulk` · `GET/PATCH/DELETE /api/v1/channels/{{id}}` · `PATCH /api/v1/channels/positions` · `PUT/DELETE /api/v1/channels/{{id}}/permissions/{{target}}` · `POST /api/v1/channels/{{id}}/threads` |
| Rollen | `GET/POST /api/v1/roles` · `POST /api/v1/roles/bulk` · `GET/PATCH/DELETE /api/v1/roles/{{id}}` · `PATCH /api/v1/roles/positions` |
| Mitglieder | `GET /api/v1/members` · `GET /api/v1/members/search?q=` · `GET/PATCH /api/v1/members/{{id}}` · `PUT/DELETE /api/v1/members/{{id}}/roles/{{role}}` · `DELETE /api/v1/members/{{id}}` (Kick) |
| Moderation | `GET /api/v1/bans` · `PUT/DELETE /api/v1/bans/{{id}}` · `POST/DELETE /api/v1/timeout/{{id}}` · `GET/POST /api/v1/automod/rules` · `PATCH/DELETE /api/v1/automod/rules/{{id}}` |
| Nachrichten | `GET/POST /api/v1/channels/{{id}}/messages` · `GET/PATCH/DELETE /api/v1/channels/{{cid}}/messages/{{mid}}` · `POST /api/v1/channels/{{id}}/messages/bulk-delete` · `PUT/DELETE …/reactions/{{emoji}}` · `GET/PUT/DELETE /api/v1/channels/{{id}}/pins` · `POST /api/v1/channels/{{id}}/typing` |
| Invites | `GET /api/v1/invites` · `POST /api/v1/channels/{{id}}/invites` · `DELETE /api/v1/invites/{{code}}` |
| Expressions | `GET/POST /api/v1/emojis` · `PATCH/DELETE /api/v1/emojis/{{id}}` · `GET /api/v1/stickers` |
| Events | `GET/POST /api/v1/events` · `PATCH/DELETE /api/v1/events/{{id}}` |
| Webhooks | `GET/POST /api/v1/channels/{{id}}/webhooks` · `DELETE /api/v1/webhooks/{{id}}` |
| Setup | `POST /api/v1/setup` · `POST /api/v1/setup/preview` |

> Die **verbindliche** Liste liefert immer `GET /api/v1/capabilities` — sie enthält
> Methode, Pfad, Beschreibung, benötigten Scope und Body-Felder.

## 4. `POST /api/v1/setup` — der ganze Server in einem Aufruf

```json
{{
  "guild": {{
    "name": "Mein Server",
    "verification_level": "medium",
    "explicit_content_filter": "all_members",
    "default_notifications": "only_mentions",
    "community": true
  }},
  "roles": [
    {{"key": "admin",  "name": "👑 Admin",  "color": "#E74C3C", "hoist": true,
      "permissions": ["administrator"]}},
    {{"key": "mod",    "name": "🛡️ Mod",    "color": "#3498DB", "hoist": true,
      "permissions": ["kick_members", "ban_members", "moderate_members", "manage_messages"]}},
    {{"key": "member", "name": "Mitglied",  "color": "#2ECC71",
      "permissions": ["view_channel", "send_messages", "read_message_history"]}}
  ],
  "categories": [
    {{
      "key": "info",
      "name": "📌 INFORMATION",
      "overwrites": [
        {{"id": "@everyone", "deny": ["send_messages"]}},
        {{"role_key": "mod",  "allow": ["send_messages", "manage_messages"]}}
      ],
      "channels": [
        {{"key": "regeln", "name": "regeln", "type": "text",
          "topic": "Bitte erst lesen!", "slowmode_delay": 0}},
        {{"key": "news", "name": "ankündigungen", "type": "announcement"}},
        {{"key": "willkommen", "name": "willkommen", "type": "text"}}
      ]
    }},
    {{
      "key": "community",
      "name": "💬 COMMUNITY",
      "channels": [
        {{"key": "chat", "name": "allgemein", "type": "text", "slowmode_delay": 3}},
        {{"name": "memes", "type": "text"}},
        {{"name": "bilder", "type": "forum"}},
        {{"name": "Lounge", "type": "voice", "user_limit": 10}}
      ]
    }}
  ],
  "messages": [
    {{"channel": "regeln", "content": "**Serverregeln**\\n1. Seid nett zueinander."}},
    {{"channel": "willkommen",
      "embeds": [{{"title": "Willkommen!", "description": "Schön, dass du da bist.",
                   "color": "#5865F2"}}]}}
  ],
  "settings": {{
    "system_channel": "willkommen",
    "rules_channel": "regeln"
  }},
  "reason": "Server-Grundsetup durch Arena AI"
}}
```

Antwort: `data.created` enthält alle neuen IDs, indexiert über deine `key`-Werte —
du kannst also später direkt mit `"channel": "regeln"` weiterarbeiten.

## 5. Wichtige Details

**Permissions** — Namen statt Zahlen:
`view_channel`, `send_messages`, `read_message_history`, `manage_channels`,
`manage_roles`, `manage_messages`, `manage_guild`, `kick_members`, `ban_members`,
`moderate_members` (Timeout), `administrator`, `create_instant_invite`,
`embed_links`, `attach_files`, `add_reactions`, `mention_everyone`,
`connect`, `speak`, `stream`, `mute_members`, `deafen_members`, `move_members`,
`use_voice_activation`, `priority_speaker`, `manage_threads`,
`create_public_threads`, `create_private_threads`, `send_messages_in_threads`,
`use_application_commands`, `manage_webhooks`, `manage_expressions`,
`manage_events`, `create_events`, `change_nickname`, `manage_nicknames`,
`view_audit_log`, `request_to_speak`, `use_soundboard`, `send_polls`.

**Overwrites** (Kanalberechtigungen):
```json
"overwrites": [
  {{"id": "@everyone", "deny": ["send_messages", "create_instant_invite"]}},
  {{"type": "role", "id": "123456789012345678", "allow": ["manage_messages"]}},
  {{"type": "member", "id": "987654321098765432", "allow": ["view_channel"]}}
]
```
Auch möglich: `"role:Mods"`, `"role:123…"`, `"member:123…"`, `"<@123…>"`.

**Nachrichten senden** — volles Discord-Feature-Set:
```json
{{
  "content": "Hallo @here",
  "tts": false,
  "embeds": [{{"title": "T", "description": "D", "color": "#5865F2",
               "fields": [{{"name": "F", "value": "V", "inline": true}}],
               "footer": {{"text": "by Arena AI"}}, "thumbnail": "https://…"}}],
  "allowed_mentions": {{"parse": ["users"], "replied_user": false}},
  "reply_to": "123456789012345678"
}}
```

**Audit-Grund**: Fast jeder schreibende Endpoint akzeptiert `"reason": "…"`. Immer
ausfüllen — das erscheint im Discord-Audit-Log und macht Änderungen nachvollziehbar.

## 6. Sicherheits-Leitplanken

1. **Keine destruktiven Aktionen ohne Auftrag**: Kein `DELETE` auf Kanäle/Rollen,
   kein Server-Rename, kein Massen-Ban, solange der Nutzer es nicht sagt.
2. **Rollen-Hierarchie**: Der Bot kann keine Rolle über seiner eigenen Top-Rolle
   ändern. Fehler `50035` → Rolle niedriger anlegen oder Nutzer um Position bitten.
3. **Community-Features** (`rules_channel`, Welcome-Screen, Onboarding) brauchen
   `community: true`. Erst aktivieren, dann zuweisen.
4. **Privilegierte Aktionen** (`mfa_level`, Vanity-URL, Server-Transfer) brauchen
   Rechte des Server-Owners — Fehler einfach an den Nutzer weitergeben.
5. **Token nie weitergeben**, nie in eine Nachricht auf dem Server schreiben,
   nie in Logs ausgeben.

## 7. Deine Aufgabe

Der Nutzer beschreibt dir jetzt auf Deutsch, wie der Server aussehen soll.
Du: (a) liest den Ist-Zustand, (b) planst kurz und bestätigst den Plan,
(c) setzt ihn mit möglichst wenigen, gebündelten API-Aufrufen um,
(d) verifizierst per GET, (e) fasst auf Deutsch zusammen, was du geändert hast —
mit den neuen Kanal-/Rollen-Namen und IDs.

Los geht's mit `GET /api/v1/guild/snapshot`.
"""


def system_prompt(ctx: PromptContext) -> str:
    """Kompakter Systemprompt-Baustein (falls separat unterstützt)."""
    return (
        "Du bist ein Discord-Server-Einrichtungs-Agent. Du steuerst den Discord-Server "
        f"'{ctx.guild_name}' ausschließlich über die REST-API unter {ctx.base_url}/api/v1 "
        f"mit dem Bearer-Token {ctx.token}. Nutze curl und sende bei JEDEM Aufruf "
        f'-H "Authorization: Bearer {ctx.token}". '
        f"Erster Schritt: GET {ctx.base_url}/api/v1/capabilities — dort steht die komplette "
        "API-Beschreibung, danach GET /api/v1/guild/snapshot für den Ist-Zustand. "
        "Alle IDs sind Strings. Antworte auf Deutsch. "
        'Prüfe jede Antwort auf "ok":true und folge error.hint bei Fehlern, statt zu raten. '
        "Bündle Massenänderungen in POST /api/v1/setup. Führe keine destruktiven Aktionen "
        "(Löschen, Bannen, Prune) ohne ausdrückliche Anweisung aus."
    )


def build_prompt(ctx: PromptContext, *, variant: str = "short") -> str:
    variants = {"short": short_prompt, "long": long_prompt, "system": system_prompt}
    try:
        builder = variants[variant]
    except KeyError as exc:
        raise ValueError(f"Unbekannte Prompt-Variante {variant!r} (erlaubt: {sorted(variants)})") from exc
    return builder(ctx)
