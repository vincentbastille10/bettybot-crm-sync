from __future__ import annotations

"""Spectra Media – BettyBot CRM Sync
================================================
Flask micro‑service (monolith) qui :
• reçoit un POST `/submit` depuis ton chatbot / formulaire  
• rafraîchit en continu le token OAuth Zoho  
• crée le lead dans Zoho CRM + attache un PDF facultatif  
• envoie un mail de notification (facultatif)  
• expose `/` et `/healthz` pour Render et tests rapides

⚙ **Toutes les valeurs sensibles doivent être injectées par variables d’environnement Render**
"""

import logging
import os
import smtplib
import threading
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict

import requests
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename

# =============================================================================
# CONFIGURATION (ENV VARS) – Fallbacks minimes pour dev local
# =============================================================================
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_DEST = os.getenv("EMAIL_DEST", "")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")

ZOHO_BASE_API = os.getenv("ZOHO_BASE_API", "https://www.zohoapis.eu")
TOKEN_ENDPOINT = "https://accounts.zoho.eu/oauth/v2/token"
LEADS_ENDPOINT = f"{ZOHO_BASE_API}/crm/v2/Leads"
ATTACH_ENDPOINT = f"{ZOHO_BASE_API}/crm/v2/Leads/{{record_id}}/Attachments"

UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s – %(message)s", force=True
)
logger = logging.getLogger("bettybot-crm-sync")

class ZohoTokenKeeper:
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                if time.time() >= self._expires_at - 60:
                    self._refresh()
            except Exception as err:
                logger.error("Loop refresh error %s", err, exc_info=True)
            time.sleep(30)

    def _refresh(self) -> None:
        if not (ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN):
            raise RuntimeError("ZOHO_CLIENT_ID / SECRET / REFRESH_TOKEN sont requis")
        payload = {
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }
        logger.info("🔄  Refresh Zoho token…")
        resp = requests.post(TOKEN_ENDPOINT, data=payload, timeout=15)
        if resp.status_code != 200:
            logger.error("❌  Token %s → %s", resp.status_code, resp.text)
            resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"Pas d'access_token : {data}")
        expires_in = int(data.get("expires_in", 3600))
        with self._lock:
            self._access_token = token
            self._expires_at = time.time() + expires_in
        logger.info("✅  Nouveau token OK (exp dans %ds)", expires_in)

    def get(self) -> str:
        if time.time() >= self._expires_at - 60:
            with self._lock:
                if time.time() >= self._expires_at - 60:
                    self._refresh()
        return self._access_token  # type: ignore[return-value]

token_keeper = ZohoTokenKeeper()

def zoho_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {token_keeper.get()}",
        "Content-Type": "application/json",
    }

def zoho_create_lead(payload: Dict[str, Any]) -> str:
    logger.info("➡️  POST %s", LEADS_ENDPOINT)
    r = requests.post(LEADS_ENDPOINT, json={"data": [payload]}, headers=zoho_headers(), timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Zoho Lead error {r.status_code}: {r.text}")
    pdata = r.json()["data"][0]
    if pdata.get("status") != "success":
        raise RuntimeError(f"Unexpected Zoho response: {pdata}")
    lead_id = pdata["details"]["id"]
    logger.info("✅  Lead créé #%s", lead_id)
    return lead_id

def zoho_attach_pdf(lead_id: str, pdf: Path) -> None:
    if not pdf.exists():
        return
    url = ATTACH_ENDPOINT.format(record_id=lead_id)
    with pdf.open("rb") as fb:
        files = {"file": (pdf.name, fb, "application/pdf")}
        r = requests.post(url, files=files, headers={"Authorization": f"Zoho-oauthtoken {token_keeper.get()}"}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Zoho attach error {r.status_code}: {r.text}")
    logger.info("📎  PDF attaché")

def send_mail(subject: str, body: str, attachment: Path | None = None) -> None:
    if not (SMTP_USER and SMTP_PASSWORD and EMAIL_DEST):
        logger.info("SMTP désactivé – vars manquantes")
        return
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_DEST
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    if attachment and attachment.exists():
        with attachment.open("rb") as fb:
            part = MIMEApplication(fb.read(), Name=attachment.name)
        part["Content-Disposition"] = f"attachment; filename={attachment.name}"
        msg.attach(part)
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    logger.info("📧  Mail envoyé → %s", EMAIL_DEST)

app = Flask(__name__)

@app.route("/")
def index() -> tuple[str, int]:
    return "BettyBot CRM Sync : up 🚀", 200

@app.route("/healthz")
def healthz() -> tuple[str, int]:
    return "OK", 200

@app.route("/submit", methods=["POST"])
def submit():
    try:
        form = request.form.to_dict()
        logger.info("Form → %s", form)

        lead_payload = {
            "Company": form.get("Company", "Spectra Media"),
            "Last_Name": form.get("Last_Name") or form.get("LastName") or "Unknown",
            "First_Name": form.get("First_Name") or form.get("FirstName"),
            "Email": form.get("Email"),
            "Phone": form.get("Phone"),
            "Description": form.get("Description"),
        }

        lead_id = zoho_create_lead(lead_payload)

        pdf_path: Path | None = None
        if "file" in request.files:
            f = request.files["file"]
            filename = secure_filename(f.filename)
            pdf_path = UPLOAD_DIR / filename
            f.save(pdf_path)
            zoho_attach_pdf(lead_id, pdf_path)
            pdf_attached = True
        else:
            pdf_attached = False

        send_mail(
            subject="Nouveau lead BettyBot",
            body=f"Un nouveau lead Zoho vient d'être créé (ID {lead_id}). Pièce jointe: {pdf_attached}",
            attachment=pdf_path if pdf_attached else None,
        )

        if pdf_path and pdf_path.exists():
            pdf_path.unlink(missing_ok=True)

        return jsonify({"status": "success", "lead_id": lead_id}), 201

    except Exception as err:
        logger.error("Submit error %s", err, exc_info=True)
        return jsonify({"status": "error", "message": str(err)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
