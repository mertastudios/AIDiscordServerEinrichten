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

Dazu kommen zwei Sicherheits-Commands: ``/status`` (Rechte-Check) und
``/revoke`` (Zugriff sofort entziehen).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands

from . import __version__
from .config import Config, mask_proxy_url
from .sessions import DEFAULT_MODE, SessionStore
from .util import ApiError, human_duration, now_utc

log = logging.getLogger("relay.discord")

__all__ = ("RelayClient", "build_intents", "ADMIN_INVITE_SCOPES")

ADMIN_INVITE_SCOPES = ("bot", "applications.commands")

#: Mindestabstand zwischen zwei globalen Slash-Command-Syncs (Sekunden).
COMMAND_SYNC_MIN_INTERVAL = 3600.0

OK_COLOR = discord.Color.from_str("#2ECC71")
WARN_COLOR = discord.Color.from_str("#F1C40F")
ERR_COLOR = discord.Color.from_str("#E74C3C")

#: Ziel des „Arena AI öffnen“-Link-Buttons.
ARENA_URL = "https://arena.ai"

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


def _bridge_prompt_block(base_url: str, token: str) -> str:
    """
    Der Mini-Prompt für Arena AI: **nur** Verbindung und Einstieg.

    Der komplette Regel- und Workflow-Katalog (Hygiene, Personas, Branding,
    Unicode-Design …) muss der Nutzer nicht mehr kopieren — die Bridge erklärt
    sich Arena AI selbst, sobald diese arbeitet: ``GET /api/v1/capabilities``
    liefert Konventionen und Endpoints, dazu die ``/api/v1/guides/*``-Texte.
    """
    return (
        "Discord-Bridge für Arena AI\n"
        f"URL: {base_url}\n"
        f"TOKEN: {token}\n"
        f'Start: GET {base_url}/api/v1/capabilities mit Header "Authorization: Bearer TOKEN"'
    )


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
            f"```\n{_bridge_prompt_block(base_url, token)}\n```"
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
            presence = discord.Activity(
                type=discord.ActivityType.playing, name=self.config.activity_text
            )
            status = getattr(discord.Status, self.config.status_presence, discord.Status.online)
            await self.change_presence(activity=presence, status=status)
        except discord.HTTPException as exc:
            log.warning("Presence konnte nicht gesetzt werden: %s", exc)

        self._check_guild_permissions()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Server beigetreten: %s (%d Mitglieder, ID %s)",
                 guild.name, guild.member_count, guild.id)
        self._check_guild_permissions()

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
            name="status",
            description="🩺 Rechte-Check: Bot, Server, aktive KI-Zugriffe",
        )
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction) -> None:
            gate = await self._guard(interaction, require_bot_admin=False)
            if gate is None:
                return
            guild = gate["guild"]
            me = guild.me
            perms = me.guild_permissions if me else discord.Permissions.none()
            sessions = self.store.active_for_guild(guild.id)
            base = self.state.base_url()

            embed = discord.Embed(
                title=f"🩺 {self.config.bot_name} · Status",
                color=OK_COLOR if perms.administrator else WARN_COLOR,
                timestamp=now_utc(),
            )
            embed.add_field(
                name="🤖 Bot",
                value=f"{self.user.name if self.user else '?'} · `{self.user.id if self.user else '?'}`\n"
                      f"Version {__version__} · discord.py {discord.__version__}",
                inline=False,
            )
            embed.add_field(
                name="🔑 Administrator",
                value="✅ Ja — alles möglich" if perms.administrator
                      else "❌ **Nein** — bitte Bot-Rolle auf Administrator setzen",
                inline=True,
            )
            embed.add_field(
                name="🛡️ Fehlende Rechte",
                value="keine" if perms.administrator else _missing_permissions_text(perms),
                inline=True,
            )
            embed.add_field(
                name="🌐 Öffentliche URL",
                value=f"[{base}]({base})",
                inline=False,
            )
            embed.add_field(
                name="🔗 Aktive KI-Zugriffe",
                value=_sessions_text(sessions) or "keine",
                inline=False,
            )
            embed.add_field(
                name="📊 Betrieb",
                value=f"Uptime {human_duration(self.state.uptime_seconds())} · "
                      f"{self.state.request_count} API-Aufrufe · "
                      f"{self.state.error_count} Fehler · Gateway "
                      f"{round(self.latency * 1000)} ms",
                inline=False,
            )
            if self.admin_invite_url():
                embed.add_field(
                    name="➕ Bot neu einladen (mit Administrator)",
                    value=self.admin_invite_url(),
                    inline=False,
                )
            warn = self._base_url_warning()
            if warn:
                embed.add_field(name="⚠️ Hinweis", value=warn[:1020], inline=False)
            embed.set_footer(text="AIDiscordServerEinrichten · Relay für Arena AI")

            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(
            name="revoke",
            description="⛔ Alle aktiven KI-Zugriffe (Tokens) für diesen Server widerrufen",
        )
        @app_commands.guild_only()
        async def revoke(interaction: discord.Interaction) -> None:
            gate = await self._guard(interaction, require_bot_admin=False)
            if gate is None:
                return
            guild = gate["guild"]
            member = gate["member"]
            sessions = self.store.active_for_guild(guild.id)
            if not sessions:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        description="ℹ️ Es gibt gerade keine aktiven KI-Zugriffe auf diesem Server.",
                        color=OK_COLOR,
                    ),
                    ephemeral=True,
                )
                return
            revoked = await self.store.revoke(guild_id=guild.id, by=f"discord:{member.id}")
            self.state.maybe_save(force=True)
            embed = discord.Embed(
                title="⛔ Zugriff widerrufen",
                description=(
                    f"**{len(revoked)} Token** sofort deaktiviert. Arena AI hat damit "
                    "keinen Zugriff mehr auf diesen Server."
                ),
                color=ERR_COLOR,
                timestamp=now_utc(),
            )
            embed.add_field(
                name="Betroffene Sitzungen",
                value=_sessions_text(sessions),
                inline=False,
            )
            embed.set_footer(text=f"Ausgeführt von {member.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            log.info("%s hat %d Sitzung(en) auf '%s' widerrufen.", member.display_name,
                     len(revoked), guild.name)

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


def _missing_permissions_text(perms: discord.Permissions) -> str:
    needed = [
        "manage_guild", "manage_channels", "manage_roles", "manage_webhooks",
        "manage_expressions", "manage_events", "kick_members", "ban_members",
        "moderate_members", "manage_messages", "view_audit_log", "manage_threads",
        "move_members", "mute_members", "deafen_members", "request_to_speak",
    ]
    missing = [name for name in needed if not getattr(perms, name, False)]
    if not missing:
        return "keine"
    text = ", ".join(f"`{m}`" for m in missing)
    return text[:900] + (" …" if len(text) > 900 else "")


def _sessions_text(sessions: List[Any]) -> str:
    if not sessions:
        return ""
    lines: List[str] = []
    for session in sessions[-8:]:
        info = session.to_public_dict()
        lines.append(
            f"• `{session.token_prefix}` · {info['mode_label']} · von "
            f"{info['created_by_name']} · {info['expires_in']} · "
            f"{info['request_count']} Aufrufe"
        )
    if len(sessions) > 8:
        lines.append(f"… und {len(sessions) - 8} weitere")
    return "\n".join(lines)[:1000]
