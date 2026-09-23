"""
Der Discord-Teil: Slash-Commands, Buttons und Rechteprüfungen.

``/connect`` ist *der* Command — bewusst **ohne Optionen**. Dauer und Modus
waren Stolperfallen; jetzt gelten die Defaults aus der Konfiguration
(24 h Gültigkeit, Modus „Lesen + Schreiben“ — alles per Umgebungsvariablen
änderbar).

Die Antwort ist eine **ephemerale Container-V2-Nachricht** (Discord
*Components V2*) mit genau einem Container, der zwei Zustände hat:

**Noch nicht verbunden** — die Bridge ist für diesen Server deaktiviert:

* ``# Willkommen!`` + kurzer Einstiegstext
* ein grüner **Verbinden**-Button: erzeugt das Sitzungs-Token und bearbeitet
  dieselbe Nachricht in den verbundenen Zustand

**Verbunden**:

* ``# Willkommen!`` — „Du kannst sofort mit Arena AI losarbeiten.“
* **1. Kopiere diesen Prompt** — ein Mini-Prompt im Codeblock: nur URL und
  Token. Alles Weitere (Regeln, Workflow, Endpoints) erfährt Arena AI *während
  der Arbeit* von der Bridge selbst (``GET /api/v1/capabilities`` samt
  Konventionen und den ``/guides``-Endpoints).
* **2. Öffne Arena AI …** — inklusive Link-Button direkt zum Arena-Agenten
* **Weitere Optionen** — **Verbindung trennen** (rot: widerruft *alle* Tokens
  dieses Servers) und **Neues Token generieren** (macht das alte sofort
  ungültig und zeigt den aktualisierten Prompt)

Die Buttons sind persistent (``timeout=None`` + Registrierung in
``setup_hook``): Auch nach einem Render-Deploy reagieren alte Nachrichten noch.
Der Prompt darin ist bewusst ein **einzeiliger** Codeblock — Discord-Mobile
kopiert einzeilige Codeboxen mit einem einzigen Tipp, mehrzeilige nicht.

Der Zugriff wird direkt in ``/connect`` über die Buttons verwaltet; separate
``/status``- und ``/revoke``-Commands gibt es nicht.

**Owner-Features** (``BOT_OWNER_ID``): Die Presence zeigt live
``/connect | 👀 N eingerichtete Server``. Im Privatchat mit dem Bot gibt es
``/adminpanel`` — eine durchsuchbare, blätterbare Server-Liste (sortiert nach
Mitgliederzahl; Server, auf denen der Owner **nicht** Mitglied ist, stehen mit
❗️ ganz oben). Der Owner bekommt zusätzlich eine DM, wenn der Bot einem Server
beitritt oder ihn verlässt (mit bestmöglichem Grund), und der Server-Owner
erhält beim Beitritt eine Willkommens-DM mit der Kurzanleitung.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands

from . import __version__
from .config import Config, mask_proxy_url
from .sessions import DEFAULT_MODE, SessionStore
from .util import ApiError, now_utc

log = logging.getLogger("relay.discord")

__all__ = (
    "RelayClient", "build_intents", "ADMIN_INVITE_SCOPES", "ARENA_URL",
    "connected_view", "welcome_view", "admin_panel_view",
    "admin_guild_detail_view", "admin_guild_leave_confirm_view",
)

ADMIN_INVITE_SCOPES = ("bot", "applications.commands")

#: Mindestabstand zwischen zwei globalen Slash-Command-Syncs (Sekunden).
COMMAND_SYNC_MIN_INTERVAL = 3600.0

OK_COLOR = discord.Color.from_str("#2ECC71")
WARN_COLOR = discord.Color.from_str("#F1C40F")
ERR_COLOR = discord.Color.from_str("#E74C3C")

#: Ziel des „Arena AI öffnen“-Link-Buttons.
ARENA_URL = "https://arena.ai/agent"

#: Akzentfarbe der Container: Blurple fürs Willkommen, Grün für verbunden.
WELCOME_ACCENT = discord.Color.from_str("#5865F2")
CONNECTED_ACCENT = discord.Color.from_str("#2ECC71")

# Custom-IDs der persistenten Buttons — sie überleben einen Neustart.
CID_CONNECT = "relay:v2_connect"
CID_DISCONNECT = "relay:v2_disconnect"
CID_REGENERATE = "relay:v2_regenerate"

# Legacy-IDs der Buttons vor dem Components-V2-Upgrade. Nachrichten aus der
# Zeit vor dem Deploy sollen nicht ins Leere laufen: Ihre Klicks werden auf
# die neuen Handler gemappt (die bearbeiten die alte Nachricht dann in die
# neue Optik).
CID_LEGACY_REGENERATE = "relay:regenerate"
CID_LEGACY_REVOKE_ALL = "relay:revoke_all"

#: Adminpanel: Buttons + Suchen-Modal. Die Custom-IDs tragen die Ziel-Seite
#: und den Suchbegriff in sich (``relay:admin:nav:<seite>:<suche>``), damit
#: auch alte Panel-Nachrichten nach einem Neustart noch bedienbar sind.
CID_ADMIN_PREFIX = "relay:admin:"
CID_ADMIN_SEARCH = "relay:admin:search"

#: Wie viele Server das Adminpanel pro Seite anzeigt.
ADMIN_PAGE_SIZE = 10

#: Max. Länge des Suchbegriffs im Adminpanel — die Custom-IDs der Blätter-
#: Buttons transportieren ihn mit und sind auf 100 Zeichen begrenzt.
ADMIN_QUERY_MAX = 40


def build_intents(privileged: bool = True) -> discord.Intents:
    """
    Intents zusammenstellen.

    ``members`` und ``message_content`` sind **privilegiert** und müssen im
    Developer Portal aktiviert sein. Ist ``privileged=False``, wird ohne sie
    gestartet — der Bot läuft dann weiter, nur die Mitgliederliste ist ggf.
    unvollständig (die API weicht automatisch auf REST/Gateway-Suche aus).
    """
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = privileged
    intents.message_content = privileged
    intents.guild_messages = True
    intents.invites = True
    intents.emojis_and_stickers = True
    return intents


# ─────────────────────────────────────────────────────────────────────────────
#  Container-V2-Nachrichten (Components V2)
# ─────────────────────────────────────────────────────────────────────────────


def _bridge_prompt_line(base_url: str, token: str) -> str:
    """
    Der Mini-Prompt für Arena AI: **nur** URL und Token — ohne Leerzeichen.

    Das maschinenlesbare ``URL=…;TOKEN=…``-Format ist trotz seiner Kompaktheit
    eindeutig und lässt sich von Arena zuverlässig auswerten.

    Der komplette Regel- und Workflow-Katalog (Hygiene, Personas, Branding,
    Unicode-Design …) muss der Nutzer nicht mehr kopieren — die Bridge erklärt
    sich Arena AI selbst, sobald diese arbeitet: ``GET /api/v1/capabilities``
    liefert Konventionen und Endpoints, dazu die ``/api/v1/guides/*``-Texte.
    """
    return f"URL={base_url};TOKEN={token}"


def welcome_view() -> discord.ui.LayoutView:
    """Container-V2-Nachricht für „Bridge noch deaktiviert“."""
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "# Willkommen!\n"
            "Du kannst sofort mit Arena AI losarbeiten. Die Bridge für diesen "
            "Server ist aktuell noch deaktiviert. Um zu beginnen, klicke auf "
            "diesen Button:"
        ),
        discord.ui.Separator(),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Verbinden",
                emoji="🔌",
                style=discord.ButtonStyle.success,
                custom_id=CID_CONNECT,
            )
        ),
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def connected_view(base_url: str, token: str) -> discord.ui.LayoutView:
    """Container-V2-Nachricht für „Bridge aktiv“: Prompt, Arena-Link, Optionen."""
    container = discord.ui.Container(
        discord.ui.TextDisplay(
            "# Willkommen!\n"
            "Du kannst sofort mit Arena AI losarbeiten."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**1. Kopiere diesen Prompt:**\n"
            # Bewusst EIN einzeiliger Inline-Codeblock mit einfachen Backticks:
            # Discord-Mobile kopiert diesen mit einem einzigen Tipp.
            f"`{_bridge_prompt_line(base_url, token)}`"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**2. Öffne Arena AI, schreib ihm was er auf deinem Server "
            "einrichten soll und schick ihm den Prompt:**"
        ),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Arena AI öffnen",
                emoji="🤖",
                style=discord.ButtonStyle.link,
                url=ARENA_URL,
            )
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay("**Weitere Optionen:**"),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Verbindung trennen",
                emoji="⛔",
                style=discord.ButtonStyle.danger,
                custom_id=CID_DISCONNECT,
            ),
            discord.ui.Button(
                label="Neues Token generieren",
                emoji="🔄",
                style=discord.ButtonStyle.secondary,
                custom_id=CID_REGENERATE,
            ),
        ),
        accent_colour=CONNECTED_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _button_registration_view() -> discord.ui.LayoutView:
    """
    Wird nie angezeigt: Diese View registriert in ``setup_hook`` nur die drei
    Custom-ID-Buttons bei discord.py, damit Klicks auf alte Nachrichten auch
    nach einem Neustart ihren Handler finden. Die eigentliche Arbeit passiert
    in :meth:`RelayClient.on_interaction` — die Buttons hier haben absichtlich
    keinen Callback.
    """
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.ActionRow(
                discord.ui.Button(label="Verbinden", custom_id=CID_CONNECT),
                discord.ui.Button(label="Verbindung trennen", custom_id=CID_DISCONNECT),
                discord.ui.Button(label="Neues Token generieren", custom_id=CID_REGENERATE),
            )
        )
    )
    return view


# ─────────────────────────────────────────────────────────────────────────────
#  Adminpanel (Container V2, nur für den Bot-Owner im Privatchat)
# ─────────────────────────────────────────────────────────────────────────────


def _admin_custom_id(action: str, page: int, query: str) -> str:
    """Baut eine Admin-Custom-ID: Seite + Suchbegriff reisen in der ID mit."""
    return f"{CID_ADMIN_PREFIX}{action}:{page}:{query[:ADMIN_QUERY_MAX]}"


def _admin_guild_custom_id(action: str, guild_id: int, page: int, query: str) -> str:
    """Baut eine Admin-Custom-ID mit Guild-ID: ``relay:admin:<action>:<guild_id>:<seite>:<suche>``."""
    return f"{CID_ADMIN_PREFIX}{action}:{guild_id}:{page}:{query[:ADMIN_QUERY_MAX]}"


def _parse_admin_custom_id(custom_id: str) -> Optional[Dict[str, Any]]:
    """Zerlegt Admin-Custom-IDs — ``None`` wenn unpassend."""
    if not custom_id.startswith(CID_ADMIN_PREFIX):
        return None
    rest = custom_id[len(CID_ADMIN_PREFIX):]
    parts = rest.split(":")
    action = parts[0]
    if action == "search":
        return {"action": "search", "page": 0, "query": ""}

    if action in ("guild", "invite", "leave", "leave_confirm"):
        guild_id_raw = parts[1] if len(parts) > 1 else ""
        try:
            guild_id: Optional[int] = int(guild_id_raw)
        except ValueError:
            guild_id = None
        page_raw = parts[2] if len(parts) > 2 else "0"
        query = ":".join(parts[3:]) if len(parts) > 3 else ""
        try:
            page = max(0, int(page_raw))
        except ValueError:
            page = 0
        return {"action": action, "guild_id": guild_id, "page": page, "query": query[:ADMIN_QUERY_MAX]}

    page_raw = parts[1] if len(parts) > 1 else "0"
    query = ":".join(parts[2:]) if len(parts) > 2 else ""
    try:
        page = max(0, int(page_raw))
    except ValueError:
        page = 0
    return {"action": action, "page": page, "query": query[:ADMIN_QUERY_MAX]}


def admin_panel_view(
    entries: List[Dict[str, Any]],
    *,
    page: int,
    query: str,
    total_guilds: int,
) -> discord.ui.LayoutView:
    """
    Baut das Adminpanel aus **vorsortierten** Einträgen
    (``{"guild", "member_count", "owner_present"}``).

    Sortiert hat :meth:`RelayClient._collect_admin_guilds`: Server ohne
    Bot-Owner zuerst (❗️), dann Mitglieder abwärts. Suchbegriff und Seitenzahl
    stecken in den Custom-IDs der Buttons — so bleibt auch ein altes Panel
    nach einem Neustart bedienbar (Dispatch via ``on_interaction``).
    """
    page_size = ADMIN_PAGE_SIZE
    total = len(entries)
    pages = max(1, -(-total // page_size))          # aufrunden ohne math.ceil
    page = max(0, min(page, pages - 1))
    chunk = entries[page * page_size:(page + 1) * page_size]

    lines: List[str] = []
    select_options: List[discord.SelectOption] = []
    for entry in chunk:
        guild = entry["guild"]
        marker = "" if entry["owner_present"] else "❗️ "
        count = f"{entry['member_count']:,}".replace(",", ".")
        lines.append(f"{marker}**{guild.name}** · {count} Mitglieder")
        desc = f"{count} Mitglieder"
        if not entry["owner_present"]:
            desc += " · ❗️ Kein Mitglied"
        select_options.append(
            discord.SelectOption(
                label=getattr(guild, "name", "Server")[:100],
                value=str(guild.id),
                description=desc[:100],
                emoji="❗️" if not entry["owner_present"] else "🏰",
            )
        )
    if not lines:
        lines.append("Keine Server gefunden." if query else "Der Bot ist auf keinem Server.")

    header = f"🛠️ Adminpanel · {total_guilds} Server gesamt · {total} angezeigt"
    if query:
        header += f" · Suche: „{query}“"
    legend = "❗️ = Du bist auf diesem Server noch **nicht** Mitglied — die stehen ganz oben."
    footer = f"Seite {page + 1}/{pages} · Sortierung: ❗️-Server zuerst, dann Mitgliederzahl"

    buttons = [
        discord.ui.Button(
            label="Zurück", emoji="◀️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", page - 1, query), disabled=page <= 0,
        ),
        discord.ui.Button(
            label="Suchen", emoji="🔍", style=discord.ButtonStyle.primary,
            custom_id=CID_ADMIN_SEARCH,
        ),
        discord.ui.Button(
            label="Weiter", emoji="▶️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", page + 1, query), disabled=page >= pages - 1,
        ),
        discord.ui.Button(
            label="Schließen", emoji="✖️", style=discord.ButtonStyle.danger,
            custom_id=_admin_custom_id("close", page, query),
        ),
    ]
    if query:
        buttons.append(discord.ui.Button(
            label="Suche löschen", emoji="✖️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", 0, ""),
        ))

    container_items: List[Any] = [
        discord.ui.TextDisplay(f"# {header}"),
        discord.ui.TextDisplay(legend),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(lines)),
    ]

    if select_options:
        container_items.extend([
            discord.ui.Separator(),
            discord.ui.ActionRow(
                discord.ui.Select(
                    placeholder="Server für Details auswählen…",
                    custom_id=_admin_custom_id("select", page, query),
                    options=select_options,
                )
            ),
        ])

    container_items.extend([
        discord.ui.Separator(),
        discord.ui.TextDisplay(footer),
        discord.ui.ActionRow(*buttons),
    ])

    container = discord.ui.Container(
        *container_items,
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _admin_sessions_detail_text(sessions: List[Any]) -> str:
    """Formatierte Liste aktiver Sitzungen für die Server-Detailansicht."""
    if not sessions:
        return ""
    lines: List[str] = []
    for s in sessions:
        info = s.to_public_dict()
        last_used = f" · zuletzt genutzt: {info['last_used_ago']}" if info.get("last_used_ago") else ""
        lines.append(
            f"• `{s.token_prefix}` ({info['mode_label']}) · Ablauf: {info['expires_in']}"
            f" · {info['request_count']} Requests{last_used} · von {info['created_by_name']}"
        )
    return "\n".join(lines)[:1000]


def admin_guild_detail_view(
    guild: discord.Guild,
    *,
    owner: Optional[Any],
    owner_present: bool,
    sessions: List[Any],
    page: int,
    query: str,
    invite_url: Optional[str] = None,
    notice: Optional[str] = None,
) -> discord.ui.LayoutView:
    """Server-Übersicht (Container V2) im Adminpanel."""
    count = f"{(guild.member_count or 0):,}".replace(",", ".")
    owner_str = _owner_line(guild, owner)
    member_status = "✅ Du bist Mitglied auf diesem Server" if owner_present else "❗️ Du bist auf diesem Server **noch nicht** Mitglied"

    details = [
        f"**Server-ID:** `{guild.id}`",
        f"**Mitglieder:** {count}",
        owner_str,
        f"**Bot-Owner-Status:** {member_status}",
    ]

    components: List[Any] = [
        _guild_header_section(
            guild,
            f"🏰 {guild.name}",
            details,
        ),
        discord.ui.Separator(),
    ]

    if sessions:
        components.append(
            discord.ui.TextDisplay(
                f"**🔗 Aktive KI-Verbindung ({len(sessions)} Token):**\n"
                f"{_admin_sessions_detail_text(sessions)}"
            )
        )
    else:
        components.append(
            discord.ui.TextDisplay(
                "**🔗 KI-Verbindung:**\n"
                "⚪ Keine aktiven Tokens — Bridge für diesen Server ist nicht verbunden."
            )
        )

    if invite_url:
        components.extend([
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"**✉️ Einladungslink (1 Stunde gültig, 1 Nutzung):**\n<{invite_url}>"
            ),
        ])

    if notice:
        components.extend([
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"⚠️ **Hinweis:** {notice}"),
        ])

    buttons = [
        discord.ui.Button(
            label="Zurück", emoji="◀️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("nav", page, query),
        ),
        discord.ui.Button(
            label="Einladung erstellen", emoji="✉️", style=discord.ButtonStyle.primary,
            custom_id=_admin_guild_custom_id("invite", guild.id, page, query),
        ),
        discord.ui.Button(
            label="Server verlassen", emoji="🚪", style=discord.ButtonStyle.danger,
            custom_id=_admin_guild_custom_id("leave", guild.id, page, query),
        ),
        discord.ui.Button(
            label="Schließen", emoji="✖️", style=discord.ButtonStyle.secondary,
            custom_id=_admin_custom_id("close", page, query),
        ),
    ]

    components.extend([
        discord.ui.Separator(),
        discord.ui.ActionRow(*buttons),
    ])

    container = discord.ui.Container(
        *components,
        accent_colour=CONNECTED_ACCENT if sessions else WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def admin_guild_leave_confirm_view(
    guild: discord.Guild,
    *,
    page: int,
    query: str,
) -> discord.ui.LayoutView:
    """Bestätigungsansicht (Container V2) vor dem Verlassen eines Servers."""
    container = discord.ui.Container(
        discord.ui.TextDisplay(f"# ⚠️ Server wirklich verlassen?"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"Möchtest du wirklich, dass der Bot den Server **{guild.name}** (`{guild.id}`) verlässt?\n\n"
            "Der Bot wird sofort vom Server entfernt und alle aktiven KI-Zugriffe werden gestoppt."
        ),
        discord.ui.Separator(),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Abbrechen", emoji="◀️", style=discord.ButtonStyle.secondary,
                custom_id=_admin_guild_custom_id("guild", guild.id, page, query),
            ),
            discord.ui.Button(
                label="Ja, Server verlassen", emoji="🚪", style=discord.ButtonStyle.danger,
                custom_id=_admin_guild_custom_id("leave_confirm", guild.id, page, query),
            ),
            discord.ui.Button(
                label="Schließen", emoji="✖️", style=discord.ButtonStyle.secondary,
                custom_id=_admin_custom_id("close", page, query),
            ),
        ),
        accent_colour=ERR_COLOR,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


class AdminSearchModal(discord.ui.Modal, title="🔍 Server suchen"):
    """Das Such-Fenster des Adminpanels — nach Absenden startet die Liste bei Seite 1."""

    query = discord.ui.TextInput(
        label="Servername (Teile genügen)",
        placeholder="z. B. Community",
        max_length=ADMIN_QUERY_MAX,
        required=False,
    )

    def __init__(self, client: "RelayClient") -> None:
        super().__init__(timeout=300)
        self.client = client

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = (self.query.value or "").strip()
        entries = await self.client._collect_admin_guilds()
        if query:
            lowered = query.lower()
            entries = [e for e in entries if lowered in e["guild"].name.lower()]
        view = admin_panel_view(entries, page=0, query=query, total_guilds=len(self.client.guilds))
        try:
            await interaction.response.send_message(view=view)
        except discord.HTTPException as exc:
            log.error("Adminpanel-Suche konnte nicht antworten: %s", exc)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        log.exception("Fehler im Adminpanel-Suchfenster.", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ Suche fehlgeschlagen: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ Suche fehlgeschlagen: {error}", ephemeral=True)
        except discord.HTTPException:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Owner-Benachrichtigungen (Container V2 mit Server-Icon)
# ─────────────────────────────────────────────────────────────────────────────


def _guild_header_section(guild: discord.Guild, title: str, detail_lines: List[str]) -> Any:
    """
    Kopfzeile einer Guild-DM: Titel + Details, das Server-Icon als Thumbnail.

    Mit Icon: ein ``Section`` mit Thumbnail-Accessory. **Ohne** Icon: ein
    schlichtes ``TextDisplay``. Der frühere Fallback mit einem leeren
    ``TextDisplay`` im Accessory-Slot einer Section war **ungültig** —
    Discord lässt dort ausschließlich *Button* oder *Thumbnail* zu und
    lehnte die komplette Nachricht mit ``400 Invalid Form Body`` ab. Das war
    der Grund dafür, dass die Server-Übersicht des Adminpanels (und die
    Join-/Leave-DMs) für jeden Server **ohne eigenes Icon** mit
    „Die Anwendung reagiert nicht" abbrachen.
    """
    icon = getattr(guild, "icon", None)
    text = discord.ui.TextDisplay(f"## {title}\n" + "\n".join(detail_lines))
    if icon is not None:
        try:
            return discord.ui.Section(text, accessory=discord.ui.Thumbnail(str(icon.url)))
        except Exception:  # noqa: BLE001 — Icon darf den Aufbau nie sprengen
            pass
    # Kein Icon (oder das Icon war nicht serialisierbar): Absichtlich KEINE
    # Section mit Ersatz-Accessory bauen — ein TextDisplay-Accessory ist für
    # Discord ungültige Payload (siehe Docstring oben).
    return text


def _owner_line(guild: discord.Guild, owner: Optional[Any]) -> str:
    if owner is not None:
        return f"Owner: {owner.mention} ({getattr(owner, 'name', '?')})"
    return f"Owner: <@{guild.owner_id}> (Konnte nicht geladen werden)"


def guild_join_notify_view(guild: discord.Guild, owner: Optional[Any]) -> discord.ui.LayoutView:
    """DM an den Bot-Owner: der Bot ist einem Server beigetreten."""
    count = f"{(guild.member_count or 0):,}".replace(",", ".")
    container = discord.ui.Container(
        _guild_header_section(
            guild,
            f"🟢 Server beigetreten: {guild.name}",
            [f"Mitglieder: **{count}**", _owner_line(guild, owner), f"ID: `{guild.id}`"],
        ),
        accent_colour=OK_COLOR,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def guild_remove_notify_view(
    guild: discord.Guild, owner: Optional[Any], reason: str
) -> discord.ui.LayoutView:
    """DM an den Bot-Owner: der Bot hat einen Server verlassen (mit bestmöglichem Grund)."""
    count = f"{(guild.member_count or 0):,}".replace(",", ".")
    container = discord.ui.Container(
        _guild_header_section(
            guild,
            f"🔴 Server verlassen: {guild.name}",
            [f"Mitglieder: **{count}**", _owner_line(guild, owner), f"ID: `{guild.id}`"],
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(f"**Grund:** {reason}"),
        accent_colour=ERR_COLOR,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def server_owner_welcome_view(guild: discord.Guild) -> discord.ui.LayoutView:
    """
    DM an den Server-Owner direkt nach dem Beitritt: die Kurzanleitung.

    Bewusst freundlich und kurz — wer den Bot einlädt, soll in 30 Sekunden
    wissen, wie er Arena AI auf seinem Server arbeiten lässt.
    """
    container = discord.ui.Container(
        discord.ui.TextDisplay("# Hey! 👋"),
        discord.ui.TextDisplay(
            f"Danke, dass du mich auf **{guild.name}** eingeladen hast!\n\n"
            "Mit mir kannst du **Arena AI direkt auf deinem Discord-Server arbeiten "
            "lassen** — sie richtet dir alles ein: Rollen, Kanäle, Kategorien, "
            "Rechte, AutoMod, Branding, Willkommens-Nachrichten … einfach alles, "
            "was dein Server braucht."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "**So startest du (dauert keine 2 Minuten):**\n"
            f"1. Geh auf deinen Server **{guild.name}**\n"
            "2. Nutze den Command `/connect`\n"
            "3. Klicke auf **Verbinden** und schick Arena AI den Prompt aus der "
            "Nachricht — dann verstehst du schon alles."
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "Es geht wirklich schnell — und für die Einrichtung deines Servers "
            "spare ich dir massig Zeit. 🚀"
        ),
        accent_colour=WELCOME_ACCENT,
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view



# ─────────────────────────────────────────────────────────────────────────────
#  Client
# ─────────────────────────────────────────────────────────────────────────────


class RelayClient(discord.Client):
    """Discord-Client mit Slash-Command-Tree und Relay-Anbindung."""

    def __init__(
        self,
        config: Config,
        store: SessionStore,
        state: Any = None,
        *,
        intents: Optional[discord.Intents] = None,
    ) -> None:
        """
        ``state`` darf zunächst ``None`` sein.

        Client und ``AppState`` verweisen aufeinander; ``main.amain`` erzeugt
        deshalb erst den Client und setzt ``client.state`` direkt danach.
        """
        options: Dict[str, Any] = {"intents": intents or build_intents(True)}
        proxy = (getattr(config, "discord_proxy", "") or "").strip()
        if proxy:
            # discord.py reicht den Proxy an REST **und** Gateway weiter — genau
            # das brauchen wir, wenn die ausgehende IP des Containers von
            # Cloudflare gesperrt ist (Error 1015).
            options["proxy"] = proxy
            user = (getattr(config, "discord_proxy_user", "") or "").strip()
            if user:
                options["proxy_auth"] = aiohttp.BasicAuth(
                    user, getattr(config, "discord_proxy_password", "") or ""
                )
            log.info("Discord-Traffic läuft über Proxy %s", mask_proxy_url(proxy))
        super().__init__(**options)
        self.config = config
        self.store = store
        self.state = state
        self.tree = app_commands.CommandTree(self)
        self.ready_at: Optional[Any] = None
        self.tree.on_error = self._on_command_error  # type: ignore[assignment]

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        self._register_commands()
        # Persistente Button-View registrieren: ``timeout=None`` plus diese
        # Registrierung sorgt dafür, dass Klicks auf ausgelieferte Nachrichten
        # auch nach einem Render-Deploy noch dispatched werden.
        self.add_view(_button_registration_view())
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """
        Registriert die Slash-Commands — höchstens einmal pro
        ``COMMAND_SYNC_MIN_INTERVAL``.

        Zwei Gründe für die Bremse:

        1. Ein globaler Sync ist ein eigener Rate-Limit-Bucket. Bei jedem
           Reconnect erneut zu syncen bringt nichts (die Commands stehen schon
           serverseitig), erzeugt aber zusätzliche Anfragen — genau die wollen
           wir bei einer Cloudflare-Sperre der IP vermeiden.
        2. Ein fehlgeschlagener Sync darf den Bot niemals in einen Reconnect-
           Loop zwingen: bereits registrierte Commands bleiben gültig.
        """
        last = getattr(self.state, "commands_synced_at", None)
        now = now_utc()
        if last is not None and (now - last).total_seconds() < COMMAND_SYNC_MIN_INTERVAL:
            log.debug("Slash-Commands wurden vor %.0f s registriert — Sync übersprungen.",
                      (now - last).total_seconds())
            return
        try:
            await self.tree.sync()
        except Exception as exc:  # noqa: BLE001 — HTTPException, RateLimited, Sync-Fehler
            log.warning("Slash-Command-Sync fehlgeschlagen (%s) — Bot läuft weiter, "
                        "bereits registrierte Commands bleiben aktiv.", exc)
            return
        if self.state is not None:
            self.state.commands_synced_at = now
        log.info("Slash-Commands global registriert (Sync kann bis zu 1 Stunde dauern; "
                 "auf bestehenden Servern meist sofort).")

    async def on_ready(self) -> None:
        self.ready_at = now_utc()
        # Login geglückt → Health-Status zurück auf „online“, alte Fehlermeldung weg.
        self.state.discord_status = "online"
        self.state.discord_last_error = None
        self.state.discord_retry_at = None
        self.state.platform_verdict = None
        # Erfolgreicher Login ⇒ Neustart-Zähler (IP-Sperre) zurücksetzen.
        ledger = getattr(self.state, "restart_ledger", None)
        if ledger is not None:
            try:
                ledger.clear(reason="Login erfolgreich")
            except Exception as exc:  # noqa: BLE001 — Buchführung darf on_ready nie stören
                log.debug("Neustart-Buch nicht zurücksetzbar: %s", exc)
        user = self.user
        guild_names = ", ".join(g.name for g in self.guilds[:8]) or "(keine)"
        log.info("═" * 68)
        log.info("  %s v%s ist ONLINE", self.config.bot_name, __version__)
        log.info("  Bot        : %s (ID %s)", getattr(user, "name", "?"), getattr(user, "id", "?"))
        log.info("  Server     : %d — %s", len(self.guilds), guild_names)
        log.info("  Öffentliche URL : %s", self.state.base_url())
        log.info("  Console    : %s/console", self.state.base_url())
        log.info("  Healthcheck: %s/api/health", self.state.base_url())
        if user:
            log.info("  Invite (Admin): %s", self.admin_invite_url())
        log.info("═" * 68)

        try:
            await self._update_presence()
        except discord.HTTPException as exc:
            log.warning("Presence konnte nicht gesetzt werden: %s", exc)

        self._check_guild_permissions()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Server beigetreten: %s (%d Mitglieder, ID %s)",
                 guild.name, guild.member_count, guild.id)
        self._check_guild_permissions()
        await self._update_presence()
        # Owner informieren (Bot-Owner per DM) und dem Server-Owner die
        # Kurzanleitung schicken — beides darf den Join niemals stören.
        try:
            await self._notify_owner_join(guild)
        except Exception as exc:  # noqa: BLE001
            log.warning("Join-Benachrichtigung fehlgeschlagen: %s", exc)
        try:
            await self._welcome_server_owner(guild)
        except Exception as exc:  # noqa: BLE001
            log.warning("Willkommens-DM fehlgeschlagen: %s", exc)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Server verlassen: %s (ID %s)", guild.name, guild.id)
        await self._update_presence()
        try:
            await self._notify_owner_remove(guild)
        except Exception as exc:  # noqa: BLE001
            log.warning("Leave-Benachrichtigung fehlgeschlagen: %s", exc)

    async def on_resumed(self) -> None:
        log.info("Gateway-Verbindung wiederhergestellt.")

    # ── Helfer ───────────────────────────────────────────────────────────────
    def admin_invite_url(self) -> str:
        """Invite-Link, der dem Bot Administrator + Slash-Commands gibt."""
        if not self.user:
            return ""
        return discord.utils.oauth_url(
            self.user.id,
            permissions=discord.Permissions(administrator=True),
            scopes=ADMIN_INVITE_SCOPES,
        )

    def _check_guild_permissions(self) -> None:
        """Warnt im Log, wenn der Bot irgendwo kein Administrator ist."""
        for guild in self.guilds:
            me = guild.me
            if me is None:
                continue
            if not me.guild_permissions.administrator:
                missing = [
                    name for name, value in me.guild_permissions
                    if not value and name in {"manage_guild", "manage_channels", "manage_roles",
                                              "kick_members", "ban_members", "moderate_members",
                                              "manage_messages", "view_audit_log"}
                ]
                log.warning(
                    "⚠ Auf '%s' (ID %s) ist der Bot KEIN Administrator. Fehlend u. a.: %s. "
                    "→ Servereinstellungen → Rollen → Bot-Rolle auf 'Administrator' "
                    "und ganz nach oben schieben. /connect verweigert sonst die Arbeit.",
                    guild.name, guild.id, ", ".join(sorted(missing)) or "viele",
                )

    def _base_url_warning(self) -> Optional[str]:
        base = self.state.base_url()
        if base.startswith(("http://127.", "http://localhost", "http://0.0.0.0")):
            return (
                f"⚠️ Die öffentliche URL ist nicht gesetzt (`{base}`). Auf Render wird "
                "`RENDER_EXTERNAL_URL` automatisch geliefert — lokal bitte `PUBLIC_URL` "
                "setzen, sonst funktioniert der Link für Arena AI nicht."
            )
        return None

    # ── Presence & Owner-Benachrichtigungen ──────────────────────────────────
    def _presence_text(self) -> str:
        """
        Der Live-Status: ``/connect | 👀 N eingerichtete Server``.

        Die Zahl ist die Anzahl der Server, auf denen der Bot ist — sie wird
        bei jedem Join/Leave aktualisiert.
        """
        count = len(self.guilds)
        server_word = "eingerichteter Server" if count == 1 else "eingerichtete Server"
        return f"/{self.config.command_name} | 👀 {count} {server_word}"

    async def _update_presence(self) -> None:
        """Setzt den Live-Status (Aufruf bei on_ready und jedem Server-Wechsel)."""
        presence = discord.Activity(type=discord.ActivityType.playing, name=self._presence_text())
        status = getattr(discord.Status, self.config.status_presence, discord.Status.online)
        await self.change_presence(activity=presence, status=status)

    def _bot_owner(self) -> Optional[discord.abc.User]:
        """Der Bot-Owner als User-Objekt (Cache, sonst REST — darf None sein)."""
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        if not owner_id:
            return None
        return self.get_user(owner_id)

    async def _dm_user(self, user: Optional[discord.abc.User], view: discord.ui.LayoutView,
                       *, context: str) -> bool:
        """
        Schickt eine Container-V2-DM — robust gegen geschlossene DMs.

        Geschlossene DMs oder andere Fehler dürfen den Ablauf niemals abreißen
        lassen; es wird nur geloggt, ob es geklappt hat.
        """
        if user is None:
            log.debug("DM (%s) übersprungen — Zielnutzer unbekannt.", context)
            return False
        try:
            await user.send(view=view)
            return True
        except discord.Forbidden:
            log.info("DM (%s) an %s nicht möglich — DMs sind geschlossen.",
                     context, getattr(user, "id", "?"))
        except discord.HTTPException as exc:
            log.warning("DM (%s) an %s fehlgeschlagen: %s", context, getattr(user, "id", "?"), exc)
        return False

    async def _notify_owner_join(self, guild: discord.Guild) -> None:
        """DM an den Bot-Owner: Name, Mitgliederzahl, Icon, Owner-Mention."""
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        if not owner_id:
            return
        bot_owner = self._bot_owner() or None
        guild_owner = guild.owner
        if guild_owner is None:
            try:
                guild_owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                guild_owner = None
        sent = await self._dm_user(
            bot_owner, guild_join_notify_view(guild, guild_owner),
            context=f"Join-Info {guild.id}",
        )
        if not sent:
            log.info("Join auf '%s' (ID %s, %s Mitglieder) konnte nicht per DM gemeldet werden.",
                     guild.name, guild.id, guild.member_count)

    async def _welcome_server_owner(self, guild: discord.Guild) -> None:
        """
        DM an den Server-Owner direkt nach dem Beitritt: die Kurzanleitung.

        Das ist die wichtigste DM überhaupt — sie erklärt, wie man Arena AI
        auf dem eigenen Server arbeiten lässt (``/connect`` → Verbinden).
        """
        target = guild.owner
        if target is None:
            try:
                target = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                target = None
        await self._dm_user(target, server_owner_welcome_view(guild),
                             context=f"Willkommen {guild.id}")

    async def _leave_reason(self, guild: discord.Guild) -> str:
        """
        Bestmöglicher Grund, warum der Bot den Server verlassen hat.

        Ehrlich gesagt: Nach dem Entfernen hat der Bot **keinen Zugriff mehr**
        auf das Audit-Log des Servers — der Grund ist deshalb meist nicht
        ermittelbar. Der Versuch kostet aber nichts (kurzes Zeitfenster, bis
        Discord die Rechte durchzieht), und wenn er durchgeht, gibt es den
        echten Grund inklusive Ausführendem.
        """
        await asyncio.sleep(1.5)  # Audit-Einträge brauchen einen Moment zum Anlegen
        try:
            async for entry in guild.audit_logs(limit=15):
                target_id = getattr(getattr(entry, "target", None), "id", None)
                if target_id != (self.user.id if self.user else -1):
                    continue
                if entry.action in (discord.AuditLogAction.kick, discord.AuditLogAction.ban):
                    action = "gekickt" if entry.action is discord.AuditLogAction.kick else "gebannt"
                    executor = getattr(entry, "user", None)
                    by = f" von **{executor}**" if executor else ""
                    reason = entry.reason or "kein Grund angegeben"
                    return f"Bot wurde {action}{by} — Grund: {reason}"
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        except Exception as exc:  # noqa: BLE001 — der Grund darf nie crashen
            log.debug("Audit-Log-Lookup nach Leave fehlgeschlagen: %s", exc)
        return (
            "nicht ermittelbar — nach dem Entfernen hat der Bot keinen Zugriff mehr auf "
            "das Audit-Log des Servers. Typische Ursachen: gekickt (ggf. mit Grund), "
            "gebannt oder der Server wurde gelöscht."
        )

    async def _notify_owner_remove(self, guild: discord.Guild) -> None:
        """DM an den Bot-Owner: Name, Mitgliederzahl, Icon, Owner-Mention + Grund."""
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        if not owner_id:
            return
        bot_owner = self._bot_owner() or None
        reason = await self._leave_reason(guild)
        sent = await self._dm_user(
            bot_owner, guild_remove_notify_view(guild, guild.owner, reason),
            context=f"Leave-Info {guild.id}",
        )
        if not sent:
            log.warning(
                "Verlassen von '%s' (ID %s) konnte nicht per DM gemeldet werden. Grund: %s",
                guild.name, guild.id, reason,
            )

    # ── Adminpanel ───────────────────────────────────────────────────────────
    async def _collect_admin_guilds(self) -> List[Dict[str, Any]]:
        """
        Alle Server des Bots als sortierte Einträge fürs Adminpanel.

        Sortierung: Server, auf denen der Bot-Owner **nicht** Mitglied ist,
        kommen zuerst (im Panel mit ❗️), danach jeweils Mitglieder abwärts.

        Ist der Mitglieder-Cache vollständig (privilegiertes Intent, guild ist
        ``chunked``), genügt ein Blick in den Cache; sonst wird pro Server
        einmal per REST nachgeprüft.
        """
        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        entries: List[Dict[str, Any]] = []
        for guild in self.guilds:
            owner_present = owner_id != 0 and guild.get_member(owner_id) is not None
            if not owner_present and owner_id and not guild.chunked:
                try:
                    await guild.fetch_member(owner_id)
                    owner_present = True
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    owner_present = False
            entries.append({
                "guild": guild,
                "member_count": guild.member_count or 0,
                "owner_present": owner_present,
            })
        entries.sort(key=lambda e: (e["owner_present"], -e["member_count"]))
        return entries

    async def _admin_panel_view(self, *, page: int = 0, query: str = "") -> discord.ui.LayoutView:
        """Sammelt die Serverdaten und baut das Panel (Suchfilter inklusive)."""
        entries = await self._collect_admin_guilds()
        if query:
            lowered = query.strip().lower()
            entries = [e for e in entries if lowered in e["guild"].name.lower()]
        return admin_panel_view(entries, page=page, query=query.strip(), total_guilds=len(self.guilds))

    async def _guild_detail_view(
        self,
        guild_or_id: Any,
        *,
        page: int = 0,
        query: str = "",
        invite_url: Optional[str] = None,
        notice: Optional[str] = None,
    ) -> discord.ui.LayoutView:
        """Baut die Detailansicht eines Servers für das Adminpanel."""
        if isinstance(guild_or_id, int):
            guild = self.get_guild(guild_or_id)
        else:
            guild = guild_or_id
        if guild is None:
            return await self._admin_panel_view(page=page, query=query)

        owner_id = int(getattr(self.config, "bot_owner_id", 0) or 0)
        owner_present = owner_id != 0 and guild.get_member(owner_id) is not None
        if not owner_present and owner_id and not getattr(guild, "chunked", True):
            try:
                await guild.fetch_member(owner_id)
                owner_present = True
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                owner_present = False

        guild_owner = getattr(guild, "owner", None)
        if guild_owner is None and hasattr(guild, "owner_id") and hasattr(guild, "fetch_member"):
            try:
                guild_owner = await guild.fetch_member(guild.owner_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                guild_owner = None

        sessions = self.store.active_for_guild(guild.id)
        return admin_guild_detail_view(
            guild,
            owner=guild_owner,
            owner_present=owner_present,
            sessions=sessions,
            page=page,
            query=query,
            invite_url=invite_url,
            notice=notice,
        )

    async def _handle_admin_button(self, interaction: discord.Interaction, custom_id: str) -> None:
        """Adminpanel-Interaktionen: Blättern, Suchen, Detailansicht, Invite, Leave."""
        if interaction.guild is not None:
            return  # Panel lebt ausschließlich im Privatchat
        if int(getattr(self.config, "bot_owner_id", 0) or 0) == 0:
            return
        if interaction.user.id != self.config.bot_owner_id:
            log.warning("Adminpanel-Klick von %s — nicht der Bot-Owner.",
                        getattr(interaction.user, "id", "?"))
            return

        parsed = _parse_admin_custom_id(custom_id)
        if parsed is None:
            return

        action = parsed["action"]
        page = parsed.get("page", 0)
        query = parsed.get("query", "")

        if action == "close":
            # Das Panel ist eine normale DM. Erst den Klick bestätigen, dann
            # die Panel-Nachricht vollständig entfernen.
            try:
                await interaction.response.defer()
                message = getattr(interaction, "message", None)
                if message is not None:
                    await message.delete()
                else:
                    await interaction.delete_original_response()
            except (discord.HTTPException, discord.NotFound, discord.InteractionResponded) as exc:
                log.warning("Adminpanel konnte nicht geschlossen werden: %s", exc)
            return

        if action == "search":
            await interaction.response.send_modal(AdminSearchModal(self))
            return

        if action == "select":
            values = (interaction.data or {}).get("values") or []
            if values:
                try:
                    guild_id = int(values[0])
                    view = await self._guild_detail_view(guild_id, page=page, query=query)
                    await self._show(interaction, view)
                    return
                except ValueError:
                    pass
            view = await self._admin_panel_view(page=page, query=query)
            await self._show(interaction, view)
            return

        if action == "guild":
            guild_id = parsed.get("guild_id")
            if guild_id is not None:
                view = await self._guild_detail_view(guild_id, page=page, query=query)
                await self._show(interaction, view)
            else:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
            return

        if action == "invite":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return

            invite_url: Optional[str] = None
            notice: Optional[str] = None
            try:
                # 1. Bevorzugt guild.invites.create() falls implementiert
                if hasattr(guild, "invites") and hasattr(guild.invites, "create"):
                    inv = await guild.invites.create(max_uses=1, max_age=3600, reason="Adminpanel-Einladung für den Bot-Owner")
                    invite_url = getattr(inv, "url", str(inv))
                else:
                    # 2. Suche nach passendem Kanal
                    target_channel = getattr(guild, "system_channel", None) or getattr(guild, "rules_channel", None)
                    if target_channel is None or not hasattr(target_channel, "create_invite"):
                        for c in getattr(guild, "text_channels", []) or []:
                            if hasattr(c, "create_invite"):
                                target_channel = c
                                break
                    if target_channel is None or not hasattr(target_channel, "create_invite"):
                        for c in getattr(guild, "channels", []) or []:
                            if hasattr(c, "create_invite"):
                                target_channel = c
                                break
                    if target_channel is None or not hasattr(target_channel, "create_invite"):
                        notice = "Kein Kanal gefunden, um eine Einladung zu erstellen."
                    else:
                        inv = await target_channel.create_invite(
                            max_uses=1, max_age=3600, reason="Adminpanel-Einladung für den Bot-Owner"
                        )
                        invite_url = getattr(inv, "url", str(inv))
            except discord.Forbidden:
                notice = "Fehlende Berechtigung: Dem Bot fehlt 'Sofortige Einladung erstellen' (create_instant_invite) auf diesem Server."
            except Exception as exc:
                notice = f"Einladung konnte nicht erstellt werden: {exc}"

            view = await self._guild_detail_view(
                guild, page=page, query=query, invite_url=invite_url, notice=notice
            )
            await self._show(interaction, view)
            return

        if action == "leave":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return
            view = admin_guild_leave_confirm_view(guild, page=page, query=query)
            await self._show(interaction, view)
            return

        if action == "leave_confirm":
            guild_id = parsed.get("guild_id")
            guild = self.get_guild(guild_id) if guild_id else None
            if guild is None:
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
                return
            try:
                await guild.leave()
                log.info("Bot hat Server '%s' (ID %s) über das Adminpanel verlassen.",
                         guild.name, guild.id)
                if hasattr(self, "_connection") and hasattr(self._connection, "_guilds"):
                    self._connection._guilds.pop(guild.id, None)
                view = await self._admin_panel_view(page=page, query=query)
                await self._show(interaction, view)
            except Exception as exc:
                log.warning("Server '%s' (ID %s) konnte nicht verlassen werden: %s",
                            guild.name, guild.id, exc)
                view = await self._guild_detail_view(
                    guild, page=page, query=query,
                    notice=f"Server konnte nicht verlassen werden: {exc} (z. B. wenn der Bot Server-Owner ist)",
                )
                await self._show(interaction, view)
            return

        # nav, back oder sonstige Navigation
        view = await self._admin_panel_view(page=page, query=query)
        await self._show(interaction, view)

    async def _new_session(self, guild: discord.Guild, member: discord.Member, *, note: str):
        """
        Legt eine Sitzung mit den Defaults aus der Konfiguration an
        (Gültigkeit = ``SESSION_TTL_HOURS``, Modus = Lesen + Schreiben) und
        gibt ``(session, plaintext_token)`` zurück.
        """
        return await self.store.create(
            guild_id=guild.id,
            guild_name=guild.name,
            created_by=member.id,
            created_by_name=member.display_name,
            mode=DEFAULT_MODE,
            ttl_hours=self.config.session_ttl_hours,
            note=note,
        )

    # ── Command-Registrierung ────────────────────────────────────────────────
    def _register_commands(self) -> None:
        command_name = self.config.command_name

        @self.tree.command(
            name=command_name,
            description="🔗 Mit Arena AI verbinden (nur für dich sichtbar)",
        )
        @app_commands.guild_only()
        async def connect(interaction: discord.Interaction) -> None:
            """Der Haupt-Command: zeigt die Willkommens-Nachricht bzw. den verbundenen Zustand."""
            gate = await self._guard(interaction)
            if gate is None:
                return
            guild = gate["guild"]
            member = gate["member"]

            if self.store.active_for_guild(guild.id):
                # Bereits verbunden → verbundenen Zustand zeigen. Der Klartext
                # des alten Tokens liegt nicht mehr vor (gespeichert ist nur
                # sein Hash) — deshalb gibt es ein frisches Token; die alten
                # bleiben bis zu ihrem Ablauf gültig.
                session, token = await self._new_session(
                    guild, member, note="per /connect (bereits verbunden) erneuert"
                )
                self.state.maybe_save(force=True)
                try:
                    await interaction.response.send_message(
                        view=connected_view(self.state.base_url(), token), ephemeral=True,
                    )
                except discord.HTTPException as exc:
                    log.error("Antwort auf /%s fehlgeschlagen: %s", self.config.command_name, exc)
                    return
                log.info(
                    "/%s (bereits verbunden) von %s (%s) auf '%s' — frisches Token %s für Sitzung %s",
                    self.config.command_name, member.display_name, member.id, guild.name,
                    session.token_prefix, session.id,
                )
            else:
                # Noch nicht verbunden → Willkommen mit grünem Verbinden-Button.
                try:
                    await interaction.response.send_message(view=welcome_view(), ephemeral=True)
                except discord.HTTPException as exc:
                    log.error("Antwort auf /%s fehlgeschlagen: %s", self.config.command_name, exc)
                    return
                log.info(
                    "/%s (Willkommen, Bridge deaktiviert) von %s (%s) auf '%s'.",
                    self.config.command_name, member.display_name, member.id, guild.name,
                )

        @self.tree.command(
            name="adminpanel",
            description="🛠️ Server-Übersicht für den Bot-Owner (nur im Privatchat)",
        )
        @app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
        async def adminpanel(interaction: discord.Interaction) -> None:
            """Das Adminpanel: Server-Liste mit Suche und Blättern — Owner-only."""
            if interaction.guild is not None:
                await self._deny(
                    interaction, "Nur im Privatchat",
                    "Das Adminpanel funktioniert ausschließlich im **Privatchat mit dem Bot** — "
                    "die Serverliste ist nichts für öffentliche Channels.",
                )
                return
            if not self.config.bot_owner_id or interaction.user.id != self.config.bot_owner_id:
                await self._deny(
                    interaction, "Nur für den Bot-Owner",
                    "Dieser Command ist reserviert für den Besitzer dieses Bots.",
                )
                log.warning("adminpanel-Versuch von %s (%s) — nicht der Bot-Owner.",
                            interaction.user.name, interaction.user.id)
                return

            # Das Sammeln der Serverdaten kann (ohne privilegierte Intents)
            # ein paar REST-Aufrufe kosten — erst deferigen, dann liefern.
            await interaction.response.defer()
            view = await self._admin_panel_view(page=0, query="")
            try:
                await interaction.followup.send(view=view)
            except discord.HTTPException as exc:
                log.error("Adminpanel konnte nicht gesendet werden: %s", exc)

        # Hinweis: pro-Command on_error ist hier NICHT nötig — discord.py ruft
        # bei einem Fehler SOWOHL command.on_error ALS AUCH tree.on_error auf
        # (siehe CommandTree._dispatch_error). tree.on_error ist bereits in
        # __init__ gesetzt, daher würden diese Zuweisungen jede Fehlermeldung
        # doppelt senden. Bewusst weggelassen.

        # Kein add_listener() hier: das gibt es nur auf commands.Bot, nicht auf
        # discord.Client. Button-Klicks bedient stattdessen die Methode
        # ``on_interaction`` weiter unten — discord.py ruft für JEDE
        # Interaktion getattr(self, "on_interaction") auf.

    # ── Rechteprüfung ────────────────────────────────────────────────────────
    async def _guard(
        self, interaction: discord.Interaction, *, require_bot_admin: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Zentrale Prüfung vor jedem Command und Button-Klick.

        Liefert ``None``, wenn bereits eine Fehlerantwort gesendet wurde,
        sonst ``{"guild": …, "member": …}``.
        """
        guild = interaction.guild
        member = interaction.user

        if guild is None:
            await self._deny(
                interaction, "Nur auf einem Server",
                "Dieser Command funktioniert ausschließlich in einem Discord-Server, "
                "nicht in Direktnachrichten.",
            )
            return None

        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await self._deny(
                interaction, "Administrator-Rechte erforderlich",
                "Nur Mitglieder mit **Administrator**-Berechtigung dürfen einen KI-Zugriff "
                "freischalten.\n\n"
                "→ Servereinstellungen → Rollen → deine Rolle → *Administrator* aktivieren.",
                color=ERR_COLOR,
            )
            log.warning("%s (%s) versuchte /%s auf '%s' ohne Administrator-Rechte.",
                        getattr(member, "display_name", "?"), getattr(member, "id", "?"),
                        self.config.command_name, guild.name)
            return None

        if require_bot_admin:
            me = guild.me
            if me is None or not me.guild_permissions.administrator:
                invite = self.admin_invite_url()
                await self._deny(
                    interaction, "Der Bot braucht Administrator-Rechte",
                    "Damit Arena AI den Server wirklich komplett einrichten kann, muss "
                    "**dieser Bot Administrator** sein — genau wie du.\n\n"
                    "**So geht's:**\n"
                    "1. Servereinstellungen → Rollen → **"
                    f"{self.user.name if self.user else 'AIDiscordServerEinrichten'}**\n"
                    "2. *Administrator* aktivieren **und** die Rolle ganz nach oben schieben\n"
                    "3. `/connect` erneut ausführen"
                    + (f"\n\n**Oder Bot neu einladen:** {invite}" if invite else ""),
                    color=WARN_COLOR,
                )
                return None

        return {"guild": guild, "member": member}

    async def _deny(
        self, interaction: discord.Interaction, title: str, description: str,
        *, color: discord.Color = ERR_COLOR,
    ) -> None:
        embed = discord.Embed(title=f"⛔ {title}", description=description[:4000], color=color)
        embed.set_footer(text=self.config.bot_name)
        embed.timestamp = now_utc()
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException as exc:
            log.error("Fehlerantwort konnte nicht gesendet werden: %s", exc)

    # ── Button-Handler ───────────────────────────────────────────────────────
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """
        Wird von discord.py für JEDE Interaktion gerufen; Slash-Commands
        gehen über den CommandTree, hier zählen nur Komponenten-Klicks.

        Neue Components-V2-Buttons und die Legacy-IDs vor dem Upgrade werden
        beide bedient — alte Nachrichten laufen so nicht ins Leere.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")

        if custom_id == CID_CONNECT:
            await self._handle_connect(interaction)
        elif custom_id in (CID_DISCONNECT, CID_LEGACY_REVOKE_ALL):
            await self._handle_revoke_all(interaction)
        elif custom_id in (CID_REGENERATE, CID_LEGACY_REGENERATE):
            await self._handle_regenerate(interaction)
        elif custom_id.startswith(CID_ADMIN_PREFIX):
            await self._handle_admin_button(interaction, custom_id)

    async def _handle_connect(self, interaction: discord.Interaction) -> None:
        """Grüner „Verbinden“-Button: erzeugt das Token und bearbeitet die Nachricht."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        member = gate["member"]

        session, token = await self._new_session(guild, member, note="per Verbinden-Button erzeugt")
        self.state.maybe_save(force=True)
        await self._show(interaction, connected_view(self.state.base_url(), token))
        log.info(
            "Verbinden-Button von %s (%s) auf '%s' — Sitzung %s, Token %s, Modus %s.",
            member.display_name, member.id, guild.name, session.id,
            session.token_prefix, session.mode,
        )

    async def _handle_revoke_all(self, interaction: discord.Interaction) -> None:
        """Roter „Verbindung trennen“-Button: alle Tokens widerrufen, zurück zum Willkommen."""
        gate = await self._guard(interaction, require_bot_admin=False)
        if gate is None:
            return
        guild = gate["guild"]
        revoked = await self.store.revoke(guild_id=guild.id, by=f"button:{interaction.user.id}")
        self.state.maybe_save(force=True)
        await self._show(interaction, welcome_view())
        log.info(
            "Verbindung getrennt: %s (%s) hat %d Sitzung(en) auf '%s' widerrufen.",
            gate["member"].display_name, interaction.user.id, len(revoked), guild.name,
        )

    async def _handle_regenerate(self, interaction: discord.Interaction) -> None:
        """„Neues Token generieren“-Button: alte Tokens ungültig, frisches Token zeigen."""
        gate = await self._guard(interaction)
        if gate is None:
            return
        guild = gate["guild"]
        member = gate["member"]

        await self.store.revoke(guild_id=guild.id, by=f"button:{interaction.user.id}")
        session, token = await self._new_session(guild, member, note="per Button neu generiert")
        self.state.maybe_save(force=True)
        await self._show(interaction, connected_view(self.state.base_url(), token))
        log.info(
            "Neues Token generiert: %s (%s) auf '%s' — Sitzung %s, Token %s "
            "(alte Tokens dieser Guild sind ungültig).",
            member.display_name, member.id, guild.name, session.id, session.token_prefix,
        )

    async def _show(self, interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
        """
        Bearbeitet die Container-V2-Nachricht, auf der der Button sitzt.

        Components-V2-Nachrichten haben keinen ``content`` — beim Umstylen
        müssen ``content``/``embeds``/``attachments`` explizit geleert werden
        (sonst bleibt Alter Content stehen bzw. Discord lehnt ab). Klappt das
        Bearbeiten nicht mehr (Nachricht weg/zu alt), gibt es eine frische
        ephemeral Nachricht als Fallback.
        """
        try:
            await interaction.response.edit_message(
                view=view, content=None, embeds=[], attachments=[],
            )
            return
        except (discord.HTTPException, discord.InteractionResponded) as exc:
            log.warning("Nachricht konnte nicht bearbeitet werden (%s) — sende eine neue.", exc)
        try:
            await interaction.followup.send(view=view, ephemeral=True)
        except discord.HTTPException as exc:
            log.error("Ersatznachricht konnte nicht gesendet werden: %s", exc)

    # ── Fehlerbehandlung ─────────────────────────────────────────────────────
    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        inner = getattr(error, "__cause__", error)

        if isinstance(error, app_commands.MissingPermissions):
            await self._deny(
                interaction, "Administrator-Rechte erforderlich",
                "Dieser Command ist nur für Mitglieder mit **Administrator**-Berechtigung.",
            )
            return
        if isinstance(error, app_commands.BotMissingPermissions):
            await self._deny(
                interaction, "Dem Bot fehlen Rechte",
                f"Bitte gib dem Bot: {', '.join(error.missing_permissions)}",
                color=WARN_COLOR,
            )
            return
        if isinstance(error, app_commands.NoPrivateMessage):
            await self._deny(interaction, "Nur auf einem Server",
                             "Dieser Command funktioniert nicht in Direktnachrichten.")
            return
        if isinstance(error, app_commands.CommandOnCooldown):
            await self._deny(
                interaction, "Zu schnell",
                f"Bitte warte {error.retry_after:.1f} Sekunden.",
                color=WARN_COLOR,
            )
            return
        if isinstance(inner, ApiError):
            await self._deny(interaction, inner.code, f"{inner.message}\n\n{inner.hint or ''}",
                             color=WARN_COLOR)
            return

        log.exception("Command-Fehler", exc_info=inner)
        await self._deny(
            interaction, "Interner Fehler",
            f"```{type(inner).__name__}: {str(inner)[:600]}```\n"
            "Details stehen im Render-Log.",
            color=ERR_COLOR,
        )
