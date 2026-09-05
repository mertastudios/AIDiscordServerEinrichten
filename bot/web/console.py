"""
Die **Console** — eine einzige, self-contained HTML-Seite unter ``/console``.

Warum eine Seite statt nur JSON?
--------------------------------
Discord kann lange Texte in einem Codeblock nicht bequem kopierbar machen, und
ein ephemeraler Chat-Verlauf verschwindet. Die Console löst das:

* Token kommt per ``?t=…`` aus dem Discord-Button (oder wird eingetippt).
* Ein Klick kopiert den kompletten Arena-AI-Prompt in die Zwischenablage.
* Live-Status: Ist der Bot verbunden? Läuft das Token bald ab?
* Action-Log: jede API-Aktion der KI ist nachvollziehbar.
* API-Tester: Endpoints direkt im Browser ausprobieren.
* Widerruf: Zugriff sofort beenden.

Alles läuft über dieselbe REST-API wie die KI — die Seite hat keine eigenen
Rechte und kein eigenes Geheimnis.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ..config import Config

log = logging.getLogger("relay.console")

__all__ = ("register_console_routes", "CONSOLE_HTML")


CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<meta name="referrer" content="no-referrer">
<title>AIDiscordServerEinrichten · Console</title>
<style>
:root{
  --bg:#0b0d14; --bg2:#11141f; --card:#151926; --card2:#1b2032;
  --line:#252c42; --line2:#313a58;
  --txt:#e8ebf5; --mut:#8b93ad; --dim:#5d6580;
  --acc:#5865f2; --acc2:#7c87ff; --ok:#2ecc71; --warn:#f1c40f; --err:#e74c3c;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:
    radial-gradient(1200px 600px at 12% -8%, rgba(88,101,242,.22), transparent 60%),
    radial-gradient(900px 500px at 92% 4%, rgba(235,69,158,.13), transparent 62%),
    var(--bg);
  color:var(--txt); font-family:var(--sans); line-height:1.55;
  -webkit-font-smoothing:antialiased; min-height:100vh;
}
.wrap{max-width:1180px;margin:0 auto;padding:26px 18px 70px}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;gap:13px;align-items:center;min-width:0}
.logo{
  width:46px;height:46px;border-radius:14px;flex:0 0 auto;display:grid;place-items:center;
  background:linear-gradient(140deg,var(--acc),#eb459e);font-size:24px;
  box-shadow:0 8px 26px rgba(88,101,242,.42);
}
h1{font-size:19px;margin:0;letter-spacing:-.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{color:var(--mut);font-size:12.5px;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:999px;
  font-size:12px;font-weight:600;border:1px solid var(--line2);background:var(--card)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:0 0 auto}
.dot.on{background:var(--ok);box-shadow:0 0 0 4px rgba(46,204,113,.16);animation:pulse 2.4s infinite}
.dot.off{background:var(--err);box-shadow:0 0 0 4px rgba(231,76,60,.16)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}

.card{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line);
  border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 12px 34px rgba(0,0,0,.28)}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);
  margin:0 0 14px;font-weight:700}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>*{min-width:0}
.grow{flex:1 1 260px}

input,textarea,select,button{font-family:inherit;font-size:14px;color:var(--txt)}
input,textarea,select{background:#0e1120;border:1px solid var(--line2);border-radius:10px;
  padding:10px 12px;outline:none;width:100%}
input:focus,textarea:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(88,101,242,.18)}
input.mono,textarea.mono{font-family:var(--mono);font-size:12.5px}
textarea{resize:vertical;min-height:96px}

.btn{display:inline-flex;align-items:center;gap:8px;justify-content:center;cursor:pointer;
  border-radius:10px;border:1px solid var(--line2);background:var(--card2);color:var(--txt);
  padding:10px 15px;font-weight:600;font-size:13.5px;transition:.15s;white-space:nowrap;text-decoration:none}
.btn:hover{border-color:var(--acc);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn.primary{background:linear-gradient(135deg,var(--acc),#4752c4);border-color:transparent;
  box-shadow:0 8px 22px rgba(88,101,242,.34)}
.btn.primary:hover{box-shadow:0 10px 26px rgba(88,101,242,.46)}
.btn.danger{background:rgba(231,76,60,.13);border-color:rgba(231,76,60,.42);color:#ff8b7d}
.btn.danger:hover{background:rgba(231,76,60,.22);border-color:var(--err)}
.btn.big{padding:14px 22px;font-size:15px}
.btn[disabled]{opacity:.45;cursor:not-allowed;transform:none}

.grid{display:grid;gap:12px}
.g2{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
.stat{background:#0e1120;border:1px solid var(--line);border-radius:12px;padding:13px 14px}
.stat .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;font-weight:700}
.stat .v{font-size:21px;font-weight:700;margin-top:3px;letter-spacing:-.02em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stat .v.sm{font-size:14px;font-weight:600;white-space:normal}

pre{background:#080a12;border:1px solid var(--line2);border-radius:12px;padding:14px;
  overflow:auto;font-family:var(--mono);font-size:12px;line-height:1.6;margin:0;
  white-space:pre-wrap;word-break:break-word;color:#cfd6ee;max-height:520px}
.tabs{display:flex;gap:7px;margin-bottom:11px;flex-wrap:wrap}
.tab{padding:7px 13px;border-radius:9px;border:1px solid var(--line2);background:#0e1120;
  cursor:pointer;font-size:12.5px;font-weight:600;color:var(--mut)}
.tab.active{background:var(--acc);border-color:transparent;color:#fff}

.log{max-height:330px;overflow:auto;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:#12162a;color:var(--mut);font-size:11px;
  text-transform:uppercase;letter-spacing:.06em;z-index:1}
tbody tr:hover{background:#131830}
td.m{font-family:var(--mono);font-size:11.5px;color:var(--mut);white-space:nowrap}
code{font-family:var(--mono);background:#0e1120;border:1px solid var(--line);
  border-radius:6px;padding:1px 6px;font-size:12px;color:#a8ffd0}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700}
.b-ok{background:rgba(46,204,113,.16);color:#7ee2a8}
.b-err{background:rgba(231,76,60,.16);color:#ff9b90}
.b-warn{background:rgba(241,196,15,.16);color:#f5d76e}
.b-mut{background:rgba(139,147,173,.15);color:var(--mut)}

.alert{border-radius:12px;padding:13px 15px;font-size:13.5px;border:1px solid;margin-bottom:14px}
.alert.err{background:rgba(231,76,60,.1);border-color:rgba(231,76,60,.4);color:#ffb3aa}
.alert.ok{background:rgba(46,204,113,.09);border-color:rgba(46,204,113,.35);color:#9be8bd}
.alert.info{background:rgba(88,101,242,.1);border-color:rgba(88,101,242,.38);color:#b9c0ff}
.alert b{color:#fff}
.hint{color:var(--mut);font-size:12.5px;margin-top:8px}
.hide{display:none!important}
.spinner{width:15px;height:15px;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;
  border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
footer{color:var(--dim);font-size:12px;text-align:center;margin-top:26px;line-height:1.8}
kbd{font-family:var(--mono);background:#0e1120;border:1px solid var(--line2);border-bottom-width:2px;
  border-radius:5px;padding:1px 6px;font-size:11px}
.lockscreen{display:grid;place-items:center;min-height:70vh}
.lockbox{max-width:520px;width:100%}
@media (max-width:640px){.wrap{padding:18px 12px 50px}h1{font-size:16px}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="brand">
    <div class="logo">🛠️</div>
    <div>
      <h1>AIDiscordServerEinrichten</h1>
      <div class="sub">Discord ⇄ Arena-AI Relay · Console</div>
    </div>
  </div>
  <div class="row" style="align-items:center">
    <span class="pill" id="statusPill"><span class="dot" id="statusDot"></span><span id="statusText">Verbinde …</span></span>
  </div>
</header>

<!-- ────────────── Token-Eingabe (nur ohne gültiges Token sichtbar) ────────── -->
<section class="card" id="tokenCard">
  <h2>🔑 Sitzungs-Token</h2>
  <div class="alert info">
    Dein Token kommt vom Discord-Command <b>/connect</b> — nur für dich sichtbar,
    nur für diesen Server gültig. Es beginnt mit <code>adse_</code>.
  </div>
  <div class="row">
    <input id="tokenInput" class="mono grow" type="password" spellcheck="false"
           placeholder="adse_…" autocomplete="off">
    <button class="btn primary" id="connectBtn">Verbinden</button>
  </div>
  <div class="hint">
    Tipp: Das Token steht auch in der URL, wenn du den Button <b>„Console öffnen“</b>
    in Discord benutzt hast (<code>?t=…</code>). Es wird nur in diesem Browser-Tab gehalten
    und niemals an Dritte gesendet.
  </div>
</section>

<!-- ─────────────────────────── Hauptbereich ─────────────────────────── -->
<div id="main" class="hide">

  <div id="alerts"></div>

  <!-- Prompt -->
  <section class="card">
    <h2>📋 Prompt für Arena AI</h2>
    <div class="tabs">
      <div class="tab active" data-variant="short">Kurz (Discord)</div>
      <div class="tab" data-variant="long">Vollständig</div>
      <div class="tab" data-variant="system">System-Prompt</div>
    </div>
    <pre id="promptBox">Lade Prompt …</pre>
    <div class="row" style="margin-top:13px">
      <button class="btn primary big grow" id="copyBtn">📋 Prompt kopieren</button>
      <a class="btn" id="downloadBtn" download="arena-prompt.md" href="#">⬇️ Als .md speichern</a>
      <button class="btn" id="newTokenBtn">🔄 Neues Token</button>
    </div>
    <div class="hint">
      Kopiere den Prompt und füge ihn bei <b>Arena AI</b> ein. Die KI ruft dann
      <code>/api/v1/capabilities</code> auf und weiß sofort, was sie tun kann.
    </div>
  </section>

  <!-- Status -->
  <section class="card">
    <h2>📡 Verbindung</h2>
    <div class="grid g3" id="stats"></div>
    <div class="hint" id="countdown"></div>
  </section>

  <!-- Server -->
  <section class="card">
    <h2>🏠 Server</h2>
    <div class="grid g3" id="guildStats">
      <div class="stat"><div class="k">Lade …</div><div class="v">–</div></div>
    </div>
    <div class="row" style="margin-top:13px">
      <button class="btn" id="snapshotBtn">🔍 Snapshot laden</button>
      <button class="btn" id="refreshBtn">↻ Aktualisieren</button>
    </div>
    <div id="snapshotOut" class="hide" style="margin-top:13px"><pre id="snapshotPre"></pre></div>
  </section>

  <!-- Action-Log -->
  <section class="card">
    <h2>🧾 API-Action-Log <span class="badge b-mut" id="logCount">0</span></h2>
    <div class="log">
      <table>
        <thead><tr><th>Zeit</th><th>Methode</th><th>Pfad</th><th>Status</th><th>Dauer</th></tr></thead>
        <tbody id="logBody"><tr><td colspan="5" class="m">Noch keine Aktionen.</td></tr></tbody>
      </table>
    </div>
    <div class="hint">Zeigt jede Aktion, die die KI (oder du) über dieses Token ausgeführt hat.</div>
  </section>

  <!-- API-Tester -->
  <section class="card">
    <h2>🧪 API-Tester</h2>
    <div class="row">
      <select id="tMethod" style="flex:0 0 112px">
        <option>GET</option><option>POST</option><option>PATCH</option>
        <option>PUT</option><option>DELETE</option>
      </select>
      <input id="tPath" class="mono grow" value="/api/v1/guild/snapshot" spellcheck="false">
      <button class="btn primary" id="tSend">Senden</button>
    </div>
    <div style="margin-top:10px">
      <textarea id="tBody" class="mono" placeholder='Body (JSON) — nur für POST/PATCH/PUT, z. B. {"name": "neuer-kanal", "type": "text"}'></textarea>
    </div>
    <div style="margin-top:11px"><pre id="tOut">Antwort erscheint hier.</pre></div>
  </section>

  <!-- Sicherheit -->
  <section class="card">
    <h2>🔐 Sicherheit</h2>
    <div class="grid g2">
      <div class="alert info" style="margin:0">
        <b>Kein Bot-Token im Spiel.</b><br>
        Die KI bekommt ein sitzungsgebundenes Token — nicht den Discord-Bot-Token.
        Es gilt nur für diesen Server, hat ein Ablaufdatum und ist widerrufbar.
      </div>
      <div class="alert info" style="margin:0">
        <b>Nur mit Administrator-Rechten.</b><br>
        <code>/connect</code> funktioniert ausschließlich für Nutzer mit
        Administrator-Berechtigung, und auch der Bot muss Administrator sein.
        Die Antwort ist nur für dich sichtbar (ephemeral).
      </div>
    </div>
    <div class="row" style="margin-top:13px">
      <button class="btn danger grow" id="revokeBtn">⛔ Zugriff jetzt widerrufen</button>
    </div>
    <div class="hint">
      Widerrufen macht das Token sofort ungültig. Die KI verliert damit jeden Zugriff —
      laufende Aktionen schlagen ab dem nächsten Aufruf fehl.
    </div>
  </section>

</div>

<footer>
  AIDiscordServerEinrichten · Relay für Arena AI ·
  <a href="/api/health" style="color:var(--acc2)">Health</a> ·
  <a href="/api/v1/capabilities" style="color:var(--acc2)">API-Doku</a><br>
  Token verlassen niemals deinen Browser außer als <kbd>Authorization</kbd>-Header an diesen Server.
</footer>
</div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
let TOKEN = "";
let BASE = location.origin.replace(/\/$/, "");
let VARIANT = "short";
let pollTimer = null;
let countdownTimer = null;
let sessionExpiresAt = null;
const MAX_SESSIONS_HINT = "5";

// ── Token aus URL (?t=…) ─────────────────────────────────────────────────────
(function readUrlToken(){
  try{
    const p = new URLSearchParams(location.search);
    const t = p.get("t") || p.get("token") || "";
    if(t){ TOKEN = t.trim(); }
    const h = (location.hash||"").replace(/^#/,"");
    if(!TOKEN && h.startsWith("adse_")) TOKEN = h;
  }catch(e){}
})();

function authHeaders(extra){
  const h = {"Authorization": "Bearer " + TOKEN};
  return Object.assign(h, extra || {});
}

async function api(path, opts){
  opts = opts || {};
  const init = {method: opts.method || "GET", headers: authHeaders()};
  if(opts.body !== undefined && opts.body !== null){
    init.headers["Content-Type"] = "application/json";
    init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
  }
  const res = await fetch(BASE + path, init);
  let data = null;
  const text = await res.text();
  try{ data = text ? JSON.parse(text) : null; }catch(e){ data = {raw:text}; }
  if(!res.ok || (data && data.ok === false)){
    const err = (data && data.error) || {message: "HTTP " + res.status, code: res.status};
    throw Object.assign(new Error(err.message || ("HTTP " + res.status)), {detail: err, status: res.status});
  }
  return data ? data.data : null;
}

function alertBox(kind, html){
  return '<div class="alert ' + kind + '">' + html + '</div>';
}
function showAlert(kind, html){
  $("alerts").innerHTML = alertBox(kind, html);
}
function clearAlert(){ $("alerts").innerHTML = ""; }

function setStatus(on, text){
  $("statusDot").className = "dot " + (on ? "on" : "off");
  $("statusText").textContent = text;
}

function showMain(on){
  $("main").classList.toggle("hide", !on);
  $("tokenCard").classList.toggle("hide", on);
}

// ── Prompt ───────────────────────────────────────────────────────────────────
async function loadPrompt(){
  $("promptBox").textContent = "Lade Prompt …";
  try{
    const res = await fetch(BASE + "/api/v1/prompt?variant=" + VARIANT + "&format=text",
                            {headers: authHeaders()});
    let text = await res.text();
    if(!res.ok){ throw new Error(text || ("HTTP " + res.status)); }
    // Der Server setzt das Token normalerweise selbst ein. Falls nicht (z. B.
    // Auth per Cookie), ersetzt die Console den Platzhalter — sonst würde ein
    // unbrauchbarer Prompt in der Zwischenablage landen.
    if(TOKEN){ text = text.split("<DEIN-TOKEN>").join(TOKEN); }
    $("promptBox").textContent = text;
    const blob = new Blob([text], {type:"text/markdown;charset=utf-8"});
    $("downloadBtn").href = URL.createObjectURL(blob);
  }catch(e){
    $("promptBox").textContent = "Prompt konnte nicht geladen werden: " + (e.message || e);
  }
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    VARIANT = tab.dataset.variant;
    loadPrompt();
  });
});

async function copyPrompt(){
  const text = $("promptBox").textContent || "";
  const btn = $("copyBtn");
  const original = btn.innerHTML;
  try{
    if(navigator.clipboard && window.isSecureContext){
      await navigator.clipboard.writeText(text);
    }else{
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); document.body.removeChild(ta);
    }
    btn.innerHTML = "✅ Kopiert!";
    btn.classList.remove("primary");
  }catch(e){
    btn.innerHTML = "❌ Kopieren fehlgeschlagen";
  }
  setTimeout(() => { btn.innerHTML = original; btn.classList.add("primary"); }, 1800);
}

// ── Status / Metriken ────────────────────────────────────────────────────────
function stat(k, v, small){
  return '<div class="stat"><div class="k">' + k + '</div><div class="v' +
         (small ? ' sm' : '') + '">' + v + '</div></div>';
}
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

async function loadMe(){
  const me = await api("/api/v1/me");
  const s = me.session || {};
  const g = me.guild || {};
  const b = me.bot || {};
  const p = me.permissions || {};

  $("stats").innerHTML =
      stat("Bot", esc(b.username || "?"), true)
    + stat("Bot-Latenz", (b.latency_ms != null ? b.latency_ms + " ms" : "–"))
    + stat("Sitzung", esc(s.mode_label || s.mode || "?"), true)
    + stat("Scope", '<span class="badge b-warn">' + esc(s.scope || "?") + "</span>", true)
    + stat("Anfragen", s.request_count != null ? s.request_count : "–")
    + stat("Uptime", fmtDuration(me.uptime_seconds));

  if(p.missing_for_full_control && p.missing_for_full_control.length){
    showAlert("err", "<b>Dem Bot fehlen Rechte:</b> " +
      p.missing_for_full_control.map(x => "<code>" + esc(x) + "</code>").join(", ") +
      "<br>Bitte die Bot-Rolle auf <b>Administrator</b> setzen oder ganz nach oben schieben.");
  }else{
    clearAlert();
  }

  sessionExpiresAt = s.expires_at ? new Date(s.expires_at) : null;
  updateCountdown(s);
  return me;
}

async function loadGuild(){
  try{
    const g = await api("/api/v1/guild?detailed=true");
    const c = g.counts || {};
    $("guildStats").innerHTML =
        stat("Server", esc(g.name || "?"), true)
      + stat("Mitglieder", g.member_count != null ? g.member_count : "–")
      + stat("Kanäle", c.channels != null ? c.channels : "–")
      + stat("Kategorien", c.categories != null ? c.categories : "–")
      + stat("Rollen", c.roles != null ? c.roles : "–")
      + stat("Emojis", c.emojis != null ? c.emojis : "–")
      + stat("Bots", c.bots != null ? c.bots : "–")
      + stat("Online", c.members_online != null ? c.members_online : "–")
      + stat("Verifikation", esc(g.verification_level_name || "–"), true);
  }catch(e){
    $("guildStats").innerHTML = stat("Fehler", esc(e.message), true);
  }
}

async function loadActions(){
  try{
    const a = await api("/api/v1/actions?limit=60");
    const items = a.actions || [];
    $("logCount").textContent = items.length;
    if(!items.length){
      $("logBody").innerHTML = '<tr><td colspan="5" class="m">Noch keine Aktionen.</td></tr>';
      return;
    }
    $("logBody").innerHTML = items.map(x => {
      const badge = x.ok ? '<span class="badge b-ok">' + x.status + "</span>"
                         : '<span class="badge b-err">' + x.status + "</span>";
      return "<tr><td class='m'>" + fmtTime(x.ts) + "</td><td class='m'>" + esc(x.method) +
             "</td><td class='m'>" + esc(x.path) + "</td><td>" + badge +
             "</td><td class='m'>" + Math.round(x.duration_ms) + " ms</td></tr>";
    }).join("");
  }catch(e){ /* still — Log ist optional */ }
}

function fmtTime(isoStr){
  if(!isoStr) return "–";
  const d = new Date(isoStr);
  return d.toLocaleTimeString("de-DE", {hour12:false});
}
function fmtDuration(sec){
  if(sec == null) return "–";
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600),
        m = Math.floor(sec%3600/60), s = sec%60;
  if(d) return d + "d " + h + "h";
  if(h) return h + "h " + m + "m";
  if(m) return m + "m " + s + "s";
  return s + "s";
}
function updateCountdown(session){
  if(!sessionExpiresAt){ $("countdown").textContent = "⏳ Gültigkeit: unbegrenzt"; return; }
  const tick = () => {
    const left = (sessionExpiresAt - Date.now())/1000;
    if(left <= 0){
      $("countdown").innerHTML = '<span class="badge b-err">Token abgelaufen</span> — ' +
        'bitte <code>/connect</code> erneut ausführen.';
      setStatus(false, "Abgelaufen");
      return;
    }
    $("countdown").textContent = "⏳ Läuft ab in " + fmtDuration(left) +
      "  ·  erstellt von " + (session.created_by_name || "?");
  };
  tick();
  if(countdownTimer) clearInterval(countdownTimer);
  countdownTimer = setInterval(tick, 1000);
}

// ── Aktionen ─────────────────────────────────────────────────────────────────
async function connect(){
  TOKEN = ($("tokenInput").value || TOKEN).trim();
  if(!TOKEN){ showAlert("err", "Bitte zuerst ein Token einfügen."); return; }
  const btn = $("connectBtn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Prüfe …';
  try{
    await api("/api/v1/session");
    if(location.search.indexOf("t=") === -1){
      history.replaceState(null, "", location.pathname + "?t=" + encodeURIComponent(TOKEN));
    }
    $("tokenInput").value = TOKEN;
    showMain(true);
    setStatus(true, "Verbunden");
    await refreshAll();
    startPolling();
  }catch(e){
    showMain(false);
    setStatus(false, "Abgelehnt");
    const err = e.detail || {};
    showAlert("err", "<b>Token abgelehnt:</b> " + esc(err.message || e.message) +
      (err.hint ? "<br>" + esc(err.hint) : ""));
    $("alerts").className = "";
    $("tokenCard").after($("alerts"));
  }finally{
    btn.disabled = false; btn.textContent = "Verbinden";
  }
}

async function refreshAll(){
  try{
    await loadMe();
    setStatus(true, "Verbunden");
  }catch(e){
    const err = e.detail || {};
    if(e.status === 401){
      setStatus(false, "Token ungültig");
      showMain(false);
      stopPolling();
      showAlert("err", "<b>Sitzung beendet:</b> " + esc(err.message || e.message) +
        (err.hint ? "<br>" + esc(err.hint) : ""));
      $("tokenCard").classList.remove("hide");
      $("tokenCard").after($("alerts"));
      return;
    }
    setStatus(false, "Fehler");
    showAlert("err", esc(err.message || e.message));
  }
  await Promise.all([loadGuild(), loadActions(), loadPrompt()]);
}

function startPolling(){
  stopPolling();
  pollTimer = setInterval(() => { loadActions(); }, 6000);
}
function stopPolling(){ if(pollTimer){ clearInterval(pollTimer); pollTimer = null; } }

async function revoke(){
  if(!confirm("Zugriff wirklich widerrufen?\n\nDas Token wird sofort ungültig. " +
              "Die KI verliert damit jeden Zugriff auf den Server.")) return;
  try{
    await api("/api/v1/session", {method: "DELETE", body: {reason: "Über die Console widerrufen"}});
    stopPolling();
    setStatus(false, "Widerrufen");
    showMain(false);
    TOKEN = "";
    history.replaceState(null, "", location.pathname);
    $("alerts").innerHTML = alertBox("ok", "<b>Zugriff widerrufen.</b> Das Token ist ungültig. " +
      "Für neuen Zugriff auf Discord <code>/connect</code> ausführen.");
    $("tokenCard").after($("alerts"));
  }catch(e){
    showAlert("err", esc((e.detail && e.detail.message) || e.message));
  }
}

async function newToken(){
  if(!confirm("Neues Token erzeugen?\n\nDas aktuelle Token wird sofort widerrufen und " +
              "durch ein neues mit gleicher Gültigkeit ersetzt.")) return;
  const btn = $("newTokenBtn");
  btn.disabled = true;
  try{
    const res = await api("/api/v1/session/regenerate", {method: "POST"});
    TOKEN = res.token;
    history.replaceState(null, "", location.pathname + "?t=" + encodeURIComponent(TOKEN));
    $("tokenInput").value = TOKEN;
    stopPolling();
    await refreshAll();
    startPolling();
    showAlert("ok", "<b>Neues Token aktiv.</b> Der Prompt wurde aktualisiert — " +
                    "bitte neu kopieren und an Arena AI senden.");
    $("main").prepend($("alerts"));
  }catch(e){
    const err = e.detail || {};
    showAlert("err", esc(err.message || e.message) + (err.hint ? "<br>" + esc(err.hint) : ""));
  }finally{
    btn.disabled = false;
  }
}
$("connectBtn").addEventListener("click", connect);
$("tokenInput").addEventListener("keydown", e => { if(e.key === "Enter") connect(); });
$("copyBtn").addEventListener("click", copyPrompt);
$("revokeBtn").addEventListener("click", revoke);
$("newTokenBtn").addEventListener("click", newToken);
$("refreshBtn").addEventListener("click", refreshAll);
$("snapshotBtn").addEventListener("click", async () => {
  const box = $("snapshotOut"), pre = $("snapshotPre");
  box.classList.remove("hide");
  pre.textContent = "Lade Snapshot … (kann einige Sekunden dauern)";
  try{
    const snap = await api("/api/v1/guild/snapshot?members=50");
    pre.textContent = JSON.stringify(snap, null, 2);
  }catch(e){
    pre.textContent = "Fehler: " + ((e.detail && e.detail.message) || e.message);
  }
});
$("tSend").addEventListener("click", async () => {
  const out = $("tOut");
  const method = $("tMethod").value;
  let path = $("tPath").value.trim() || "/api/v1/me";
  if(!path.startsWith("/")) path = "/" + path;
  let body;
  if(["POST","PATCH","PUT"].includes(method)){
    const raw = $("tBody").value.trim();
    if(raw){
      try{ body = JSON.parse(raw); }
      catch(e){ out.textContent = "Body ist kein gültiges JSON: " + e.message; return; }
    }
  }
  out.textContent = "Sende " + method + " " + path + " …";
  try{
    const res = await api(path, {method, body});
    out.textContent = JSON.stringify(res, null, 2);
  }catch(e){
    out.textContent = JSON.stringify(e.detail || {error: String(e.message || e)}, null, 2);
  }
});

// ── Start ────────────────────────────────────────────────────────────────────
if(TOKEN){
  $("tokenInput").value = TOKEN;
  connect();
}else{
  setStatus(false, "Kein Token");
  $("tokenInput").focus();
}
</script>
</body>
</html>
"""


def register_console_routes(app: web.Application, state: Any) -> None:
    """Registriert Console, Favicon und robots.txt."""
    config: Config = state.config

    async def console(request: web.Request) -> web.Response:
        if not config.console_enabled:
            # Kein ApiError hier: dieser Handler läuft außerhalb des JSON-Wrappers.
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "CONSOLE_DISABLED",
                        "message": "Die Console ist auf dieser Instanz deaktiviert "
                                   "(CONSOLE_ENABLED=false).",
                        "hint": "Die REST-API funktioniert weiterhin normal. "
                                "GET /api/v1/capabilities zeigt alle Endpoints.",
                        "status": 404,
                    },
                },
                status=404,
            )
        return web.Response(
            text=CONSOLE_HTML, content_type="text/html", charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )

    async def favicon(request: web.Request) -> web.Response:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#5865f2"/><stop offset="1" stop-color="#eb459e"/>'
            '</linearGradient></defs>'
            '<rect width="100" height="100" rx="24" fill="url(#g)"/>'
            '<text x="50" y="70" font-size="56" text-anchor="middle" '
            'font-family="system-ui,sans-serif" fill="#fff">🛠</text></svg>'
        )
        return web.Response(body=svg.encode("utf-8"), content_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    async def robots(request: web.Request) -> web.Response:
        return web.Response(
            text="User-agent: *\nDisallow: /\n", content_type="text/plain", charset="utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    app.router.add_get("/console", console)
    app.router.add_get("/console/", console)
    app.router.add_get("/dashboard", console)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/favicon.svg", favicon)
    app.router.add_get("/robots.txt", robots)

    log.info("Console registriert unter /console")
