#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FD Alerts - RasPiPush Ultimate (Yuboto OMNI API Edition)
Author: FDTeam 2012 Automation
Version: 2.0
"""

from flask import Flask, request, jsonify, send_from_directory
import os, requests, json, uuid, datetime

app = Flask(__name__)

# === Configuration ===
PORT = int(os.getenv("PORT", 8899))
YUBOTO_API_KEY = os.getenv("YUBOTO_API_KEY", "")
SENDER_NAME = os.getenv("SENDER_NAME", "FDTeam 2012")

LOG_FILE = "/opt/raspipush_ultimate/fdalerts.log"

# === Helper ===
def log_message(msg):
    """Απλή συνάρτηση log"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {msg}\n")
    print(msg)

# === Routes ===
@app.route("/")
def index():
    return f"""
    <html>
    <head>
        <title>FD Alerts Panel</title>
        <meta name='viewport' content='width=device-width, initial-scale=1'>
        <style>
            body {{
                background: #101820;
                color: #f2f2f2;
                font-family: Arial, sans-serif;
                text-align: center;
                padding: 40px;
            }}
            h1 {{ color: #00e6a8; }}
            form {{
                margin: 0 auto;
                max-width: 420px;
                background: #1b2430;
                padding: 25px;
                border-radius: 15px;
                box-shadow: 0 0 12px rgba(0,0,0,0.5);
            }}
            input, textarea {{
                width: 100%;
                padding: 10px;
                margin-top: 8px;
                border: none;
                border-radius: 8px;
                background: #2b3949;
                color: #fff;
            }}
            button {{
                background: #00e6a8;
                color: #000;
                border: none;
                padding: 12px 20px;
                border-radius: 8px;
                margin-top: 15px;
                cursor: pointer;
                font-weight: bold;
            }}
            button:hover {{
                background: #00c18d;
            }}
            .footer {{
                margin-top: 25px;
                font-size: 13px;
                color: #888;
            }}
        </style>
    </head>
    <body>
        <h1>📢 FD Alerts Sender</h1>
        <form action="/send" method="post">
            <input type="text" name="number" placeholder="Αριθμός (π.χ. 3069...)" required><br>
            <textarea name="message" placeholder="Μήνυμα προς αποστολή..." rows="4" required></textarea><br>
            <button type="submit">Αποστολή SMS</button>
        </form>
        <div class="footer">Powered by FDTeam 2012 | Port {PORT}</div>
    </body>
    </html>
    """

@app.route("/send", methods=["POST"])
def send_sms():
    try:
        number = request.form.get("number") or request.json.get("number")
        message = request.form.get("message") or request.json.get("message")

        if not number or not message:
            return jsonify({"error": "Missing number or message"}), 400

        payload = {
            "dlr": False,
            "contacts": [{"phonenumber": str(number)}],
            "sms": {
                "sender": SENDER_NAME,
                "text": message,
                "validity": 180,
                "typesms": "sms",
                "longsms": False,
                "priority": 1
            }
        }

        headers = {
            "Authorization": f"Basic {YUBOTO_API_KEY}",
            "Content-Type": "application/json; charset=utf-8"
        }

        response = requests.post("https://services.yuboto.com/omni/v1/Send",
                                 headers=headers,
                                 data=json.dumps(payload))

        log_message(f"📤 Αποστολή σε {number}: {message}")
        log_message(f"✅ Response: {response.text}")

        return jsonify({
            "status": "ok",
            "provider_response": response.json()
        }), 200

    except Exception as e:
        log_message(f"❌ Σφάλμα: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/get_logs", methods=["GET"])
def get_logs():
    """Επιστρέφει τα τελευταία logs"""
    if not os.path.exists(LOG_FILE):
        return jsonify({"logs": []})
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()[-50:]
    return jsonify({"logs": lines})


@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)


if __name__ == "__main__":
    log_message(f"🚀 FD Alerts ξεκίνησε στη θύρα {PORT}")
    app.run(host="0.0.0.0", port=PORT)
