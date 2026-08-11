#!/usr/bin/env python3
"""
Helper script to generate OAuth Refresh Token for Google Chat E2E Testing using an Owned Test Account (OTA).

Usage:
  CLIENT_ID="<your_client_id>" CLIENT_SECRET="<your_client_secret>" python3 tests/e2e/scripts/generate_token.py
"""

import os
import sys
import tempfile
from urllib.parse import parse_qs, urlparse
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID = os.environ.get("CLIENT_ID") or (sys.argv[1] if len(sys.argv) > 1 else "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET") or (sys.argv[2] if len(sys.argv) > 2 else "")

if not CLIENT_ID or not CLIENT_SECRET:
    print("[ERROR] Please provide CLIENT_ID and CLIENT_SECRET.")
    print("Usage: CLIENT_ID=\"<your_client_id>\" CLIENT_SECRET=\"<your_client_secret>\" python3 tests/e2e/scripts/generate_token.py")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/chat.messages.readonly"]
REDIRECT_URI = "http://localhost:8080/"

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES, redirect_uri=REDIRECT_URI)
auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

print("\n======================================================================")
print("🔑 Google OAuth Refresh Token Generator for E2E Tests")
print("======================================================================")
print(" 1. Copy this URL and paste it into your Chrome browser in Incognito:")
print("----------------------------------------------------------------------")
print(auth_url)
print("----------------------------------------------------------------------")
print(" 2. Log in as your OTA account and click 'Allow'.")
print(" 3. Copy the authorization code (e.g. 4/0A...) OR the full URL from your browser address bar,")
print("    and paste it below.")
print("======================================================================\n")

user_input = input("Enter code OR full browser URL: ").strip()

# Accept both raw authorization code or full browser redirect URL (with or without http:// scheme)
if "code=" in user_input:
    url_to_parse = user_input if (user_input.startswith("http://") or user_input.startswith("https://")) else f"http://{user_input}"
    parsed = urlparse(url_to_parse)
    query = parse_qs(parsed.query)
    if "code" not in query or not query["code"][0]:
        print("[ERROR] Could not find 'code' parameter in the pasted URL.")
        sys.exit(1)
    code = query["code"][0]
else:
    code = user_input

try:
    flow.fetch_token(code=code)
except Exception as e:
    print(f"[ERROR] Failed to fetch token: {e}")
    sys.exit(1)

creds = flow.credentials

if not creds.refresh_token:
    print("[ERROR] Google returned no refresh token, only a short-lived access token.")
    print("        This happens when the account has already granted consent. Revoke")
    print("        it at https://myaccount.google.com/permissions and re-run.")
    sys.exit(1)

# Neither credential is printed. The client secret is an *input* to this script,
# so echoing it tells the operator nothing they did not already type. The refresh
# token is the one genuine output, and once the OAuth app is published it does not
# expire — writing it to stdout would leave it in terminal scrollback, screen
# shares, and any log that captures this run. Both go to a 0600 file instead.
fd, cred_path = tempfile.mkstemp(prefix="e2e-chat-credentials-", suffix=".env")
with os.fdopen(fd, "w") as handle:
    handle.write(f"E2E_CHAT_CLIENT_ID={creds.client_id}\n")
    handle.write(f"E2E_CHAT_REFRESH_TOKEN={creds.refresh_token}\n")

print("\n================ SUCCESS ================")
print("Wrote E2E_CHAT_CLIENT_ID and E2E_CHAT_REFRESH_TOKEN (mode 0600) to:")
print(f"  {cred_path}")
print("")
print("E2E_CHAT_CLIENT_SECRET is the Client Secret you supplied above; copy it")
print("from where you already have it rather than from this run.")
print("")
print("Save each value as a GitHub Repository Secret of the same name, then:")
print(f"  rm {cred_path}")
print("=========================================")
