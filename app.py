from flask import Flask, request, render_template_string
import requests
import socket
from fpdf import FPDF
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import os
import datetime
import uuid

ACCESS_TOKEN = "1000.33b9b40e2f48ad1c5024ad09afa5efe9.02f7e307d8579fe773a472a6dbd9d946"
ZOHO_API_URL = "https://www.zohoapis.eu/crm/v2/Leads"
ZOHO_ATTACHMENT_URL = "https://www.zohoapis.eu/crm/v2/Leads/{record_id}/Attachments"
EMAIL_DEST = "vinylestorefrance@gmail.com"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "vinylestorefrance@gmail.com"
SMTP_PASSWORD = "wsie xfqw ilcl zpqe"  # à remplacer

app = Flask(__name__)
FORM_HTML = open("form.html").read()

def generate_pdf(data, filename):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    uid = str(uuid.uuid4())[:8]
    data['Horodatage'] = timestamp
    data['Identifiant'] = uid
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt="Fiche Lead - Zoho CRM", ln=True, align='C')
    pdf.ln(10)
    for key, value in data.items():
        pdf.multi_cell(0, 10, txt=f"{key} : {value}")
    pdf.output(filename)

def send_email_with_pdf(to_email, subject, body, pdf_path):
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), Name="lead.pdf")
        part['Content-Disposition'] = 'attachment; filename="lead.pdf"'
        msg.attach(part)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

def attach_pdf_to_zoho(record_id, filepath):
    url = ZOHO_ATTACHMENT_URL.format(record_id=record_id)
    headers = {
        "Authorization": f"Zoho-oauthtoken {ACCESS_TOKEN}"
    }
    files = {
        'file': open(filepath, 'rb')
    }
    response = requests.post(url, headers=headers, files=files)
    
    print(f"📎 Tentative d'attachement - Status: {response.status_code}")
    print(response.text)
    
    try:
        data = response.json()
        if response.status_code in [200, 201] and data.get("data", [{}])[0].get("status") == "success":
            return True
    except Exception as e:
        print(f"⚠️ Erreur lors du parsing JSON de la réponse d’attachement : {e}")
    
    return False

@app.route("/")
def form():
    return render_template_string(FORM_HTML)

@app.route("/submit", methods=["POST"])
def submit():
    description_text = f"""Objectif : {request.form["objectif"]}
Système actuel : {request.form["systeme_actuel"]}
Budget : {request.form["budget"]}
Délai : {request.form["delai"]}
Contraintes techniques : {request.form["contraintes"]}"""

    data = {
        "First_Name": request.form["first_name"],
        "Last_Name": request.form["last_name"],
        "Email": request.form["email"],
        "Phone": request.form["phone"],
        "Company": request.form["company"],
        "Description": description_text
    }

    headers = {
        "Authorization": f"Zoho-oauthtoken {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(ZOHO_API_URL, json={"data": [data]}, headers=headers)

    # PDF generation
    filename = f"lead_{data['Last_Name']}.pdf"
    full_data = data.copy()
    full_data.update({
        "Objectif": request.form["objectif"],
        "Système actuel": request.form["systeme_actuel"],
        "Budget": request.form["budget"],
        "Délai": request.form["delai"],
        "Contraintes techniques": request.form["contraintes"]
    })
    generate_pdf(full_data, filename)

    # Email
    send_email_with_pdf(EMAIL_DEST, "📝 Nouveau lead reçu via le formulaire", "Voir pièce jointe PDF.", filename)

    # Upload PDF to Zoho CRM if record created
    if response.status_code == 201:
        record_id = response.json()["data"][0]["details"]["id"]
        success = attach_pdf_to_zoho(record_id, filename)
        os.remove(filename)
        if success:
            return "✅ Lead Zoho créé, PDF envoyé par email et attaché à la fiche CRM."
        else:
            return "⚠️ Lead Zoho créé et email envoyé, mais l'attachement CRM a échoué (voir log terminal)."
    else:
        return f"❌ Erreur lors de la création du lead : {response.text}"

def get_available_port(default=5000, fallback=5050):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", default))
        sock.close()
        return default
    except OSError:
        return fallback

if __name__ == "__main__":
    port = get_available_port()
    print(f"🔁 Lancement sur le port {port}")
    app.run(debug=True, port=port)
