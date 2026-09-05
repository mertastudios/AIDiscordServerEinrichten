"""
Der Setup-Wizard: ein kompletter Discord-Server in **einem** API-Aufruf.

Warum das wichtig ist
---------------------
Ein Server-Setup besteht aus Dutzenden abhängigen Schritten: Rollen brauchen
Permissions, Kategorien brauchen Overwrites auf diese Rollen, Kanäle hängen in
Kategorien, Nachrichten landen in Kanälen, Server-Einstellungen verweisen auf
Kanäle. Würde die KI das Schritt für Schritt machen, bräuchte sie 60+ Aufrufe
— mit entsprechendem Rate-Limit-Risiko und vielen Gelegenheiten, sich zu
verheddern.

``POST /api/v1/setup`` nimmt deshalb **einen** deklarativen Plan und führt ihn
in der richtigen Reihenfolge aus. Über ``key``-Felder verweist man auf Objekte,
die im selben Aufruf erst noch entstehen:

.. code-block:: json

    {"roles": [{"key": "mod", "name": "🛡️ Mod"}],
     "categories": [{"key": "info", "name": "📌 INFO",
                     "overwrites": [{"role_key": "mod", "allow": ["send_messages"]}],
                     "channels": [{"key": "regeln", "name": "regeln", "type": "text"}]}],
     "settings": {"rules_channel": "regeln"}}

Jeder Schritt ist fehlertolerant: Schlägt etwas fehl, läuft der Rest weiter und
der Fehler erscheint strukturiert in ``report``. So bleibt ein halb fertiger
Server nie unverständlich.

Dazu gibt es ``GET /api/v1/setup/templates`` mit ausformulierten, deutschen
Server-Vorlagen, die die KI nur noch anpassen muss.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional

import discord
from discord.utils import MISSING

from ...serializers import serialize_channel, serialize_guild, serialize_message, serialize_role
from ...util import (
    ApiError,
    parse_bool,
    parse_int,
    parse_str,
    sf,
)
from ..channel_ops import create_channel
from ..context import Ctx, guard
from ..registry import route
from .roles import ROLE_PRESETS, _finalize_role_kwargs, _role_kwargs_from_spec

MAX_ITEMS = 60


# ─────────────────────────────────────────────────────────────────────────────
#  Vorlagen
# ─────────────────────────────────────────────────────────────────────────────

def _template_gaming() -> Dict[str, Any]:
    return {
        "beschreibung": "Klassische Gaming-Community mit Voice-Lounges, LFG und Turnieren.",
        "roles": [
            {"key": "owner", "name": "👑 Owner", "color": "#E74C3C", "hoist": True,
             "permissions": ["administrator"], "position": 20},
            {"key": "admin", "name": "🛠️ Admin", "color": "#E67E22", "hoist": True,
             "permissions": ["manage_guild", "manage_channels", "manage_roles", "manage_messages",
                             "kick_members", "ban_members", "moderate_members", "view_audit_log",
                             "manage_webhooks", "manage_expressions", "manage_events"], "position": 18},
            {"key": "mod", "preset": "moderator", "position": 16},
            {"key": "vip", "preset": "vip", "position": 12},
            {"key": "gamer", "name": "🎮 Gamer", "color": "#5865F2", "hoist": True,
             "permissions": ["view_channel", "send_messages", "read_message_history", "connect",
                             "speak", "stream", "use_voice_activation", "add_reactions",
                             "attach_files", "embed_links", "create_instant_invite",
                             "use_application_commands"], "position": 8},
            {"key": "member", "preset": "mitglied", "position": 4},
            {"key": "bot", "preset": "bot", "position": 14},
            {"key": "muted", "preset": "muted", "position": 2},
        ],
        "categories": [
            {"key": "info", "name": "📌 INFORMATION", "position": 0,
             "overwrites": [{"id": "@everyone", "deny": ["send_messages", "add_reactions"]}],
             "channels": [
                 {"key": "regeln", "name": "regeln", "type": "text",
                  "topic": "Serverregeln — bitte zuerst lesen!"},
                 {"key": "news", "name": "ankündigungen", "type": "announcement",
                  "topic": "Wichtige Neuigkeiten"},
                 {"key": "welcome", "name": "willkommen", "type": "text",
                  "topic": "Neue Mitglieder"},
                 {"key": "roles", "name": "rollen-wahl", "type": "text",
                  "topic": "Such dir deine Rollen"}]},
            {"key": "community", "name": "💬 COMMUNITY", "position": 1,
             "channels": [
                 {"key": "chat", "name": "allgemein", "type": "text", "slowmode_delay": 3,
                  "topic": "Hauptchat für alles"},
                 {"name": "memes", "type": "text", "topic": "Nur die besten Memes"},
                 {"name": "clips", "type": "forum", "topic": "Teile deine besten Momente",
                  "available_tags": ["Highlight", "Fail", "Clip"]},
                 {"name": "bilder", "type": "text", "topic": "Screenshots & Fotos"},
                 {"name": "bot-commands", "type": "text", "topic": "Nur für Bots"}]},
            {"key": "gaming", "name": "🎮 GAMING", "position": 2,
             "channels": [
                 {"name": "lfg-suche", "type": "text",
                  "topic": "Mitspieler gesucht? Hier posten!"},
                 {"name": "valorant", "type": "text"},
                 {"name": "minecraft", "type": "text"},
                 {"name": "turniere", "type": "announcement", "topic": "Turnier-Ankündigungen"}]},
            {"key": "voice", "name": "🔊 SPRACHKANÄLE", "position": 3,
             "channels": [
                 {"name": "Lounge", "type": "voice", "user_limit": 0},
                 {"name": "Gaming 1", "type": "voice", "user_limit": 10},
                 {"name": "Gaming 2", "type": "voice", "user_limit": 10},
                 {"name": "Duo", "type": "voice", "user_limit": 2},
                 {"name": "AFK", "type": "voice", "user_limit": 0}]},
            {"key": "team", "name": "🔒 TEAM INTERN", "position": 4,
             "overwrites": [
                 {"id": "@everyone", "deny": ["view_channel"]},
                 {"role_key": "mod", "allow": ["view_channel", "send_messages", "read_message_history",
                                               "attach_files", "embed_links", "add_reactions",
                                               "connect", "speak"]},
                 {"role_key": "admin", "allow": ["view_channel", "send_messages", "manage_messages"]}],
             "channels": [
                 {"name": "team-chat", "type": "text", "topic": "Nur für das Team"},
                 {"name": "mod-log", "type": "text", "topic": "Automatische Moderations-Logs"},
                 {"name": "Team-Besprechung", "type": "voice"}]},
        ],
        "settings": {"system_channel": "welcome", "rules_channel": "regeln",
                     "afk_channel": "AFK", "afk_timeout": 900},
        "messages": [
            {"channel": "regeln",
             "embeds": [{"title": "📜 Serverregeln", "color": "#E74C3C",
                         "description": "Mit dem Beitritt akzeptierst du diese Regeln.",
                         "fields": [
                             {"name": "1 · Respekt", "value": "Keine Beleidigungen, kein Rassismus, "
                                                             "keine Belästigung.", "inline": False},
                             {"name": "2 · Kein Spam", "value": "Kein Flood, keine Werbung, "
                                                              "keine unerwünschten DMs.", "inline": False},
                             {"name": "3 · Inhalte", "value": "Keine NSFW-Inhalte, keine illegalen "
                                                              "Inhalte.", "inline": False},
                             {"name": "4 · Kanäle", "value": "Themen in die passenden Kanäle.",
                              "inline": False},
                             {"name": "5 · Team", "value": "Anweisungen des Teams sind "
                                                           "bindend.", "inline": False}],
                         "footer": {"text": "Verstöße führen zu Timeout, Kick oder Ban."}}]},
            {"channel": "welcome",
             "embeds": [{"title": "👋 Willkommen!", "color": "#5865F2",
                         "description": "Schön, dass du da bist! Lies zuerst <#regeln> "
                                        "und hol dir in <#rollen-wahl> deine Rollen.",
                         "footer": {"text": "Viel Spaß auf dem Server"}}]},
        ],
        "automod": [
            {"name": "Keine Fremd-Werbung", "trigger_type": "keyword",
             "regex_patterns": ["discord\\.(gg|com|me)/\\w+", "https?://\\S+/invite/\\w+"],
             "actions": [{"type": "block_message"},
                         {"type": "send_alert_message", "channel": "mod-log"}],
             "exempt_roles": ["admin", "mod"]},
            {"name": "Beleidigungen blockieren", "trigger_type": "keyword_preset",
             "presets": ["profanity", "slurs", "sexual_content"],
             "actions": [{"type": "block_message"}, {"type": "timeout", "duration": "10m"}],
             "exempt_roles": ["admin", "mod"]},
            {"name": "Mention-Raid", "trigger_type": "mention_spam", "mention_limit": 6,
             "actions": [{"type": "block_message"}, {"type": "timeout", "duration": "1h"}]},
        ],
    }


def _template_creator() -> Dict[str, Any]:
    return {
        "beschreibung": "Für Streamer/Creator: Ankündigungen, Clips, Sub-Rollen, Community-Events.",
        "roles": [
            {"key": "owner", "name": "👑 Creator", "color": "#9B59B6", "hoist": True,
             "permissions": ["administrator"], "position": 20},
            {"key": "mod", "preset": "moderator", "position": 16},
            {"key": "sub", "name": "⭐ Subscriber", "color": "#F1C40F", "hoist": True,
             "permissions": ["view_channel", "send_messages", "read_message_history",
                             "use_external_emojis", "attach_files", "embed_links",
                             "add_reactions", "connect", "speak", "stream"], "position": 12},
            {"key": "member", "preset": "mitglied", "position": 4},
            {"key": "bot", "preset": "bot", "position": 14},
        ],
        "categories": [
            {"key": "start", "name": "🚀 START HIER", "position": 0,
             "overwrites": [{"id": "@everyone", "deny": ["send_messages"]}],
             "channels": [
                 {"key": "regeln", "name": "regeln", "type": "text"},
                 {"key": "streams", "name": "stream-ankündigungen", "type": "announcement",
                  "topic": "Wann geht's live?"},
                 {"key": "welcome", "name": "willkommen", "type": "text"}]},
            {"key": "community", "name": "💬 COMMUNITY", "position": 1,
             "channels": [
                 {"key": "chat", "name": "allgemein", "type": "text", "slowmode_delay": 5},
                 {"name": "clips", "type": "forum", "topic": "Deine besten Clips",
                  "available_tags": ["Best-of", "Fail", "Edit"]},
                 {"name": "fanart", "type": "text", "topic": "Kunst & Edits"},
                 {"name": "suggestions", "type": "forum", "topic": "Ideen für den Stream",
                  "available_tags": ["Content", "Overlay", "Event"]}]},
            {"key": "subs", "name": "⭐ SUBSCRIBER", "position": 2,
             "overwrites": [{"id": "@everyone", "deny": ["view_channel"]},
                            {"role_key": "sub", "allow": ["view_channel", "send_messages",
                                                           "read_message_history", "connect", "speak"]}],
             "channels": [
                 {"name": "sub-chat", "type": "text", "topic": "Exklusiv für Subs"},
                 {"name": "Sub-Lounge", "type": "voice"}]},
            {"key": "voice", "name": "🔊 VOICE", "position": 3,
             "channels": [
                 {"name": "Community", "type": "voice", "user_limit": 25},
                 {"name": "Gaming", "type": "voice", "user_limit": 10},
                 {"name": "AFK", "type": "voice"}]},
        ],
        "settings": {"system_channel": "welcome", "rules_channel": "regeln", "afk_channel": "AFK"},
        "messages": [
            {"channel": "streams",
             "embeds": [{"title": "🔴 Stream-Zeiten", "color": "#9B59B6",
                         "fields": [{"name": "Montag", "value": "19:00 Uhr"},
                                    {"name": "Mittwoch", "value": "19:00 Uhr"},
                                    {"name": "Freitag", "value": "20:00 Uhr"}],
                         "footer": {"text": "Aktiviere die Glocke, um nichts zu verpassen"}}]},
            {"channel": "regeln",
             "embeds": [{"title": "📜 Regeln", "color": "#E74C3C",
                         "description": "1. Sei respektvoll\n2. Kein Spam\n3. Keine Werbung\n"
                                        "4. Kein Backseat-Gaming ohne Nachfrage\n"
                                        "5. Team-Entscheidungen sind final"}]},
        ],
        "automod": [
            {"name": "Werbung", "trigger_type": "keyword",
             "regex_patterns": ["discord\\.(gg|com)/\\w+", "twitch\\.tv/(?!deinkanal)\\w+"],
             "actions": [{"type": "block_message"}]},
            {"name": "Spam-Schutz", "trigger_type": "spam",
             "actions": [{"type": "block_message"}, {"type": "timeout", "duration": "5m"}]},
        ],
    }


def _template_study() -> Dict[str, Any]:
    return {
        "beschreibung": "Lern- & Studiengruppe: Fächer-Kanäle, Lernsessions, Ressourcen.",
        "roles": [
            {"key": "owner", "name": "🎓 Leitung", "color": "#2C3E50", "hoist": True,
             "permissions": ["administrator"], "position": 20},
            {"key": "tutor", "name": "📚 Tutor", "color": "#16A085", "hoist": True,
             "permissions": ["manage_messages", "moderate_members", "view_channel",
                             "send_messages", "read_message_history", "manage_threads",
                             "pin_messages", "connect", "speak", "mute_members"], "position": 14},
            {"key": "member", "preset": "mitglied", "position": 4},
            {"key": "bot", "preset": "bot", "position": 10},
        ],
        "categories": [
            {"key": "start", "name": "📌 START", "position": 0,
             "overwrites": [{"id": "@everyone", "deny": ["send_messages"]}],
             "channels": [
                 {"key": "regeln", "name": "regeln", "type": "text"},
                 {"key": "welcome", "name": "willkommen", "type": "text"},
                 {"key": "ressourcen", "name": "ressourcen", "type": "forum",
                  "topic": "Skripte, Links, Zusammenfassungen",
                  "available_tags": ["Skript", "Video", "Tool", "Zusammenfassung"]}]},
            {"key": "faecher", "name": "📖 FÄCHER", "position": 1,
             "channels": [
                 {"name": "mathe", "type": "text", "topic": "Mathematik"},
                 {"name": "informatik", "type": "text", "topic": "Programmierung & Co."},
                 {"name": "physik", "type": "text"},
                 {"name": "fragen", "type": "forum", "topic": "Frag alles",
                  "available_tags": ["Gelöst", "Offen", "Klausur"]}]},
            {"key": "social", "name": "☕ PAUSE", "position": 2,
             "channels": [
                 {"key": "chat", "name": "allgemein", "type": "text", "slowmode_delay": 3},
                 {"name": "memes", "type": "text"}]},
            {"key": "lernraum", "name": "🎧 LERNRÄUME", "position": 3,
             "channels": [
                 {"name": "Stille Bibliothek", "type": "voice", "user_limit": 0},
                 {"name": "Gruppe A", "type": "voice", "user_limit": 6},
                 {"name": "Gruppe B", "type": "voice", "user_limit": 6}]},
        ],
        "settings": {"system_channel": "welcome", "rules_channel": "regeln",
                     "afk_channel": "Stille Bibliothek"},
        "messages": [
            {"channel": "regeln",
             "embeds": [{"title": "📜 Lernregeln", "color": "#16A085",
                         "description": "1. Fragen sind immer willkommen\n"
                                        "2. Keine fertigen Lösungen ohne Erklärung\n"
                                        "3. Fächerkanäle nutzen\n"
                                        "4. Keine Werbung für Bezahldienste"}]},
        ],
    }


def _template_business() -> Dict[str, Any]:
    return {
        "beschreibung": "Produkt-/Firma-Server mit Support, Feedback und Kundenbereich.",
        "roles": [
            {"key": "owner", "name": "👑 Inhaber", "color": "#1A1A2E", "hoist": True,
             "permissions": ["administrator"], "position": 20},
            {"key": "team", "name": "💼 Team", "color": "#3498DB", "hoist": True,
             "permissions": ["manage_messages", "moderate_members", "view_audit_log",
                             "view_channel", "send_messages", "read_message_history",
                             "manage_threads", "embed_links", "attach_files",
                             "connect", "speak"], "position": 15},
            {"key": "kunde", "name": "🛒 Kunde", "color": "#2ECC71", "hoist": True,
             "permissions": ["view_channel", "send_messages", "read_message_history",
                             "attach_files", "embed_links", "add_reactions"], "position": 8},
            {"key": "member", "preset": "mitglied", "position": 4},
            {"key": "bot", "preset": "bot", "position": 12},
        ],
        "categories": [
            {"key": "info", "name": "ℹ️ INFORMATION", "position": 0,
             "overwrites": [{"id": "@everyone", "deny": ["send_messages"]}],
             "channels": [
                 {"key": "regeln", "name": "regeln", "type": "text"},
                 {"key": "news", "name": "neuigkeiten", "type": "announcement",
                  "topic": "Updates & Ankündigungen"},
                 {"key": "welcome", "name": "willkommen", "type": "text"},
                 {"key": "faq", "name": "faq", "type": "text", "topic": "Häufige Fragen"}]},
            {"key": "support", "name": "🎧 SUPPORT", "position": 1,
             "channels": [
                 {"name": "hilfe", "type": "forum", "topic": "Support-Anfragen",
                  "available_tags": ["Offen", "In Bearbeitung", "Gelöst", "Bug", "Frage"]},
                 {"name": "status", "type": "text", "topic": "Störungen & Wartungen"}]},
            {"key": "feedback", "name": "💡 FEEDBACK", "position": 2,
             "channels": [
                 {"name": "ideen", "type": "forum", "topic": "Feature-Wünsche",
                  "available_tags": ["Neu", "Geplant", "Abgelehnt", "Umgesetzt"]},
                 {"name": "bewertungen", "type": "text", "topic": "Erfahrungsberichte"}]},
            {"key": "community", "name": "💬 COMMUNITY", "position": 3,
             "channels": [
                 {"key": "chat", "name": "allgemein", "type": "text", "slowmode_delay": 5},
                 {"name": "Showcase", "type": "text", "topic": "Zeig, was du gebaut hast"}]},
            {"key": "voice", "name": "🔊 VOICE", "position": 4,
             "channels": [{"name": "Meetings", "type": "voice", "user_limit": 15},
                          {"name": "Lounge", "type": "voice"}]},
        ],
        "settings": {"system_channel": "welcome", "rules_channel": "regeln", "afk_channel": "Lounge"},
        "messages": [
            {"channel": "faq",
             "embeds": [{"title": "❓ Häufige Fragen", "color": "#3498DB",
                         "fields": [{"name": "Wie erreiche ich den Support?",
                                     "value": "Erstelle einen Post in #hilfe."},
                                    {"name": "Wo finde ich Updates?",
                                     "value": "In #neuigkeiten."},
                                    {"name": "Wie schlage ich Features vor?",
                                     "value": "In #ideen."}]}]},
        ],
    }


def _template_friends() -> Dict[str, Any]:
    return {
        "beschreibung": "Kleiner, gemütlicher Freunde-Server ohne Overhead.",
        "roles": [
            {"key": "owner", "name": "👑 Chef", "color": "#E74C3C", "hoist": True,
             "permissions": ["administrator"], "position": 10},
            {"key": "friends", "name": "🫂 Freunde", "color": "#2ECC71", "hoist": True,
             "permissions": ["view_channel", "send_messages", "read_message_history",
                             "attach_files", "embed_links", "add_reactions",
                             "use_external_emojis", "connect", "speak", "stream",
                             "use_voice_activation", "priority_speaker", "create_instant_invite",
                             "change_nickname", "create_public_threads",
                             "send_messages_in_threads", "use_application_commands"], "position": 5},
        ],
        "categories": [
            {"key": "main", "name": "🏠 HAUPTBEREICH", "position": 0,
             "channels": [
                 {"key": "chat", "name": "chat", "type": "text", "topic": "Alles Mögliche"},
                 {"name": "bilder", "type": "text"},
                 {"name": "musik", "type": "text"},
                 {"name": "games", "type": "text"}]},
            {"key": "voice", "name": "🔊 VOICE", "position": 1,
             "channels": [{"name": "Wohnzimmer", "type": "voice"},
                          {"name": "Gaming", "type": "voice", "user_limit": 6},
                          {"name": "AFK", "type": "voice"}]},
        ],
        "settings": {"afk_channel": "AFK", "afk_timeout": 1800},
        "messages": [
            {"channel": "chat",
             "embeds": [{
                 "title": "👋 Willkommen im Wohnzimmer!",
                 "color": "#2ECC71",
                 "description": (
                     "Schön, dass du da bist. Hier ist alles — kein Regelwerk, "
                     "nur ein bisschen Rücksicht.\n\n"
                     "**Text**\n"
                     "> #chat — alles Mögliche\n"
                     "> #bilder — Memes, Fotos, Fundstücke\n"
                     "> #musik — was gerade läuft\n"
                     "> #games — Verabredungen zum Zocken\n\n"
                     "**Voice**\n"
                     "> 🔊 Wohnzimmer — einfach reinsetzen\n"
                     "> 🔊 Gaming — max. 6 Leute\n\n"
                     "Mach's dir bequem. 🛋️"
                 ),
                 "footer": {"text": "Server eingerichtet mit AIDiscordServerEinrichten"},
             }]},
        ],
    }


SETUP_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "gaming-community": _template_gaming(),
    "creator-streamer": _template_creator(),
    "lerngruppe": _template_study(),
    "business-support": _template_business(),
    "freunde": _template_friends(),
}


@route(
    "GET", "/api/v1/setup/templates", scope="read", tags=("setup",),
    summary="Fertige Server-Vorlagen (Gaming, Creator, Lerngruppe, Business, Freunde)",
    query={"name": "eine Vorlage im Detail", "list": "true — nur Namen + Beschreibung"},
    description="Jede Vorlage ist ein kompletter, direkt ausführbarer Setup-Plan. "
                "Empfohlener Ablauf: Vorlage holen → an die Wünsche des Nutzers "
                "anpassen → POST /api/v1/setup.",
)
async def setup_templates(ctx: Ctx) -> Dict[str, Any]:
    name = ctx.q("name")
    if ctx.q_bool("list", False) or not name:
        return {
            "count": len(SETUP_TEMPLATES),
            "templates": [
                {"key": key, "beschreibung": tpl.get("beschreibung", "")}
                for key, tpl in sorted(SETUP_TEMPLATES.items())
            ],
            "usage": "GET /api/v1/setup/templates?name=gaming-community liefert den "
                     "kompletten Plan; POST /api/v1/setup führt ihn aus.",
            "role_presets": sorted(ROLE_PRESETS),
        }
    key = name.strip().lower()
    if key not in SETUP_TEMPLATES:
        raise ApiError.not_found(
            f"Vorlage '{name}' existiert nicht.",
            hint="Verfügbar: " + ", ".join(sorted(SETUP_TEMPLATES)),
            code="TEMPLATE_NOT_FOUND",
        )
    return {"key": key, **SETUP_TEMPLATES[key]}


@route(
    "POST", "/api/v1/setup/preview", scope="write", tags=("setup",),
    summary="Setup-Plan prüfen, ohne etwas zu ändern (Dry Run)",
    body={"…": "identisch zu POST /api/v1/setup"},
    description="Validiert Rollen, Permissions, Overwrites, Kanal-Typen und Referenzen, "
                "zählt die Schritte und meldet Probleme — **ohne** einen einzigen "
                "Discord-Aufruf. Perfekt, bevor man etwas Großes ausführt.",
)
async def setup_preview(ctx: Ctx) -> Dict[str, Any]:
    plan = await ctx.body()
    issues = _validate_plan(ctx.guild, plan)
    counts = _count_plan(plan)
    return {
        "dry_run": True,
        "valid": not issues["errors"],
        "counts": counts,
        "errors": issues["errors"] or None,
        "warnings": issues["warnings"] or None,
        "execution_order": [
            "1. Rollen anlegen (danach Positionen in einem Call)",
            "2. Kategorien anlegen",
            "3. Kanäle in Kategorien",
            "4. Kanäle ohne Kategorie",
            "5. Server-Einstellungen (system/rules/afk/community)",
            "6. Willkommensbildschirm",
            "7. Nachrichten posten",
            "8. Invites erstellen",
            "9. AutoMod-Regeln",
            "10. Rollen-Positionen final justieren",
        ],
        "hint": "POST /api/v1/setup führt denselben Plan wirklich aus.",
    }


@route(
    "POST", "/api/v1/setup", scope="write", tags=("setup",),
    summary="Komplettes Server-Setup in EINEM Aufruf",
    body={
        "template": "'gaming-community'|'creator-streamer'|'lerngruppe'|'business-support'|"
                    "'freunde' — wird mit den anderen Feldern gemischt",
        "guild": "{…} — wie PATCH /api/v1/guild (name, verification_level, community …)",
        "roles": '[{"key":"mod","name":"🛡️ Mod","preset":"moderator","color":"#3498DB",'
                 '"permissions":[…],"position":16}]',
        "categories": '[{"key":"info","name":"📌 INFO","position":0,"overwrites":[…],'
                      '"channels":[{…}]}]',
        "channels": '[{…}] — Kanäle ohne Kategorie',
        "settings": '{"system_channel":"welcome","rules_channel":"regeln",'
                    '"afk_channel":"AFK","afk_timeout":900,"public_updates_channel":"…"}',
        "welcome": '{"enabled":true,"description":"…","welcome_channels":[…]}',
        "messages": '[{"channel":"regeln","content":"…","embeds":[…],"components":[[…]]}]',
        "invites": '[{"channel":"chat","max_age":0,"max_uses":0}]',
        "automod": '[{"name":"…","trigger_type":"keyword","keyword_filter":[…],"actions":[…]}]',
        "delay_ms": "int — Pause zwischen Discord-Aufrufen (Standard 350)",
        "stop_on_error": "bool — Standard false",
        "reason": "str",
    },
    description="Referenzen auf noch nicht existierende Objekte laufen über ``key``. "
                "Jeder Schritt wird protokolliert; Einzelfehler brechen den Rest nicht ab.",
    examples=[
        {"body": {
            "template": "gaming-community",
            "guild": {"name": "Mein Gaming Server", "verification_level": "medium"},
            "reason": "Grundsetup durch Arena AI",
        }},
    ],
)
async def setup(ctx: Ctx) -> Dict[str, Any]:
    plan = await ctx.body()
    if not plan:
        raise ApiError.bad_request(
            "Leerer Setup-Plan.",
            hint='Mindestens eines von: "template", "roles", "categories", "channels", '
                 '"guild", "messages".',
        )

    # Vorlage als Basis, eigene Felder überschreiben
    template_key = plan.get("template")
    if template_key:
        key = str(template_key).strip().lower()
        if key not in SETUP_TEMPLATES:
            raise ApiError.bad_request(
                f"Vorlage '{template_key}' existiert nicht.",
                hint="Verfügbar: " + ", ".join(sorted(SETUP_TEMPLATES))
                     + " — oder GET /api/v1/setup/templates",
                code="TEMPLATE_NOT_FOUND",
            )
        merged = {**SETUP_TEMPLATES[key], **{k: v for k, v in plan.items() if v is not None}}
        merged.pop("beschreibung", None)
        plan = merged

    issues = _validate_plan(ctx.guild, plan)
    if issues["errors"]:
        raise ApiError.bad_request(
            "Der Setup-Plan enthält Fehler und wurde nicht ausgeführt.",
            hint="Korrigiere die Punkte unter error.errors. Tipp: vorher "
                 "POST /api/v1/setup/preview aufrufen.",
            code="SETUP_PLAN_INVALID",
            errors=issues["errors"], warnings=issues["warnings"],
        )

    delay = parse_int(plan.get("delay_ms"), field="delay_ms", default=350, minimum=0, maximum=5000) or 0
    stop_on_error = bool(parse_bool(plan.get("stop_on_error"), field="stop_on_error", default=False))
    reason = ctx.reason(plan, default="Server-Setup durch Arena AI")
    guild = ctx.guild

    keys: Dict[str, Any] = {}
    report: List[Dict[str, Any]] = []
    created: Dict[str, Any] = {"roles": [], "categories": [], "channels": [], "messages": [],
                               "invites": [], "automod": [], "settings": None}
    aborted: Optional[str] = None

    def step(name: str, ok: bool, *, detail: Any = None, error: Optional[ApiError] = None) -> bool:
        entry: Dict[str, Any] = {"step": name, "ok": ok}
        if detail is not None:
            entry["detail"] = detail
        if error is not None:
            entry["error"] = error.to_dict()["error"]
        report.append(entry)
        return ok

    async def pause(seconds: Optional[float] = None) -> None:
        await asyncio.sleep((delay / 1000) if seconds is None else seconds)

    # ── 0. Server-Einstellungen, die VOR Kanälen Sinn ergeben ────────────────
    guild_patch = {k: v for k, v in (plan.get("guild") or {}).items()
                   if k in {"name", "description", "verification_level",
                            "explicit_content_filter", "default_notifications",
                            "preferred_locale", "premium_progress_bar_enabled",
                            "invites_disabled", "raid_alerts_disabled",
                            "icon", "banner", "splash"}}
    if guild_patch:
        try:
            sub = await _run_guild_patch(ctx, guild_patch, reason)
            step("guild.basis", True, detail=sub.get("changed"))
            created["settings"] = {"guild": sub.get("changed")}
            await pause()
        except ApiError as exc:
            step("guild.basis", False, error=exc)
            if stop_on_error:
                aborted = "guild.basis"

    # ── 1. Rollen ────────────────────────────────────────────────────────────
    role_specs: List[Dict[str, Any]] = plan.get("roles") or []
    if len(role_specs) > MAX_ITEMS:
        raise ApiError.bad_request(f"roles: maximal {MAX_ITEMS} pro Setup.")
    pending_positions: Dict[discord.Role, int] = {}

    for index, spec in enumerate(role_specs):
        label = f"roles[{index}]"
        if not isinstance(spec, dict):
            step(label, False, detail="Eintrag muss ein Objekt sein.")
            continue
        existing = _find_existing_role(guild, spec)
        if existing is not None:
            keys[str(spec.get("key") or existing.name)] = existing
            step(f"{label}:{existing.name}", True, detail="existiert bereits — übersprungen")
            created["roles"].append({"key": spec.get("key"), "id": sf(existing.id),
                                     "name": existing.name, "reused": True})
            position = parse_int(spec.get("position"), field=f"{label}.position",
                                 minimum=0, maximum=300)
            if position is not None:
                pending_positions[existing] = position
            continue
        try:
            kwargs = _role_kwargs_from_spec(guild, dict(spec), index_label=label)
            kwargs = await _finalize_role_kwargs(guild, kwargs, session=ctx.http_session())
            kwargs["reason"] = spec.get("reason") or reason
            role = await guard(guild.create_role(**kwargs), action=f"Rolle '{kwargs['name']}' anlegen")
            await ctx.settle(0.35)
            role = guild.get_role(role.id) or role
            keys[str(spec.get("key") or role.name)] = role
            created["roles"].append({"key": spec.get("key"), "id": sf(role.id), "name": role.name})
            step(f"{label}:{role.name}", True, detail={"id": sf(role.id)})
            position = parse_int(spec.get("position"), field=f"{label}.position",
                                 minimum=0, maximum=300)
            if position is not None:
                pending_positions[role] = position
        except ApiError as exc:
            step(f"{label}:{spec.get('name')}", False, error=exc)
            if stop_on_error:
                aborted = label
                break
        await pause()

    # ── 2. Kategorien + deren Kanäle ─────────────────────────────────────────
    for cat_index, cat_spec in enumerate(plan.get("categories") or []):
        if aborted:
            break
        label = f"categories[{cat_index}]"
        if not isinstance(cat_spec, dict):
            step(label, False, detail="Eintrag muss ein Objekt sein.")
            continue

        cat_name = parse_str(cat_spec.get("name"), field=f"{label}.name", min_length=1,
                             max_length=100, allow_empty=False)
        if not cat_name:
            step(label, False, detail="name fehlt")
            continue

        existing = _find_existing_channel(guild, cat_name, discord.CategoryChannel)
        try:
            if existing is not None:
                category = existing
                step(f"{label}:{cat_name}", True, detail="existiert bereits — wird wiederverwendet")
            else:
                category = await create_channel(
                    guild, {**cat_spec, "type": "category"}, keys=keys, reason=reason, index=cat_index
                )
                await ctx.settle(0.4)
                step(f"{label}:{cat_name}", True, detail={"id": sf(category.id)})
            keys[str(cat_spec.get("key") or cat_name)] = category
            created["categories"].append({"key": cat_spec.get("key"), "id": sf(category.id),
                                          "name": category.name, "reused": existing is not None})
        except ApiError as exc:
            step(f"{label}:{cat_name}", False, error=exc)
            if stop_on_error:
                aborted = label
                break
            await pause()
            continue

        for ch_index, ch_spec in enumerate(cat_spec.get("channels") or []):
            if aborted:
                break
            ch_label = f"{label}.channels[{ch_index}]"
            if not isinstance(ch_spec, dict):
                step(ch_label, False, detail="Eintrag muss ein Objekt sein.")
                continue
            ch_name = parse_str(ch_spec.get("name"), field=f"{ch_label}.name",
                                min_length=1, max_length=100, allow_empty=False)
            if not ch_name:
                step(ch_label, False, detail="name fehlt")
                continue
            try:
                payload = dict(ch_spec)
                payload["category"] = category
                channel = await create_channel(guild, payload, keys=keys, reason=reason)
                await ctx.settle(0.4)
                channel_key = str(ch_spec.get("key") or ch_name)
                keys[channel_key] = channel
                created["channels"].append({
                    "key": ch_spec.get("key"), "id": sf(channel.id), "name": channel.name,
                    "type": str(getattr(channel.type, "name", "?")),
                    "category": category.name,
                })
                step(f"{ch_label}:{ch_name}", True, detail={"id": sf(channel.id)})
            except ApiError as exc:
                step(f"{ch_label}:{ch_name}", False, error=exc)
                if stop_on_error:
                    aborted = ch_label
            await pause()

    # ── 3. Kanäle ohne Kategorie ─────────────────────────────────────────────
    for index, ch_spec in enumerate(plan.get("channels") or []):
        if aborted:
            break
        label = f"channels[{index}]"
        if not isinstance(ch_spec, dict):
            step(label, False, detail="Eintrag muss ein Objekt sein.")
            continue
        try:
            channel = await create_channel(guild, ch_spec, keys=keys, reason=reason, index=index)
            await ctx.settle(0.4)
            keys[str(ch_spec.get("key") or ch_spec.get("name") or channel.id)] = channel
            created["channels"].append({"key": ch_spec.get("key"), "id": sf(channel.id),
                                        "name": channel.name,
                                        "type": str(getattr(channel.type, "name", "?")),
                                        "category": None})
            step(f"{label}:{getattr(channel, 'name', '?')}", True, detail={"id": sf(channel.id)})
        except ApiError as exc:
            step(f"{label}:{ch_spec.get('name')}", False, error=exc)
            if stop_on_error:
                aborted = label
        await pause()

    # ── 4. Rollen-Positionen (ein Call für alle) ─────────────────────────────
    if pending_positions and not aborted:
        try:
            await guard(guild.edit_role_positions(pending_positions, reason=reason),
                        action="Rollenpositionen setzen")
            await ctx.settle(0.7)
            step("roles.positionen", True,
                 detail=[{"name": r.name, "position": p} for r, p in pending_positions.items()])
        except ApiError as exc:
            step("roles.positionen", False, error=exc)

    # ── 5. Server-Einstellungen, die Kanäle brauchen ─────────────────────────
    settings = dict(plan.get("settings") or {})
    guild_late = {k: v for k, v in settings.items()
                  if k in {"system_channel", "system_channel_id", "rules_channel", "rules_channel_id",
                           "public_updates_channel", "public_updates_channel_id", "afk_channel",
                           "afk_channel_id", "safety_alerts_channel", "safety_alerts_channel_id",
                           "afk_timeout", "community", "auto_community_channels",
                           "verification_level", "explicit_content_filter",
                           "default_notifications", "preferred_locale", "vanity_code",
                           "mfa_level", "discoverable", "invites_disabled",
                           "premium_progress_bar_enabled", "raid_alerts_disabled",
                           "system_channel_flags"}}
    guild_late.update({k: v for k, v in (plan.get("guild") or {}).items()
                       if k in {"system_channel", "rules_channel", "public_updates_channel",
                                "afk_channel", "community", "auto_community_channels",
                                "verification_level", "explicit_content_filter",
                                "default_notifications", "preferred_locale", "vanity_code",
                                "mfa_level", "safety_alerts_channel", "afk_timeout"}})
    if guild_late and not aborted:
        try:
            resolved = _resolve_channel_refs(guild, guild_late, keys)
            result = await _run_guild_patch(ctx, resolved, reason)
            step("guild.einstellungen", True, detail=result.get("changed"))
            created["settings"] = {**(created.get("settings") or {}), **{"guild": result.get("changed")}}
            await pause()
        except ApiError as exc:
            step("guild.einstellungen", False, error=exc)
            if stop_on_error:
                aborted = "guild.einstellungen"

    # ── 6. Willkommensbildschirm ─────────────────────────────────────────────
    welcome = plan.get("welcome")
    if welcome and not aborted:
        try:
            from .guild import apply_welcome_screen

            payload = _resolve_welcome(guild, welcome, keys)
            await apply_welcome_screen(ctx, payload)
            step("welcome", True, detail=f"{len(payload.get('welcome_channels') or [])} Kanal-Karte(n)")
            await pause()
        except ApiError as exc:
            step("welcome", False, error=exc)

    # ── 7. Nachrichten ───────────────────────────────────────────────────────
    for index, msg_spec in enumerate(plan.get("messages") or []):
        if aborted:
            break
        label = f"messages[{index}]"
        if not isinstance(msg_spec, dict):
            step(label, False, detail="Eintrag muss ein Objekt sein.")
            continue
        try:
            channel = _resolve_channel_for_message(guild, msg_spec, keys)
            if not hasattr(channel, "send"):
                raise ApiError.bad_request(
                    f"messages[{index}]: in '{getattr(channel, 'name', channel.id)}' "
                    "kann nicht gesendet werden.",
                    code="CHANNEL_TYPE_MISMATCH",
                )
            from ..message_ops import build_message_kwargs

            kwargs, _notes = await build_message_kwargs(msg_spec, ctx=ctx, channel=channel)
            message = await guard(channel.send(**kwargs),
                                  action=f"Setup-Nachricht in '{channel.name}' senden")
            await ctx.settle(0.3)
            created["messages"].append({"channel": channel.name, "channel_id": sf(channel.id),
                                        "message_id": sf(message.id),
                                        "preview": serialize_message(message, detailed=False)})
            step(f"{label}:#{channel.name}", True, detail={"message_id": sf(message.id)})
        except ApiError as exc:
            step(f"{label}", False, error=exc)
            if stop_on_error:
                aborted = label
        await pause()

    # ── 8. Invites ───────────────────────────────────────────────────────────
    for index, invite_spec in enumerate(plan.get("invites") or []):
        if aborted:
            break
        label = f"invites[{index}]"
        if not isinstance(invite_spec, dict):
            step(label, False, detail="Eintrag muss ein Objekt sein.")
            continue
        try:
            channel = _resolve_channel_for_message(guild, invite_spec, keys)
            invite = await guard(
                channel.create_invite(
                    max_age=parse_int(invite_spec.get("max_age"), field=f"{label}.max_age",
                                      default=0, minimum=0, maximum=604800) or 0,
                    max_uses=parse_int(invite_spec.get("max_uses"), field=f"{label}.max_uses",
                                       default=0, minimum=0, maximum=100) or 0,
                    unique=bool(parse_bool(invite_spec.get("unique"), field=f"{label}.unique",
                                           default=True)),
                    reason=reason,
                ),
                action="Invite erstellen",
            )
            created["invites"].append({"channel": channel.name, "code": invite.code,
                                       "url": invite.url})
            step(f"{label}:#{channel.name}", True, detail=invite.url)
        except ApiError as exc:
            step(label, False, error=exc)
        await pause()

    # ── 9. AutoMod ───────────────────────────────────────────────────────────
    for index, rule_spec in enumerate(plan.get("automod") or []):
        if aborted:
            break
        label = f"automod[{index}]"
        if not isinstance(rule_spec, dict):
            step(label, False, detail="Eintrag muss ein Objekt sein.")
            continue
        try:
            from .moderation import _build_trigger

            name = parse_str(rule_spec.get("name"), field=f"{label}.name", min_length=1,
                             max_length=100, allow_empty=False)
            if not name:
                raise ApiError.bad_request(f"{label}.name fehlt.")
            from ...util import parse_enum

            event_type = parse_enum(
                discord.AutoModRuleEventType, rule_spec.get("event_type", "message_send"),
                field=f"{label}.event_type", default=discord.AutoModRuleEventType.message_send,
            )
            trigger = _build_trigger(dict(rule_spec), field=f"{label}")
            actions = _build_actions_with_keys(guild, rule_spec.get("actions"), keys, field=f"{label}.actions")

            exempt_roles = [
                keys[str(item)] if isinstance(item, str) and item in keys
                else _resolve_role_ref(guild, item, keys)
                for item in (rule_spec.get("exempt_roles") or [])
            ]
            exempt_channels = [
                keys[str(item)] if isinstance(item, str) and item in keys
                else _resolve_channel_ref(guild, item, keys)
                for item in (rule_spec.get("exempt_channels") or [])
            ]

            rule = await guard(
                guild.create_automod_rule(
                    name=name,
                    event_type=event_type or discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=bool(parse_bool(rule_spec.get("enabled"), field=f"{label}.enabled",
                                            default=True)),
                    exempt_roles=exempt_roles or MISSING,
                    exempt_channels=exempt_channels or MISSING,
                    reason=reason,
                ),
                action=f"AutoMod-Regel '{name}' anlegen",
            )
            created["automod"].append({"id": sf(rule.id), "name": rule.name})
            step(f"{label}:{name}", True, detail={"id": sf(rule.id)})
        except ApiError as exc:
            step(label, False, error=exc)
            if stop_on_error:
                aborted = label
        await pause()

    # ── Abschluss ────────────────────────────────────────────────────────────
    await ctx.settle(1.0)
    fresh = ctx.client.get_guild(guild.id) or guild

    ok_count = sum(1 for entry in report if entry["ok"])
    fail_count = len(report) - ok_count
    return {
        "ok": fail_count == 0 and aborted is None,
        "aborted_at": aborted,
        "steps_total": len(report),
        "steps_ok": ok_count,
        "steps_failed": fail_count,
        "report": report,
        "warnings": issues["warnings"] or None,
        "keys": {key: sf(getattr(value, "id", value)) for key, value in keys.items()},
        "created": created,
        "guild": serialize_guild(fresh, detailed=False),
        "channels": [serialize_channel(c) for c in sorted(fresh.channels, key=lambda c: (c.position, c.id))],
        "roles": [serialize_role(r) for r in sorted(fresh.roles, key=lambda r: r.position, reverse=True)],
        "next_steps": [
            "GET /api/v1/guild/snapshot — Ergebnis verifizieren",
            "GET /api/v1/channels/tree — Struktur prüfen",
            "PATCH /api/v1/channels/positions — Feinschliff der Reihenfolge",
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Helfer
# ─────────────────────────────────────────────────────────────────────────────


def _count_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    categories = plan.get("categories") or []
    channel_count = len(plan.get("channels") or [])
    for category in categories:
        if isinstance(category, dict):
            channel_count += len(category.get("channels") or [])
    return {
        "roles": len(plan.get("roles") or []),
        "categories": len(categories),
        "channels": channel_count,
        "messages": len(plan.get("messages") or []),
        "invites": len(plan.get("invites") or []),
        "automod_rules": len(plan.get("automod") or []),
        "guild_fields": len(plan.get("guild") or {}),
        "settings_fields": len(plan.get("settings") or {}),
    }


def _validate_plan(guild: discord.Guild, plan: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Prüft einen Plan, ohne ihn auszuführen (für Preview und als Vorab-Schutz)."""
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    from ...util import parse_permissions

    def err(where: str, message: str) -> None:
        errors.append({"where": where, "message": message})

    def warn(where: str, message: str) -> None:
        warnings.append({"where": where, "message": message})

    if not isinstance(plan, dict):
        err("plan", "Der Plan muss ein JSON-Objekt sein.")
        return {"errors": errors, "warnings": warnings}

    role_keys = set()
    for index, spec in enumerate(plan.get("roles") or []):
        where = f"roles[{index}]"
        if not isinstance(spec, dict):
            err(where, "muss ein Objekt sein")
            continue
        if not spec.get("name") and not spec.get("preset"):
            err(f"{where}.name", "fehlt (oder 'preset' angeben)")
        if spec.get("key"):
            if spec["key"] in role_keys:
                err(f"{where}.key", f"doppelt vergeben: '{spec['key']}'")
            role_keys.add(str(spec["key"]))
        try:
            if spec.get("permissions") is not None:
                parse_permissions(spec["permissions"], field=f"{where}.permissions")
        except ApiError as exc:
            err(f"{where}.permissions", exc.message)
        if spec.get("preset") and str(spec["preset"]).lower() not in ROLE_PRESETS:
            err(f"{where}.preset", f"unbekannt — verfügbar: {', '.join(sorted(ROLE_PRESETS))}")

    total_channels = 0
    for cat_index, cat in enumerate(plan.get("categories") or []):
        where = f"categories[{cat_index}]"
        if not isinstance(cat, dict):
            err(where, "muss ein Objekt sein")
            continue
        if not cat.get("name"):
            err(f"{where}.name", "fehlt")
        for ch_index, channel in enumerate(cat.get("channels") or []):
            total_channels += 1
            _validate_channel(f"{where}.channels[{ch_index}]", channel, err)
    for index, channel in enumerate(plan.get("channels") or []):
        total_channels += 1
        _validate_channel(f"channels[{index}]", channel, err)

    if total_channels > 500:
        warn("channels", f"{total_channels} Kanäle — Discord-Limit sind 500 pro Server.")
    if len(plan.get("roles") or []) > 250:
        warn("roles", "Discord erlaubt maximal 250 Rollen pro Server.")

    for index, msg in enumerate(plan.get("messages") or []):
        where = f"messages[{index}]"
        if not isinstance(msg, dict):
            err(where, "muss ein Objekt sein")
            continue
        if not msg.get("channel"):
            err(f"{where}.channel", "fehlt — nutze einen 'key', '#name' oder eine ID")
        if not any(msg.get(k) for k in ("content", "embeds", "attachments", "poll", "stickers")):
            warn(where, "Nachricht wäre leer (kein content/embeds/attachments).")

    for index, rule in enumerate(plan.get("automod") or []):
        where = f"automod[{index}]"
        if not isinstance(rule, dict):
            err(where, "muss ein Objekt sein")
            continue
        if not rule.get("name"):
            err(f"{where}.name", "fehlt")
        if not any(rule.get(k) for k in ("keyword_filter", "regex_patterns", "presets",
                                         "mention_limit")) and rule.get("trigger_type") not in {
                                             "spam", "harmful_link"}:
            warn(where, "Regel hat keinen Trigger-Inhalt (keyword_filter/regex_patterns/"
                        "presets/mention_limit).")

    settings = plan.get("settings") or {}
    if settings.get("community") is True and not (
        settings.get("rules_channel") and settings.get("public_updates_channel")
    ) and not settings.get("auto_community_channels"):
        warn("settings.community", "Discord braucht rules_channel UND public_updates_channel "
                                   "im selben Aufruf — oder 'auto_community_channels': true.")

    return {"errors": errors, "warnings": warnings}


def _validate_channel(where: str, spec: Any, err) -> None:
    from ...util import channel_type_from_name

    if not isinstance(spec, dict):
        err(where, "muss ein Objekt sein")
        return
    if not spec.get("name"):
        err(f"{where}.name", "fehlt")
    try:
        channel_type_from_name(spec.get("type"), field=f"{where}.type")
    except ApiError as exc:
        err(f"{where}.type", exc.message)


def _find_existing_role(guild: discord.Guild, spec: Dict[str, Any]) -> Optional[discord.Role]:
    """Verhindert Doppelanlage: sucht eine Rolle mit gleichem Namen."""
    name = parse_str(spec.get("name"), field="name", max_length=100, default=None)
    if not name:
        preset = spec.get("preset")
        if preset and str(preset).lower() in ROLE_PRESETS:
            name = ROLE_PRESETS[str(preset).lower()]["name"]
    if not name:
        return None
    lowered = name.lower()
    for role in guild.roles:
        if role.name.lower() == lowered and not role.is_default():
            return role
    return None


def _find_existing_channel(guild: discord.Guild, name: str, kind: type) -> Optional[Any]:
    lowered = name.lower()
    for channel in guild.channels:
        if isinstance(channel, kind) and channel.name.lower() == lowered:
            return channel
    return None


def _resolve_key(keys: Mapping[str, Any], value: str) -> Optional[Any]:
    if value in keys:
        return keys[value]
    lowered = value.lower()
    for key, item in keys.items():
        if key.lower() == lowered:
            return item
    return None


def _resolve_role_ref(guild: discord.Guild, value: Any, keys: Mapping[str, Any]) -> discord.Role:
    from ...util import resolve_role

    if isinstance(value, discord.Role):
        return value
    if isinstance(value, str):
        resolved = _resolve_key(keys, value)
        if isinstance(resolved, discord.Role):
            return resolved
    return resolve_role(guild, value)


def _resolve_channel_ref(guild: discord.Guild, value: Any, keys: Mapping[str, Any]) -> Any:
    from ..channel_ops import resolve_channel_by_ref

    if isinstance(value, str):
        resolved = _resolve_key(keys, value)
        if resolved is not None:
            return resolved
    return resolve_channel_by_ref(guild, value, keys=keys)


def _resolve_channel_refs(
    guild: discord.Guild, settings: Dict[str, Any], keys: Mapping[str, Any]
) -> Dict[str, Any]:
    """Übersetzt key-Referenzen in Server-Einstellungen zu echten Kanal-Objekten."""
    channel_keys = {
        "system_channel", "rules_channel", "public_updates_channel", "afk_channel",
        "safety_alerts_channel",
    }
    resolved: Dict[str, Any] = {}
    for key, value in settings.items():
        base = key[:-3] if key.endswith("_id") else key
        if base in channel_keys and value is not None:
            resolved[base] = _resolve_channel_ref(guild, value, keys)
        else:
            resolved[key] = value
    return resolved


def _resolve_channel_for_message(guild: discord.Guild, spec: Dict[str, Any],
                                 keys: Mapping[str, Any]) -> Any:
    value = spec.get("channel") or spec.get("channel_id") or spec.get("in")
    if value is None:
        raise ApiError.bad_request("'channel' fehlt.")
    return _resolve_channel_ref(guild, value, keys)


def _resolve_welcome(guild: discord.Guild, welcome: Dict[str, Any],
                     keys: Mapping[str, Any]) -> Dict[str, Any]:
    """Übersetzt Setup-``welcome`` in das Format von PATCH /guild/welcome-screen."""
    payload: Dict[str, Any] = {}
    if "enabled" in welcome:
        payload["enabled"] = welcome["enabled"]
    if "description" in welcome:
        payload["description"] = welcome["description"]
    channels: List[Dict[str, Any]] = []
    for entry in welcome.get("welcome_channels") or []:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("channel") or entry.get("channel_id")
        channel = _resolve_channel_ref(guild, ref, keys)
        channels.append({
            "channel_id": sf(channel.id),
            "description": entry.get("description", ""),
            "emoji": entry.get("emoji"),
        })
    if channels:
        payload["welcome_channels"] = channels
    return payload


async def _run_guild_patch(ctx: Ctx, fields: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """
    Führt die Logik von ``PATCH /api/v1/guild`` intern aus.

    Der Setup-Wizard ruft bewusst dieselbe ``apply_guild_patch``-Funktion auf wie
    der HTTP-Handler — gleiche Validierung, gleiche Fehlermeldungen, kein
    doppelter Code-Pfad.
    """
    from .guild import apply_guild_patch

    payload = dict(fields)
    payload.setdefault("reason", reason)
    result = await apply_guild_patch(ctx, payload, reason_default=reason)
    return result if isinstance(result, dict) else {"result": result}


def _build_actions_with_keys(guild: discord.Guild, raw: Any, keys: Mapping[str, Any],
                             *, field: str = "actions") -> List[discord.AutoModRuleAction]:
    """Wie ``_build_actions``, löst Kanal-Keys aus dem laufenden Setup auf."""
    from .moderation import _build_actions

    if not isinstance(raw, list):
        return _build_actions(guild, raw, field=field)
    normalized: List[Any] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, dict) and entry.get("channel") is not None:
            ref = entry["channel"]
            resolved = _resolve_key(keys, ref) if isinstance(ref, str) else None
            if resolved is not None:
                entry = {**entry, "channel": sf(getattr(resolved, "id", resolved))}
        normalized.append(entry)
    return _build_actions(guild, normalized, field=field)
