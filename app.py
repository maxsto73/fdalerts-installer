#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# FDTeam Alerts - RasPi Push Ultimate (Flask)
# Port: 8899
#
# Features:
# - Send SMS via Yuboto OMNI API (Basic auth)
# - Multi recipients (textarea + CSV upload)
# - Landing page with "Το είδα" tracking
# - Two-tab UI (Send / History), live preview
# - Logs persisted to data/logs.json
# - Simple PWA manifest + sw.js stub
#
# Env vars (systemd drop-in /etc/systemd/system/raspipush_ultimate.service.d/env.conf):
# [Service]
# Environment="YUBOTO_API_KEY=YOUR_BASE64_BASIC_KEY"
# Environment="YUBOTO_SENDER=FDTeam 2012"
# Environment="PUBLIC_BASE_URL=https://app.fdteam2012.gr"
#
# YUBOTO_API_KEY is the *base64* string used in Authorization: Basic <key>
# Example from your setup: MDBCNDZFQTktREI1MS00NUMxLUEzRTktOTY3RTQ0NURGNjA1

import os
import json
import csv
import time
import random
import string
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for, render_template_string, abort
import requests

# ---------- Paths / Config ----------
BASE_DIR = Path("/opt/raspipush_ultimate")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = DATA_DIR / "logs.json"
SEEN_FILE = DATA_DIR / "seen.json"

YUBOTO_API_KEY = os.getenv("YUBOTO_API_KEY", "").strip()
YUBOTO_SENDER = os.getenv("YUBOTO_SENDER", "FDTeam 2012").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8899").strip()

# ---------- Helpers ----------
def _read_json(path: Path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def _gen_id(n=8):
    return "".join(random.choices(string.hexdigits.lower(), k=n))

def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _normalize_msisdn_msisdns(raw):
    """
    Accepts a string with numbers separated by comma/semicolon/newline/space,
    returns a list of E.164-like without '+', tailored for GR:
    - remove everything non-digit
    - if starts with '0', drop it (local)
    - if starts with '30', keep
    - if starts with '69' (mobile), prefix '30'
    """
    if not raw:
        return []
    # unify separators
    for ch in [",", ";", "\t"]:
        raw = raw.replace(ch, "\n")
    parts = [p.strip() for p in raw.splitlines() if p.strip()]
    out = []
    for p in parts:
        digits = "".join([c for c in p if c.isdigit()])
        if not digits:
            continue
        if digits.startswith("0"):
            digits = digits[1:]
        if digits.startswith("30"):
            out.append(digits)
        elif digits.startswith("69"):  # GR mobile local
            out.append("30" + digits)
        elif digits.startswith("0030"):
            out.append(digits[2:])  # 0030xxxx -> 30xxxx
        else:
            # fallback: if already looks like intl (e.g., 357...), keep
            out.append(digits)
    # unique while preserving order
    seen = set()
    uniq = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq

def _parse_csv_numbers(file_storage):
    """
    Reads CSV, collects cells that look like numbers (first column by default),
    returns list normalized by _normalize_msisdn_msisdns.
    """
    numbers = []
    try:
        text = file_storage.read().decode("utf-8", errors="ignore")
        file_storage.seek(0)
        reader = csv.reader(text.splitlines())
        for row in reader:
            if not row:
                continue
            # take all columns, user might put numbers in multiple cols
            for cell in row:
                if cell and any(ch.isdigit() for ch in cell):
                    numbers.append(cell)
    except Exception:
        pass
    joined = "\n".join(numbers)
    return _normalize_msisdn_msisdns(joined)

# ---------- Provider: Yuboto OMNI ----------
def yuboto_send_sms(sender, text, msisdns):
    """
    Sends via Yuboto OMNI API:
    POST https://services.yuboto.com/omni/v1/Send
    Headers: Authorization: Basic <YUBOTO_API_KEY> ; Content-Type: application/json; charset=utf-8
    Body:
    {
      "dlr": false,
      "contacts": [{"phonenumber": "3069...."}, ...],
      "sms": {
        "sender": "FDTeam 2012",
        "text": "....",
        "validity": 180,
        "typesms": "sms",
        "longsms": false,
        "priority": 1
      }
    }
    """
    if not YUBOTO_API_KEY:
        return False, {"error": "Missing YUBOTO_API_KEY env"}

    contacts = [{"phonenumber": n} for n in msisdns]
    payload = {
        "dlr": False,
        "contacts": contacts,
        "sms": {
            "sender": sender,
            "text": text,
            "validity": 180,
            "typesms": "sms",
            "longsms": False,
            "priority": 1
        }
    }
    try:
        resp = requests.post(
            "https://services.yuboto.com/omni/v1/Send",
            headers={
                "Authorization": f"Basic {YUBOTO_API_KEY}",
                "Content-Type": "application/json; charset=utf-8"
            },
            json=payload,
            timeout=30
        )
        ok = 200 <= resp.status_code < 300
        data = None
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return ok, {"status_code": resp.status_code, "response": data}
    except Exception as e:
        return False, {"exception": str(e)}

# ---------- Flask ----------
app = Flask(__name__, static_folder=str(BASE_DIR / "static"))

# ----- HTML (Jinja) -----
INDEX_HTML = r"""
{% macro icon(name, cls="w-5 h-5") -%}
  <span class="{{ cls }}" style="display:inline-flex;align-items:center;justify-content:center">⚽</span>
{%- endmacro %}

<!doctype html>
<html lang="el">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FDTeam Alerts</title>
  <link rel="manifest" href="{{ url_for('manifest_json') }}">
  <link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}">
  <meta name="theme-color" content="#111827">
  <style>
  /* reset + playful theme */
  :root {
    --bg: #0b1020;
    --card: #121a33;
    --muted: #9aa5b1;
    --primary: #4f46e5;
    --primary-2: #7c3aed;
    --accent: #00d4ff;
    --good: #16a34a;
    --bad: #dc2626;
  }
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; background: radial-gradient(1200px 800px at 10% 10%, #111b3a 0%, #0b1020 60%); color:#e5e7eb; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
  a { color: var(--accent); text-decoration: none; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 16px; }
  .brand { display:flex; align-items:center; gap:12px; margin: 12px 0 20px; }
  .brand img { width:48px; height:48px; border-radius: 12px; box-shadow: 0 0 24px rgba(0,212,255,.2); }
  .title { font-size: 1.6rem; font-weight: 800; letter-spacing: .4px; display:flex; align-items:center; gap: 10px; }
  .tabs { display:flex; gap: 8px; background: rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.08); padding: 6px; border-radius: 14px; width:max-content; }
  .tab { padding: 8px 12px; border-radius: 10px; cursor: pointer; color:#cbd5e1; user-select:none; }
  .tab.active { background: linear-gradient(90deg, var(--primary), var(--primary-2)); color:#fff; box-shadow: 0 6px 22px rgba(79,70,229,.35); }

  .grid { display:grid; grid-template-columns: 1.2fr .8fr; gap: 16px; margin-top: 16px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }

  .card { background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02)); border:1px solid rgba(255,255,255,.06);
          border-radius: 18px; padding: 16px; box-shadow: 0 8px 30px rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.06); backdrop-filter: blur(6px); }
  .card h3 { margin: 0 0 10px; font-size: 1.1rem; color:#fff; }
  label { display:block; font-size:.9rem; color:#cbd5e1; margin: 8px 0 6px; }
  input[type="text"], input[type="date"], input[type="time"], textarea, select {
    width:100%; padding: 10px 12px; background:#0f1530; color:#e5e7eb; border:1px solid #29304f; border-radius: 12px; outline:none;
  }
  textarea { min-height: 106px; resize: vertical; }
  .row { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .hint { color: var(--muted); font-size: .82rem; }

  .btn { display:inline-flex; align-items:center; justify-content:center; gap:8px; padding: 10px 14px; border-radius: 12px; cursor:pointer;
         border:1px solid transparent; background: linear-gradient(90deg, var(--primary), var(--primary-2)); color:#fff; font-weight: 600; }
  .btn.secondary { background: #0f1530; border-color:#2a3357; color:#cbd5e1; }
  .btn.good { background: linear-gradient(90deg, #059669, #16a34a); }
  .btn.bad  { background: linear-gradient(90deg, #b91c1c, #dc2626); }
  .toolbar { display:flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }

  .preview { white-space: pre-wrap; background:#0f1530; border:1px dashed #334; padding: 12px; border-radius:12px; color:#dbeafe; min-height: 100px; }

  .chip { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; background:#0f1530; border:1px solid #2a3357; color:#cbd5e1; border-radius: 999px; font-size:.85rem; }
  .chips { display:flex; gap: 8px; flex-wrap: wrap; }

  .logrow { display:grid; grid-template-columns: 120px 1fr 220px; gap: 12px; padding:10px; border-bottom:1px solid rgba(255,255,255,.06); }
  @media (max-width: 700px) { .logrow { grid-template-columns: 1fr; } }

  /* playful balls */
  .bg-balls { position: fixed; inset: 0; pointer-events:none; z-index: -1; }
  .ball { position:absolute; width: 120px; height:120px; border-radius: 50%; filter: blur(24px); opacity:.22; animation: float 16s ease-in-out infinite; }
  .ball.a { background:#7c3aed; top:10%; left:6%; }
  .ball.b { background:#00d4ff; bottom: 12%; right:12%; animation-delay: -4s; }
  .ball.c { background:#4f46e5; top: 40%; right: 24%; animation-delay: -8s; }
  @keyframes float { 0% { transform: translateY(0) } 50% { transform: translateY(-22px) } 100% { transform: translateY(0) } }

  .footer { color:#9aa5b1; font-size:.8rem; text-align:center; margin-top: 18px; }
  </style>
</head>
<body>
<div class="bg-balls">
  <div class="ball a"></div>
  <div class="ball b"></div>
  <div class="ball c"></div>
</div>

<div class="wrap">
  <div class="brand">
    <img src="{{ url_for('static', filename='icons/logo_final.png') }}" alt="FD">
    <div class="title">FDTeam Alerts <span class="chip">v2025.10</span></div>
  </div>

  <div class="tabs">
    <div class="tab active" id="tab-send" onclick="switchTab('send')">Αποστολή</div>
    <div class="tab" id="tab-history" onclick="switchTab('history')">Ιστορικό</div>
  </div>

  <div id="page-send" class="grid" style="margin-top:14px;">
    <div class="card">
      <h3>Στοιχεία Ειδοποίησης</h3>
      <div class="row">
        <div>
          <label>Γήπεδο / Τοποθεσία</label>
          <input id="place" type="text" placeholder="π.χ. Δαβουρλής Arena">
        </div>
        <div>
          <label>Κανάλι</label>
          <select id="channel">
            <option value="sms" selected>SMS (Yuboto OMNI)</option>
          </select>
        </div>
      </div>

      <div class="row">
        <div>
          <label>Ημερομηνία</label>
          <input id="date" type="date">
        </div>
        <div>
          <label>Ώρα</label>
          <input id="time" type="time">
        </div>
      </div>

      <label>Παραλήπτες (ένας ανά γραμμή, κόμμα ή ;)</label>
      <textarea id="numbers" placeholder="+3069..., 69..., 3069..., κλπ"></textarea>
      <div class="toolbar">
        <label class="btn secondary">
          Ανέβασμα CSV
          <input id="csvfile" type="file" accept=".csv" style="display:none" onchange="handleCSV(this)">
        </label>
        <button class="btn secondary" onclick="dedupeNumbers()">Καθαρισμός/Μοναδικοί</button>
        <span class="chip"><span id="counter">0</span> αριθμοί</span>
      </div>

      <label>Προεπισκόπηση</label>
      <div class="preview" id="preview"></div>

      <div class="toolbar" style="margin-top:12px;">
        <button class="btn" onclick="buildPreview()">Δημιουργία Προεπισκόπησης</button>
        <button class="btn good" onclick="sendNow()">Αποστολή</button>
      </div>
      <div class="hint">Συντάσσεται και landing link αυτόματα με καταγραφή "Το είδα".</div>
    </div>

    <div class="card">
      <h3>Ρυθμίσεις</h3>
      <div class="chips">
        <div class="chip">Sender: <strong>&nbsp;{{ sender }}</strong></div>
        <div class="chip">Provider: <strong>&nbsp;Yuboto OMNI</strong></div>
        <div class="chip">Base URL: <strong>&nbsp;{{ base_url }}</strong></div>
      </div>
      <p class="hint" style="margin-top:8px">Το sender και το API key είναι από το systemd env.</p>
      <hr style="border-color: rgba(255,255,255,.06)">
      <p class="hint">PWA: Πρόσθεσε στη συσκευή σου (iOS/Android) για γρήγορη πρόσβαση.</p>
    </div>
  </div>

  <div id="page-history" class="card" style="display:none; margin-top:14px;">
    <h3>Ιστορικό</h3>
    <div id="history"></div>
  </div>

  <div class="footer">© FDTeam 2012 — built for RasPi • Alerts & Landing with ❤️</div>
</div>

<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(()=>{});
}

// tabs
function switchTab(name){
  for (const id of ["send","history"]) {
    document.getElementById("page-"+id).style.display = (id===name)?"block":"none";
    document.getElementById("tab-"+id).classList.toggle("active", id===name);
  }
  if (name==='history') loadHistory();
}

// CSV
function handleCSV(input){
  const f = input.files && input.files[0];
  if(!f) return;
  const form = new FormData();
  form.append('file', f);
  fetch('/api/parse_csv', { method:'POST', body: form })
    .then(r => r.json())
    .then(j => {
      const area = document.getElementById('numbers');
      const existing = area.value ? area.value + "\\n" : "";
      area.value = existing + (j.numbers || []).join("\\n");
      updateCounter();
    })
    .catch(()=>{});
}

function updateCounter(){
  const area = document.getElementById('numbers');
  const raw = area.value || "";
  const list = raw.split(/[\n,;]+/).map(s=>s.trim()).filter(Boolean);
  document.getElementById('counter').textContent = list.length;
}

function dedupeNumbers(){
  const area = document.getElementById('numbers');
  const raw = area.value || "";
  fetch('/api/dedupe', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ raw })})
    .then(r=>r.json()).then(j=>{
      area.value = (j.numbers || []).join("\\n");
      updateCounter();
    });
}

function makeText(place, date, time, landingUrl){
  const lines = [
    "Flying Dads Team ⚽",
    "Υπενθύμιση: Παίζουμε Μπαλίτσα στο " + place + " την " + date + " ώρα " + time + "!",
    "👉 Δες περισσότερα: " + landingUrl
  ];
  return lines.join("\\n");
}

function buildPreview(){
  const place = document.getElementById('place').value.trim();
  const date = document.getElementById('date').value;
  const timeV = document.getElementById('time').value;
  const numbers = document.getElementById('numbers').value.trim();
  const tmpId = Math.random().toString(16).slice(2,10);
  const landing = "{{ base_url }}/r?id=" + tmpId;
  const txt = makeText(place, date, timeV, landing);
  document.getElementById('preview').textContent = txt;
}

async function sendNow(){
  const place = document.getElementById('place').value.trim();
  const date = document.getElementById('date').value;
  const timeV = document.getElementById('time').value;
  const channel = document.getElementById('channel').value;
  const raw_numbers = document.getElementById('numbers').value;

  const res = await fetch('/send', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ place, date, time: timeV, channel, raw_numbers })
  });
  const j = await res.json();
  if(j.ok){
    alert("✅ Έφυγε! Landing: " + j.landing);
    switchTab('history');
  }else{
    alert("❌ Αποτυχία: " + (j.error || 'unknown'));
  }
}

// history
function loadHistory(){
  fetch('/api/get_logs').then(r=>r.json()).then(j=>{
    const box = document.getElementById('history');
    const arr = j.logs || [];
    if(!arr.length){ box.innerHTML = '<div class="hint">Κενό ιστορικό…</div>'; return; }
    let html = '';
    for (const x of arr.slice().reverse()){
      const seen = (x.seen_by||[]).length ? ('✅ ' + (x.seen_by||[]).length + ' είδαν') : '—';
      html += `
        <div class="logrow">
          <div class="hint">${x.timestamp||''}</div>
          <div>
            <div><strong>${(x.text||'').replace(/</g,'&lt;')}</strong></div>
            <div class="hint">Recipients: ${ (x.msisdns||[]).join(', ') }</div>
            <div class="hint">Landing: <a href="${x.landing}" target="_blank">${x.landing}</a></div>
          </div>
          <div>
            <div class="chip">${seen}</div>
            <div class="chip" style="margin-top:6px;">ID: ${x.id}</div>
          </div>
        </div>
      `;
    }
    box.innerHTML = html;
  });
}

document.getElementById('numbers').addEventListener('input', updateCounter);
</script>
</body>
</html>
"""

LANDING_HTML = r"""
<!doctype html>
<html lang="el">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FDTeam Alert</title>
  <link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}">
  <style>
    body{margin:0;background:radial-gradient(1000px 600px at 15% 10%, #1c2244 0%, #0b1020 60%); color:#e5e7eb; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;}
    .wrap{max-width:820px;margin:0 auto;padding:18px;}
    .card{background:linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02)); border:1px solid rgba(255,255,255,.06);
          border-radius:18px; padding:16px; margin-top:16px;}
    .title{display:flex;align-items:center;gap:10px;font-size:1.4rem;font-weight:800}
    .msg{white-space: pre-wrap; background:#0f1530; border:1px dashed #334; padding:12px; border-radius:12px; margin-top:10px;}
    .btn{display:inline-flex; gap:8px; align-items:center; background:linear-gradient(90deg,#059669,#16a34a); color:#fff; padding:10px 14px; border-radius:12px; border:0; cursor:pointer; font-weight:700}
    .hint{color:#9aa5b1; font-size:.85rem}
  </style>
</head>
<body>
<div class="wrap">
  <div class="title"><img src="{{ url_for('static', filename='icons/logo_final.png') }}" style="width:40px;height:40px;border-radius:10px"> FDTeam Ειδοποίηση</div>
  <div class="card">
    <div class="hint">ID: {{ mid }}</div>
    <div class="msg">{{ text }}</div>
    <div style="margin-top:12px">
      <button class="btn" onclick="markSeen()">✅ Το είδα</button>
    </div>
  </div>
  <p class="hint">Ευχαριστούμε! Καλή διασκέδαση ⚽</p>
</div>
<script>
function markSeen(){
  fetch('/seen', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ id: "{{ mid }}" })})
    .then(()=>{ alert("Καταγράφηκε!"); })
    .catch(()=>{ alert("Προέκυψε σφάλμα, δοκίμασε ξανά.") });
}
</script>
</body>
</html>
"""

# ---------- Routes ----------
@app.route("/")
def index():
    html = render_template_string(
        INDEX_HTML,
        sender=YUBOTO_SENDER,
        base_url=PUBLIC_BASE_URL
    )
    return html

@app.route("/history")
def history_page():
    # Keep for direct navigation / compatibility; reuses index UI
    return redirect(url_for("index"))

@app.route("/manifest.json")
def manifest_json():
    data = {
        "name": "FDTeam Alerts",
        "short_name": "FD Alerts",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b1020",
        "theme_color": "#111827",
        "icons": [
            {"src": "/static/icons/icon-180.png", "sizes": "180x180", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return jsonify(data)

@app.route("/sw.js")
def sw_js():
    js = (
        "self.addEventListener('install',e=>self.skipWaiting());"
        "self.addEventListener('activate',e=>self.clients.claim());"
        "self.addEventListener('fetch',()=>{});"
    )
    return js, 200, {"Content-Type":"application/javascript"}

@app.route("/api/parse_csv", methods=["POST"])
def api_parse_csv():
    if "file" not in request.files:
        return jsonify({"numbers":[]})
    nums = _parse_csv_numbers(request.files["file"])
    return jsonify({"numbers": nums})

@app.route("/api/dedupe", methods=["POST"])
def api_dedupe():
    data = request.get_json(silent=True) or {}
    raw = data.get("raw","")
    nums = _normalize_msisdn_msisdns(raw)
    return jsonify({"numbers": nums})

@app.route("/api/get_logs")
def api_get_logs():
    logs = _read_json(LOG_FILE, [])
    return jsonify({"logs": logs})

@app.route("/r")
def landing():
    mid = request.args.get("id","").strip()
    if not mid:
        abort(404)
    logs = _read_json(LOG_FILE, [])
    msg = next((x for x in logs if x.get("id")==mid), None)
    if not msg:
        abort(404)
    html = render_template_string(LANDING_HTML, mid=mid, text=msg.get("text",""))
    return html

@app.route("/seen", methods=["POST"])
def api_seen():
    data = request.get_json(silent=True) or {}
    mid = data.get("id","").strip()
    if not mid:
        return jsonify({"ok": False, "error":"missing id"}), 400
    logs = _read_json(LOG_FILE, [])
    updated = False
    for x in logs:
        if x.get("id")==mid:
            sb = x.get("seen_by", [])
            sb.append({"ts": _now_str(), "ip": request.remote_addr})
            x["seen_by"] = sb
            updated = True
            break
    if updated:
        _write_json(LOG_FILE, logs)
    return jsonify({"ok": True})

@app.route("/send", methods=["POST"])
def api_send():
    data = request.get_json(silent=True) or {}
    place = (data.get("place") or "").strip()
    date_ = (data.get("date") or "").strip()
    time_ = (data.get("time") or "").strip()
    raw_numbers = data.get("raw_numbers") or ""
    channel = (data.get("channel") or "sms").strip()

    msisdns = _normalize_msisdn_msisdns(raw_numbers)
    if not place or not date_ or not time_:
        return jsonify({"ok": False, "error": "Συμπλήρωσε τόπο/ημερομηνία/ώρα."}), 400
    if channel != "sms":
        return jsonify({"ok": False, "error": "Μόνο SMS υποστηρίζεται προς το παρόν."}), 400
    if not msisdns:
        return jsonify({"ok": False, "error": "Δεν βρέθηκαν παραλήπτες."}), 400

    msg_id = _gen_id()
    landing_url = f"{PUBLIC_BASE_URL}/r?id={msg_id}"
    text = f"Flying Dads Team ⚽\nΥπενθύμιση: Παίζουμε στο {place} την {date_} ώρα {time_}!\n👉 Δες περισσότερα: {landing_url}"

    ok, provider = yuboto_send_sms(YUBOTO_SENDER, text, msisdns)

    # log
    logs = _read_json(LOG_FILE, [])
    logs.append({
        "id": msg_id,
        "timestamp": _now_str(),
        "place": place,
        "date": date_,
        "time": time_,
        "channel": channel,
        "msisdns": msisdns,
        "text": text,
        "landing": landing_url,
        "provider_response": provider,
        "seen_by": []
    })
    _write_json(LOG_FILE, logs)

    if ok:
        return jsonify({"ok": True, "id": msg_id, "landing": landing_url})
    else:
        return jsonify({"ok": False, "id": msg_id, "landing": landing_url, "error": "Provider error", "provider": provider}), 500

# ---------- Main ----------
if __name__ == "__main__":
    # Allow local debug run if needed
    app.run(host="0.0.0.0", port=8899)
