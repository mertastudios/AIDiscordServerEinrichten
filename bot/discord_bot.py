"""
Der Discord-Teil: Slash-Commands, Buttons und Rechteprüfungen.

``/connect`` ist *der* Command. Er

1. prüft, dass **der Nutzer Administrator** ist,
2. prüft, dass **der Bot Administrator** ist,
3. erzeugt ein Sitzungs-Token (nur für diesen Server, mit Ablaufdatum),
4. antwortet **ephemeral** mit genau einer klaren Nachricht: dem fertigen
   Prompt für Arena AI im Codeblock — URL und Token stecken darin und werden
   mit **einem Klick** mitkopiert (Discord-Kopierbutton am Codeblock),
5. bietet Buttons für Console, Token-Erneuerung und Widerruf.

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
from .prompt import PromptContext, short_prompt
from .sessions import DEFAULT_MODE, MODES, SessionStore, normalize_mode
from .util import ApiError, human_duration, now_utc

log = logging.getLogger("relay.discord")

__all__ = ("RelayClient", "build_intents", "ADMIN_INVITE_SCOPES")

ADMIN_INVITE_SCOPES = ("bot", "applications.commands")

#: Mindestabstand zwischen zwei globalen Slash-Command-Syncs (Sekunden).
COMMAND_SYNC_MIN_INTERVAL = 3600.0

OK_COLOR = discord.Color.from_str("#2ECC71")
WARN_COLOR = discord.Color.from_str("#F1C40F")
ERR_COLOR = discord.Color.from_str("#E74C3C")

DURATION_CHOICES = [
    app_commands.Choice(name="⏱️ 1 Stunde", value=1.0),
    app_commands.Choice(name="🕕 6 Stunden", value=6.0),
    app_commands.Choice(name="📅 24 Stunden (empfohlen)", value=24.0),
    app_commands.Choice(name="🗓️ 3 Tage", value=72.0),
    app_commands.Choice(name="🗓️ 7 Tage", value=168.0),
    app_commands.Choice(name="🗓️ 30 Tage", value=720.0),
    app_commands.Choice(name="♾️ Unbegrenzt (nicht empfohlen)", value=0.0),
]

MODE_CHOICES = [
    app_commands.Choice(name="✍️ Lesen + Schreiben — alles einrichten & moderieren (Standard)", value="read_write"),
    app_commands.Choice(name="👁️ Nur lesen — nichts verändern", value="read"),
]


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
#  Buttons (persistent, überleben einen Neustart)
# ─────────────────────────────────────────────────────────────────────────────


class RelayButtons(discord.ui.View):
    """
    Die drei Buttons unter der ``/connect``-Antwort.

    ``timeout=None`` + Registrierung in ``setup_hook`` macht sie persistent:
    Auch nach einem Render-Deploy reagieren alte Nachrichten noch.
    """

    def __init__(self, client: "RelayClient", console_url: str) -> None:
        super().__init__(timeout=None)
        self.client = client
        self.add_item(
            discord.ui.Button(
                label="Console öffnen",
                emoji="🖥️",
                style=discord.ButtonStyle.link,
                url=console_url,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Neues Token",
                emoji="🔄",
                style=discord.ButtonStyle.primary,
                custom_id="relay:regenerate",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Alle widerrufen",
                emoji="⛔",
                style=discord.ButtonStyle.danger,
                custom_id="relay:revoke_all",
            )
        )


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
        # Persistente Button-View registrieren (URL-Button ist dekorativ,
        # die Custom-ID-Buttons brauchen den Handler).
        self.add_view(RelayButtons(self, f"{self.state.base_url()}/console"))
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

    # ── Command-Registrierung ────────────────────────────────────────────────
    def _register_commands(self) -> None:
        command_name = self.config.command_name

        @self.tree.command(
            name=command_name,
            description="🔗 Link + Token für Arena AI erzeugen (nur für dich sichtbar)",
        )
        @app_commands.guild_only()
        @app_commands.describe(
            dauer="Wie lange soll der Zugriff gültig sein?",
            modus="Was darf die KI auf diesem Server tun?",
        )
        @app_commands.choices(dauer=DURATION_CHOICES, modus=MODE_CHOICES)
        async def connect(
            interaction: discord.Interaction,
            dauer: float = 24.0,
            modus: str = DEFAULT_MODE,
        ) -> None:
            """Der Haupt-Command: erzeugt Link + Token und den fertigen KI-Prompt."""
            gate = await self._guard(interaction)
            if gate is None:
                return
            guild = gate["guild"]
            member = gate["member"]

            session, token = await self.store.create(
                guild_id=guild.id,
                guild_name=guild.name,
                created_by=member.id,
                created_by_name=member.display_name,
                mode=modus,
                ttl_hours=dauer,
                note="per /connect erzeugt",
            )
            self.state.maybe_save(force=True)
            await self._send_credentials(interaction, session, token, guild, member)

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
        Zentrale Prüfung vor jedem Command.

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

    # ── Antwort mit Zugangsdaten ─────────────────────────────────────────────
    async def _send_credentials(
        self,
        interaction: discord.Interaction,
        session,
        token: str,
        guild: discord.Guild,
        member: discord.Member,
    ) -> None:
        base = self.state.base_url()
        console_url = f"{base}/console?t={token}"
        prompt_ctx = PromptContext(
            base_url=base,
            token=token,
            guild_name=guild.name,
            guild_id=guild.id,
            mode_label=MODES.get(session.mode, {}).get("label", session.mode),
            scope=session.scope,
            expires_label=session.to_public_dict()["expires_in"],
            session_id=session.id,
            member_count=guild.member_count,
            console_url=console_url,
        )
        short = short_prompt(prompt_ctx)

        # ── Antwort: EINE Nachricht — nur der Prompt, fertig zum Kopieren ────
        mode_info = MODES.get(session.mode, {})
        expires = session.to_public_dict()["expires_in"]
        intro = (
            "✅ **Fertig!** Kopiere den **kompletten Block** unten (Kopier-Button "
            "oben rechts am Codeblock) und schicke ihn bei **Arena AI** als "
            "Nachricht ein — **URL + Token** für die Verbindung stecken schon "
            "im Prompt.\n"
            f"{mode_info.get('emoji', '')} Modus: **{mode_info.get('label', session.mode)}** · "
            f"⏳ gültig: **{expires}**\n\n"
        )
        view = RelayButtons(self, console_url)
        try:
            await interaction.response.send_message(
                content=_prompt_message(intro, short), view=view, ephemeral=True,
            )
        except discord.HTTPException as exc:
            log.error("Antwort auf /%s fehlgeschlagen: %s", self.config.command_name, exc)
            return

        log.info(
            "/%s ausgeführt von %s (%s) auf '%s' — Sitzung %s, Modus %s, gültig %s",
            self.config.command_name, member.display_name, member.id, guild.name,
            session.id, session.mode, session.to_public_dict()["expires_in"],
        )

    # ── Button-Handler ───────────────────────────────────────────────────────
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """
        Wird von discord.py für JEDE Interaktion gerufen; Slash-Commands
        gehen über den CommandTree, hier zählen nur Komponenten-Klicks.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")

        if custom_id == "relay:regenerate":
            await self._handle_regenerate(interaction)
        elif custom_id == "relay:revoke_all":
            await self._handle_revoke_all(interaction)

    async def _handle_regenerate(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        gate = await self._guard(interaction)
        if gate is None:
            return
        await interaction.response.defer(ephemeral=True)
        guild = gate["guild"]
        member = gate["member"]
        old = self.store.active_for_guild(guild.id)
        session, token = await self.store.create(
            guild_id=guild.id,
            guild_name=guild.name,
            created_by=member.id,
            created_by_name=member.display_name,
            mode=normalize_mode(old[-1].mode) if old else DEFAULT_MODE,
            ttl_hours=self.config.session_ttl_hours,
            note="per Button erneuert",
        )
        self.state.maybe_save(force=True)

        base = self.state.base_url()
        prompt_ctx = PromptContext(
            base_url=base, token=token, guild_name=guild.name, guild_id=guild.id,
            mode_label=MODES.get(session.mode, {}).get("label", session.mode),
            scope=session.scope, expires_label=session.to_public_dict()["expires_in"],
            session_id=session.id, member_count=guild.member_count,
            console_url=f"{base}/console?t={token}",
        )
        short = short_prompt(prompt_ctx)

        intro = (
            "🔄 **Neues Token erzeugt.** Alte Tokens bleiben gültig, bis sie "
            "ablaufen — mit `/revoke` sofort entziehen.\n"
            "📋 Neuer Prompt für Arena AI (URL + Token stecken drin):\n\n"
        )
        await interaction.followup.send(
            content=_prompt_message(intro, short),
            view=RelayButtons(self, prompt_ctx.console_url),
            ephemeral=True,
        )

    async def _handle_revoke_all(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        gate = await self._guard(interaction, require_bot_admin=False)
        if gate is None:
            return
        guild = gate["guild"]
        revoked = await self.store.revoke(guild_id=guild.id, by=f"button:{interaction.user.id}")
        self.state.maybe_save(force=True)
        embed = discord.Embed(
            title="⛔ Alle KI-Zugriffe widerrufen",
            description=(
                f"**{len(revoked)} Token** sofort deaktiviert." if revoked
                else "Es gab keine aktiven Tokens."
            ),
            color=ERR_COLOR,
            timestamp=now_utc(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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


def _prompt_message(intro: str, prompt_text: str) -> str:
    """Intro + Prompt im Codeblock, garantiert unter Discords 2000-Zeichen-Limit."""
    block = f"```\n{prompt_text}\n```"
    budget = 1990 - len(intro)
    if len(block) > budget:
        block = block[: max(0, budget - 8)] + "\n…\n```"
    return intro + block


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

