"""
Fertige Anleitungen („Guides”), die Arena AI 1:1 an den Nutzer weitergeben kann.

Warum ein eigener Endpoint? Die KI stößt bei manchen Wünschen an echte Grenzen
— der klassische Fall sind **Self Roles / Reaction Roles**: Das Einrichten
erfolgt über Drittanbieter-Bots, und ein Bot darf die Konfiguration eines
anderen Bots nicht anfassen. Genau dann braucht es nicht Ausreden, sondern
eine ehrliche Erklärung **plus** eine Schritt-für-Schritt-Anleitung, die der
Nutzer selbst ausführen kann.

Die Guides sind bewusst als Daten (statt als Prompt-Text) hinterlegt:

* ``GET /api/v1/guides`` listet die Themen,
* ``GET /api/v1/guides/{topic}`` liefert den ausformulierten Text,
  den die KI in den Kanal (oder als Chat-Antwort) übernehmen und an den
  konkreten Server anpassen darf.

Alle Texte sind Deutsch, in Discord-Markdown formuliert und mit konkreten
Kanal-/Rollen-Platzhaltern versehen, die die KI gegen echte IDs auflöst.
"""

from __future__ import annotations

from typing import Any, Dict

from ...util import ApiError
from ..registry import route

__all__ = ("GUIDES",)


def _self_roles_guide() -> Dict[str, Any]:
    return {
        "titel": "Self Roles / Reaction Roles einrichten — ehrliche Anleitung",
        "beschreibung": (
            "Was die KI selbst erledigen kann, was nur ein Mensch/dritter Bot "
            "kann, und eine Schritt-für-Schritt-Anleitung für den Nutzer."
        ),
        "wichtig_fuer_ki": [
            "Bots dürfen die Konfiguration anderer Bots NICHT ändern — Self-Role-"
            "Mechaniken (Reaktion ⇒ Rolle) laufen immer über einen Drittanbieter-Bot.",
            "Die KI darf und SOLL aber alles drumherum bauen: Rollen anlegen "
            "(Farbe, Name, Rechte), den Kanal inkl. Sperre für @everyone, die "
            "Übersichts-Nachricht (als Webhook-Persona) und diese Anleitung posten.",
            "Die KI DARF Slash-Commands anderer Bots aufrufen (z. B. /reactionrole, "
            "/rr add), um Ablauf und Optionen des installierten Bots zu erkunden — "
            "das macht die Anleitung konkreter. Konfigurieren muss sie trotzdem nicht.",
            "Erst prüfen, was installiert ist: GET /api/v1/members?bots=true — "
            "danach den passenden Abschnitt der Anleitung wählen.",
            "Natives Detail: Auf Community-Servern kann Discord-Onboarding "
            "(Server-Onboarding mit Rollen-Auswahl) Self Roles teilweise ersetzen — "
            "das DARF die KI selbst einrichten: PATCH /api/v1/guild/onboarding.",
        ],
        "schritte_nutzer": [
            "Prüfe, ob bereits ein Rollen-Bot auf dem Server ist "
            "(Servereinstellungen → Integrationen, oder Mitgliederliste nach Bots filtern).",
            "Fehlt einer: Lade einen geeigneten Bot über dessen Website mit der "
            "'Zum Server einladen'-Funktion (braucht die Berechtigung 'Rollen verwalten').",
            "Öffne das Dashboard des Bots (meist /dashboard oder Link in der Bot-"
            "Beschreibung) und wähle 'Reaction Roles' bzw. 'Rollen-Auswahl'.",
            "Wähle den vorbereiteten Kanal (z. B. #rollen-wahl) und die vorbereitete "
            "Nachricht des Bots als Ziel.",
            "Lege pro Rolle eine Zeile an: Rolle + Emoji. Nutze die Rollen, die "
            "Arena AI bereits erstellt hat (Namen stehen in der Übersichtsnachricht).",
            "Speichern — der Bot postet/verlinkt die Nachricht; erledigt. Reaktion "
            "drauf klicken ⇒ Rolle kommt sofort.",
        ],
        "bot_anleitungen": {
            "carl-bot": (
                "/reactionrole → 'Create' → Kanal wählen → im nächsten Schritt pro Zeile "
                "'Emoji Rolle' eintragen (Rollen per @vervollständigung). Carl-bot "
                "erlaubt auch Buttons statt Emojis (Mode: Buttons)."
            ),
            "dyno": (
                "web.dyno.gg → Modules → Reaction Roles → 'Add Reaction Role' → Kanal "
                "und Nachricht wählen → Rollen + Emojis zuordnen → Save."
            ),
            "mee6": (
                "mee6.xyz/dashboard → deinem Server wählen → Reaction Roles → "
                "'Create' → Kanal, Nachricht, Emoji→Rolle-Paare festlegen."
            ),
            "yagpdb": (
                "yagpdb.xyz/manage → Tools & Utilities → Reaction Roles → 'New Group' "
                "→ Modus 'Only one' für exklusive Farben, 'Many' für freie Rollen → "
                "Emoji→Rolle-Mapping anlegen."
            ),
            "reaction-roles /ReactionRoles": (
                "/reactionroles create → Kanal wählen → eigene Nachricht oder "
                "'Use existing message' → pro Rolle Emoji wählen und Rolle zuordnen."
            ),
            "arcane": "arcane.bot/dashboard → Leveling/Roles → 'Reaction Roles' → Assistent folgen.",
            "sonstige": "Fast jeder größere Bot hat ein Dashboard: Servereinstellungen → Integrationen → Bot auswählen → Dashboard öffnen.",
        },
        "text_vorlage": (
            "**Rollen selbst auswählen — so geht's** 🎨\n\n"
            "Ich habe alles vorbereitet: die Rollen ({rollenliste}) und diesen Kanal. "
            "Einen Haken gibt es: **Die Auswahl selbst übernimmt ein anderer Bot** — "
            "Bots dürfen andere Bots nicht konfigurieren, das ist eine Discord-Regel. "
            "Deshalb hier die 3-Minuten-Anleitung für dich:\n\n"
            "1️⃣ **Bot prüfen/laden** —Invite-Link: {bot_einladung}\n"
            "2️⃣ **Dashboard öffnen** — {dashboard_hinweis}\n"
            "3️⃣ **Reaction Roles anlegen** — {bot_schritte}\n"
            "4️⃣ **Fertig** — Emoji anklicken ⇒ Rolle ist sofort da ✅\n\n"
            "*Tipp: Alternativ kann Discord selbst Rollen beim Beitritt anbieten — "
            "das nennt sich Server-Onboarding und kann komplett eingerichtet werden; "
            "sag einfach Bescheid.*"
        ),
        "was_die_ki_ubernehmen_kann": [
            "Rollen anlegen: POST /api/v1/roles (Name, Farbe, Rechte, Icon)",
            "Kanal 'rollen-wahl' anlegen + @everyone Schreibrechte entziehen (Overwrites)",
            "Übersichts-Nachricht posten (Webhook-Persona, alle Rollen mit echten "
            "<@&id>-Mentions auflisten)",
            "Onboarding mit Rollen-Prompts konfigurieren (Community-Server)",
            "Diese Anleitung als Nachricht im Kanal hinterlassen",
        ],
    }


def _design_guide() -> Dict[str, Any]:
    return {
        "titel": "Unicode-Design-System für Kanäle, Kategorien & Rollen",
        "beschreibung": (
            "Stilvolle Kanalnamen mit Unicode-Zeichen statt langweiliger "
            "Kleinbuchstaben-Kanäle."
        ),
        "regeln": [
            "Text-Kanäle: Discord erzwingt Kleinbuchstaben und wandelt Leerzeichen "
            "in Bindestriche — Unicode-Zeichen und Emojis bleiben aber erhalten.",
            "Kategorien & Voice-Kanäle: erlauben Großbuchstaben und Leerzeichen.",
            "Ein Stil pro Server — nicht mischen. Emojis zusätzlich zum Symbol "
            "sparen Platz und bleiben auch dann lesbar, wenn Symbole fehlen.",
            " Weniger ist mehr: 1 Symbol + Trennzeichen + Begriff reicht; "
            "Rollen-/Kanallisten müssen scannbar bleiben.",
        ],
        "stile": {
            "klammern": {
                "beispiel_kategorie": "「📌」 INFORMATION",
                "beispiel_kanal": "「✦」regeln",
                "beispiel_voice": "「🎧」 Lounge",
                "beispiel_rolle": "「👑」 Owner",
                "zeichen": "「 」",
            },
            "trennpunkt": {
                "beispiel_kategorie": "📌・INFORMATION",
                "beispiel_kanal": "✦・regeln",
                "beispiel_voice": "🎧・Lounge",
                "beispiel_rolle": "⭐・VIP",
                "zeichen": "・(katakana middle dot)",
            },
            "rahmen": {
                "beispiel_kategorie": "▁▁▁ 📌 REGELN ▁▁▁",
                "beispiel_kanal": "▬▬inline-regeln",
                "beispiel_voice": "╭──── Lounge ────╮",
                "beispiel_rolle": "꒰ 👑 ꒱ Owner",
                "zeichen": "▁ ▬ ╭ ╮ ꒰ ꒱",
            },
            "symbole": {
                "beispiel_kanal": "✧ regeln · ✧ infos · ✧ willkommen",
                "beispiel_kategorie": "❖ COMMUNITY ❖",
                "beispiel_rolle": "★ Gamer ★",
                "zeichen": "✧ ✦ ❖ ★ ☆ ✵ ♡ ➤",
            },
            "spezialbuchstaben": {
                "fullwidth": "ｒｅｇｅｌｎ · ｃｈａｔ · ｉｎｆｏｓ",
                "smallcaps": "ʀᴇɢᴇʟɴ · ᴄʜᴀᴛ · ɪɴꜰᴏꜱ",
                "hochgestellt": "ʳᵉᵍᵉˡⁿ · ᶜʰᵃᵗ",
                "fett": "𝗋𝖾𝗀𝖾𝗅𝗇 (mathematische Buchstaben)",
                "hinweis": "Für Akzente & einzelne Bereiche — ganze Server nur in "
                           "Smallcaps werden schwer lesbar und schwer zu erwähnen.",
            },
            "trennlinien": {
                "kanal": "┃ infos · ┃ regeln",
                "kategorie": "═══ ⚡ SPIELE ═══",
                "zeichen": "┃ ═ ══ ─ ┄ ┈ » «",
            },
        },
        "komplett_beispiel": {
            "kategorien": ["「📌」 INFORMATION", "「💬」 COMMUNITY", "「🎮」 SPIELE", "「🔒」 TEAM"],
            "text_kanaele": ["「✦」regeln", "「✦」ankündigungen", "「✦」rollen-wahl",
                             "✦・allgemein", "✦・memes", "✦・clips"],
            "voice_kanaele": ["「🎧」 Lounge", "「🎧」 Gaming", "「🎧」 AFK"],
            "rollen": ["「👑」 Owner", "「🛡️」 Mod", "「⭐」 VIP", "「🎮」 Gamer"],
        },
        "hinweise": [
            "Vorsicht bei kombinierten Unicode-Buchstaben (fett/smallcaps) in Kanalnamen "
            "von Textkanälen: Sie bleiben erhalten, aber Discord-Suche/Autovervollständigung "
            "findet sie schlechter — deshalb für Textkanäle lieber Symbol-Stile.",
            "Voice-Kanal-Status (Boost) und Kanal-Themen sind der richtige Ort für "
            "Verzierungen, wenn der Name clean bleiben soll.",
        ],
    }


def _branding_guide() -> Dict[str, Any]:
    return {
        "titel": "Branding-Paket: Server-Bilder & Bot-Profil",
        "beschreibung": (
            "Alles Optische in einem Durchgang: Server-Icon, Banner, Splash, "
            "Bot-Avatar pro Server, Bot-Nickname, Rollenfarben & -Icons, "
            "Webhook-Avatare."
        ),
        "endpoints": {
            "server_icon": 'PATCH /api/v1/guild {"icon": "https://…/icon.png"}',
            "server_banner": 'PATCH /api/v1/guild {"banner": "https://…/banner.png"} (Boost-Stufe 2)',
            "invite_splash": 'PATCH /api/v1/guild {"splash": "https://…/splash.png"} (Boost-Stufe 1)',
            "bot_avatar_dieser_server": 'PATCH /api/v1/members/me {"avatar": "https://…"}',
            "bot_nickname": 'PATCH /api/v1/members/me {"nick": "✨ Server-Assistent"}',
            "bot_bio": 'PATCH /api/v1/members/me {"bio": "…"}',
            "rollen_farbe_icon": 'PATCH /api/v1/roles/{id} {"color": "#E74C3C", "icon": "🛡️"}',
            "webhook_avatar": 'PATCH /api/v1/webhooks/{id} {"avatar": "https://…"}',
            "webhook_persona_pro_nachricht": '{"webhook": {"name": "📜 Regeln", "avatar": "https://…"}}',
        },
        "checkliste": [
            "Server-Icon: quadratisch, ≥512×512, PNG (wird rund angezeigt)",
            "Banner: 960×540 (16:9), Boost-Stufe 2 nötig",
            "Splash (Invite-Hintergrund): 1920×1080, Boost-Stufe 1",
            "Bot-Avatar pro Server — unabhängig vom globalen Bot-Bild",
            "Einheitliche Farbpalette für Rollen (z. B. Rot=Trial, Blau=Team, "
            "Grün=Mitglied, Grau=Bot)",
            "Rollen-Icons (Emoji oder PNG) für Premium-Look",
            "Webhook-Personen mit passenden Avataren für Regeln/News/Willkommen",
        ],
        "hinweis": "Bilder dürfen auch als Data-URI (data:image/png;base64,…) übergeben werden.",
    }


def _webhook_guide() -> Dict[str, Any]:
    return {
        "titel": "Webhook-Personen: Nachrichten mit eigenem Namen & Avatar",
        "beschreibung": (
            "Warum Webhooks statt Bot-Nachrichten und wie der Persona-Werkfluss aussieht."
        ),
        "warum": [
            "Bot-Nachrichten zeigen immer den Bot-Namen — der Kanal wirkt technisch.",
            "Webhook-Nachrichten zeigen beliebigen Namen + Avatar: '📜 Serverregeln', "
            "'🛡️ Moderation', '🎉 Events' — der Server wirkt kuratiert.",
            "Ein Webhook pro Kanal genügt: Name/Avatar lassen sich pro Nachricht übersteuern.",
            "Webhook-Nachrichten sind editier-/löschbar wie normale Nachrichten.",
        ],
        "werkfluss": [
            'Kurzel: POST /api/v1/channels/{id}/messages mit {"webhook": {"name": "📜 Serverregeln", '
            '"avatar": "https://…/regeln.png"}} — legt bei Bedarf automatisch einen Webhook an.',
            "Alternative: POST /api/v1/webhooks/{id}/send mit username/avatar pro Nachricht.",
            "Avatar dauerhaft für alle Nachrichten: PATCH /api/v1/webhooks/{id} {\"avatar\": …}.",
            "Vorhandene Personen: GET /api/v1/webhooks — wiederverwenden statt neu anlegen "
            "(Limit 10 pro Kanal).",
        ],
        "grenzen": [
            "Keine Umfragen-Threads, keine Sticker, kein Antwort-Bezug (reply) bei Webhooks — "
            "solche Features braucht der seltene Fall, dann normal senden.",
            "Webhooks zählen zum Kanal-Limit (10) — Persona-Pattern nutzt deshalb einen pro Kanal.",
        ],
    }


def _security_guide() -> Dict[str, Any]:
    return {
        "titel": "Sicherheit & Anti-Raid-Paket",
        "beschreibung": "Moderatives Grundgerüst, das praktisch jeder Server haben sollte.",
        "checkliste": [
            'Verifizierungsstufe: PATCH /api/v1/guild {"verification_level": "medium"}',
            'Inhaltsfilter: PATCH /api/v1/guild {"explicit_content_filter": "all_members"}',
            "AutoMod: Werbung blockieren (discord.gg-Regex), Beleidigungs-Presets, "
            "Mention-Spam-Limit 6 → POST /api/v1/automod/rules",
            "Muted-Rolle ohne Schreibrechte + passender Overwrite in neuen Kanälen",
            "Team-Kategorie nur für Mod-Rollen sichtbar (@everyone deny view_channel)",
            "Regeln-Kanal: @everyone deny send_messages — nur Team darf posten",
            "Mod-Log-Kanal für AutoMod-Alerts (send_alert_message)",
            "Invite-Hygiene: GET /api/v1/invites → alte/unkontrollierte Links löschen",
        ],
    }


GUIDES: Dict[str, Dict[str, Any]] = {
    "self-roles": _self_roles_guide(),
    "design": _design_guide(),
    "branding": _branding_guide(),
    "webhooks": _webhook_guide(),
    "security": _security_guide(),
}


@route(
    "GET", "/api/v1/guides", scope="read", tags=("guides",),
    summary="Fertige Anleitungen (Self Roles, Design, Branding, Webhooks, Security)",
    description="Ausformulierte deutsche Guides, die 1:1 an den Nutzer weitergegeben "
                "werden dürfen — inkl. der ehrlichen Grenzen ('Bots dürfen andere "
                "Bots nicht konfigurieren') und was die KI stattdessen übernehmen kann.",
)
async def list_guides(ctx) -> Dict[str, Any]:
    return {
        "count": len(GUIDES),
        "guides": [
            {"topic": key, "titel": guide.get("titel"), "beschreibung": guide.get("beschreibung")}
            for key, guide in sorted(GUIDES.items())
        ],
        "usage": "GET /api/v1/guides/self-roles → Text anpassen (echte IDs, echte Rollen) "
                 "→ als Webhook-Persona im Kanal posten.",
    }


@route(
    "GET", "/api/v1/guides/{topic}", scope="read", tags=("guides",),
    summary="Eine Anleitung im Detail",
    description="Der Inhalt darf und soll vom Nutzer sichtbar weiterverwendet werden: "
                "an den Server anpassen (echte Kanal-/Rollen-IDs einsetzen!) und "
                "als Nachricht posten.",
)
async def get_guide(ctx) -> Dict[str, Any]:
    topic = (ctx.path("topic") or "").strip().lower()
    aliases = {"unicode": "design", "design-unicode": "design", "styling": "design",
               "selfroles": "self-roles", "reaction-roles": "self-roles",
               "reactionroles": "self-roles", "branding-paket": "branding",
               "security-hardening": "security"}
    topic = aliases.get(topic, topic)
    if topic not in GUIDES:
        raise ApiError.not_found(
            f"Anleitung '{topic}' gibt es nicht.",
            hint="Verfügbar: " + ", ".join(sorted(GUIDES)),
            code="GUIDE_NOT_FOUND",
        )
    return {"topic": topic, **GUIDES[topic]}
