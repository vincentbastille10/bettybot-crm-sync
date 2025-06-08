"""Spectra Media – Lead capture & Zoho sync
------------------------------------------------
Flask app that :
1. receives a POST (/submit) from your chatbot / form
2. refreshes Zoho OAuth token automatically (thread‑safe)
3. creates the Lead in Zoho CRM
4. optionally attaches a PDF (if provided)
5. sends a confirmation email (SMTP)

All secrets are expected as ENV variables in Render.
"""

import os
import time
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, Any

import requests
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

# --------------------------------------------------
# Configuration (all overridable from Render ENV)
# --------------------------------------------------
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_DEST = os.getenv("EMAIL_DEST")  # ex: "vinylestorefrance@gmail.com"

ZOHO_API_URL = os.getenv("ZOHO_API_URL", "https://www.zohoapis.eu/crm/v2/Leads")
ZOHO_ATTACHMENT_URL = (
    os.getenv("ZOHO_ATTACHMENT_URL", "https://www.zohoapis.eu/crm/v2/Leads/{record_id}/Attachments")
)
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
INITIAL_ACCESS_TOKEN = os.getenv("ZOHO_ACCESS_TOKEN")  # facultatif (démarrage rapide)

# --------------------------------------------------
# Minimal logging setup
# --------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s – %(message)s")
logger = logging.getLogger("bettybot-crm-sync")

# --------------------------------------------------
# OAuth token keeper
# --------------------------------------------------
class ZohoTokenKeeper:
    """Maintient un access_token valide, le rafraîchit quand il expire."""

    _lock = threading.Lock()

    def __init__(self):
        self.access_token: Optional[str] = INITIAL_ACCESS_TOKEN
        # forcer refresh immédiat si token absent
        self.expires_at: float = time.time() + 10 if self.access_token else 0.0
        # thread de fond
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    # ---------------- private -----------------
    def _loop(self):
        while True:
            try:
                if time.time() >= self.expires_at:
                    self._refresh()
            except Exception as e:
                logger.error("Erreur boucle refresh token: %s", e, exc_info=True)
            time.sleep(30)

    def _refresh(self):
        logger.info("🔄 Rafraîchissement du token Zoho…")
        url = "https://accounts.zoho.eu/oauth/v2/token"
        payload = {
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "access_token" not in data:
            raise RuntimeError(f"Réponse inattendue token Zoho: {data}")
        with self._lock:
            self.access_token = data["access_token"]
            # marge de sécurité 60 s avant expiration officielle
            self.expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
        logger.info("✅ Nouveau token Zoho OK (expire dans ~%d s)", int(self.expires_at - time.time()))

    # ---------------- public ------------------
    def get(self) -> str:
        """Retourne un token valide (rafraîchit si nécessaire)"""
        if time.time() >= self.expires_at:
            # rafraîchir en synchrone si thread n'a pas encore tourné
            with self._lock:
                if time.time() >= self.expires_at:
                    self._refresh()
        return self.access_token


token_keeper = ZohoTokenKeeper()

# --------------------------------------------------
# Helpers Zoho CRM
# --------------------------------------------------

def _zoho_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {token_keeper.get()}",
        "Content-Type": "application/json",
    }


def create_lead(payload: Dict[str, Any]) -> str:
    """Crée un lead Zoho. Retourne l'ID Zoho."""
    logger.info("➡️  Création Lead Zoho…")
    r = requests.post(ZOHO_API_URL, json={"data": [payload]}, headers=_zoho_headers(), timeout=15)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erreur Zoho Lead {r.status_code}: {r.text}")
    data = r.json()["data"][0]
    if data.get("status") != "success":
        raise RuntimeError(f"Zoho a renvoyé un status != success: {data}")
    record_id = data["details"]["id"]
    logger.info("✅ Lead créé (#%s)", record_id)
    return record_id


def attach_pdf(record_id: str, pdf_path: Path):
    """Attache un PDF au lead."""
    if not pdf_path.exists():
        logger.warning("⛔ PDF introuvable %s – pièce jointe ignorée", pdf_path)
        return
    url = ZOHO_ATTACHMENT_URL.format(record_id=record_id)
    logger.info("📎 Upload PDF → Zoho Attachments")
    with pdf_path.open("rb") as f:
        files = {"file": (pdf_path.name, f, "application/pdf")}
        r = requests.post(url, files=files, headers={"Authorization": f"Zoho-oauthtoken {token_keeper.get()}"}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erreur upload pièce jointe {r.status_code}: {r.text}")
    logger.info("✅ Pièce jointe OK")

# --------------------------------------------------
# Flask
# --------------------------------------------------
app = Flask(__name__)

UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)

@app.route("/submit", methods=["POST"])
def submit():
    try:
        data = request.form.to_dict()
        logger.info("Form reçu: %s", data)

        # Construire le payload Zoho avec les champs essentiels
        lead_payload = {
            "Company": data.get("Company", "Spectra Media"),
            "Last_Name": data.get("Last_Name") or data.get("LastName") or "Unknown",
            "First_Name": data.get("First_Name") or data.get("FirstName"),
            "Email": data.get("Email"),
            "Phone": data.get("Phone"),
            "Description": data.get("Description"),
        }

        # 1. créer le lead
        record_id = create_lead(lead_payload)

        # 2. gérer le PDF (optionnel)
        if "file" in request.files:
            file_obj = request.files["file"]
            filename = secure_filename(file_obj.filename)
            pdf_path = UPLOAD_DIR / filename
            file_obj.save(pdf_path)
            attach_pdf(record_id, pdf_path)
            pdf_path.unlink(missing_ok=True)

        return jsonify({"status": "success", "record_id": record_id})

    except Exception as e:
        logger.error("Submit error: %s", e, exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/healthz")
def health():
    return "OK", 200

# --------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
