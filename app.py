"""Spectra Media – BettyBot CRM Sync
================================================
Flask micro‑service :
• reçoit un POST (/submit) venant d’un chatbot ou d’un formulaire
• rafraîchit automatiquement le token OAuth Zoho (thread daemon)
• crée le Lead dans Zoho CRM + pièce jointe PDF facultative
• retourne une réponse JSON qui indique success / error

Toutes les valeurs sensibles (tokens, SMTP, etc.) **doivent** être passées en variables d’environnement (Render).
"""
from __future__ import annotations

import os
import time
import threading
import logging
from pathlib import Path
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Configuration depuis ENV (avec fallback minimal local)
# ---------------------------------------------------------------------------
SMTP_SERVER   = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", 587))
SMTP_USER     = os.getenv("SMTP_USER", "dummy@example.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "changeme")
EMAIL_DEST    = os.getenv("EMAIL_DEST", "vincent@example.com")

ZOHO_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")

# endpoints
ZOHO_BASE_API   = os.getenv("ZOHO_BASE_API", "https://www.zohoapis.eu")
LEADS_ENDPOINT  = f"{ZOHO_BASE_API}/crm/v2/Leads"
ATTACH_ENDPOINT = f"{ZOHO_BASE_API}/crm/v2/Leads/{{record_id}}/Attachments"
TOKEN_ENDPOINT  = "https://accounts.zoho.eu/oauth/v2/token"

# dossier temporaire pour les uploads
UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s – %(message)s")
logger = logging.getLogger("bettybot-crm-sync")

# ---------------------------------------------------------------------------
# Gestion du token OAuth Zoho (auto‑refresh)
# ---------------------------------------------------------------------------
class ZohoTokenKeeper:
    """Gardien de token : assure qu’on dispose en permanence d’un `access_token` valide."""

    _lock = threading.Lock()

    def __init__(self) -> None:
        # au 1er démarrage, on force un refresh immédiat
        self._access_token: str | None = None
        self._expires_at: float = 0.0  # timestamp UTC
        threading.Thread(target=self._daemon_loop, daemon=True).start()

    # ------------------------------------------------------------------
    def _daemon_loop(self) -> None:
        while True:
            try:
                # refresh anticipé : 60 s avant expiration
                if time.time() >= self._expires_at - 60:
                    self._refresh()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Erreur boucle refresh token: %s", exc, exc_info=True)
            time.sleep(30)

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        if not (ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN):
            raise RuntimeError("Variables ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN manquantes")

        payload = {
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }
        logger.info("🔄  Rafraîchissement du token Zoho…")
        response = requests.post(TOKEN_ENDPOINT, data=payload, timeout=20)
        if response.status_code != 200:
            # log complet pour diagnostic
            logger.error("❌  Zoho token %s → %s", response.status_code, response.text)
            response.raise_for_status()

        data = response.json()
        access_token = data.get("access_token")
        expires_in   = int(data.get("expires_in", 3600))
        if not access_token:
            raise RuntimeError(f"Réponse inattendue (pas d'access_token) : {data}")

        with self._lock:
            self._access_token = access_token
            self._expires_at   = time.time() + expires_in
        logger.info("✅  Nouveau token Zoho OK – expire dans %d s", expires_in)

    # ------------------------------------------------------------------
    def get(self) -> str:
        """Retourne un token valide (rafraîchit synchronement si nécessaire)."""
        if time.time() >= self._expires_at - 60:
            with self._lock:
                if time.time() >= self._expires_at - 60:
                    self._refresh()
        assert self._access_token, "Token Zoho non initialisé"
        return self._access_token

# instance globale
token_keeper = ZohoTokenKeeper()

# ---------------------------------------------------------------------------
# Helpers Zoho
# ---------------------------------------------------------------------------

def zoho_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {token_keeper.get()}",
        "Content-Type": "application/json",
    }


def zoho_create_lead(payload: Dict[str, Any]) -> str:
    """Crée un lead et renvoie son ID."""
    logger.info("➡️  POST %s", LEADS_ENDPOINT)
    r = requests.post(LEADS_ENDPOINT, json={"data": [payload]}, headers=zoho_headers(), timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erreur Zoho Lead {r.status_code}: {r.text}")

    data = r.json().get("data", [{}])[0]
    if data.get("status") != "success":
        raise RuntimeError(f"Réponse Zoho inattendue: {data}")
    record_id: str = data["details"]["id"]
    logger.info("✅  Lead créé #%s", record_id)
    return record_id


def zoho_attach_pdf(record_id: str, pdf_path: Path) -> None:
    if not pdf_path.exists():
        logger.warning("Pièce jointe %s introuvable", pdf_path)
        return
    url = ATTACH_ENDPOINT.format(record_id=record_id)
    with pdf_path.open("rb") as fb:
        files = {"file": (pdf_path.name, fb, "application/pdf")}
        r = requests.post(url, files=files, headers={"Authorization": f"Zoho-oauthtoken {token_keeper.get()}"}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erreur pièce jointe Zoho {r.status_code}: {r.text}")
    logger.info("📎  Pièce jointe uploadée")

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/submit", methods=["POST"])
def submit() -> Any:
    try:
        data = request.form.to_dict()
        logger.info("Form reçu → %s", data)

        lead_payload = {
            "Company":     data.get("Company", "Spectra Media"),
            "Last_Name":   data.get("Last_Name") or data.get("LastName") or "Unknown",
            "First_Name":  data.get("First_Name") or data.get("FirstName"),
            "Email":       data.get("Email"),
            "Phone":       data.get("Phone"),
            "Description": data.get("Description"),
        }

        lead_id = zoho_create_lead(lead_payload)

        # optionnel : gestion PDF en multipart
        if "file" in request.files:
            f = request.files["file"]
            filename = secure_filename(f.filename)
            filepath = UPLOAD_DIR / filename
            f.save(filepath)
            zoho_attach_pdf(lead_id, filepath)
            filepath.unlink(missing_ok=True)

        return jsonify({"status": "success", "lead_id": lead_id})

    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Erreur /submit : %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500

@app.route("/healthz")
def healthz():
    return "OK", 200

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
