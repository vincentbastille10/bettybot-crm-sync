import requests
import os

CLIENT_ID = os.getenv("1000.SL6WYQ25ZIQ3HTIDT5SUNZEM8FFB7B")
CLIENT_SECRET = os.getenv("eab21f3a131962167ce447fdfaf4c0dbb9ddbb4d40
")
REFRESH_TOKEN = os.getenv("1000.fb3042c40d053a92a5111ecb47c15a3c.1dbbcdd52c6127d21a8076240ca61fd9")
REDIRECT_URI = os.getenv("ZOHO_REDIRECT_URI", "https://www.google.com")

def refresh_access_token():
    url = "https://accounts.zoho.eu/oauth/v2/token"
    params = {
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "redirect_uri": REDIRECT_URI
    }

    response = requests.post(url, params=params)
    print(response.json())

if __name__ == "__main__":
    refresh_access_token()
 
