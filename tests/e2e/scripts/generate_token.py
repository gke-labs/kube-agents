#!/usr/bin/env python3
"""
Helper script to generate OAuth Refresh Token for Google Chat E2E Testing using an Owned Test Account (OTA).

Usage:
  CLIENT_ID="<your_client_id>" CLIENT_SECRET="<your_client_secret>" python3 tests/e2e/scripts/generate_token.py
"""

import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID = os.environ.get("CLIENT_ID") or (sys.argv[1] if len(sys.argv) > 1 else "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET") or (sys.argv[2] if len(sys.argv) > 2 else "")

if not CLIENT_ID or not CLIENT_SECRET:
    print("[ERROR] Please provide CLIENT_ID and CLIENT_SECRET.")
    print("Usage: CLIENT_ID=\"<your_client_id>\" CLIENT_SECRET=\"<your_client_secret>\" python3 tests/e2e/scripts/generate_token.py")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/chat.messages.readonly"]

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES, redirect_uri="urn:ietf:wg:oauth:2.0:oob")
auth_url, _ = flow.authorization_url(prompt="consent")

print("\n======================================================================")
print(" 1. Copy this URL and paste it into your Chrome browser in Incognito:")
print("----------------------------------------------------------------------")
print(auth_url)
print("----------------------------------------------------------------------")
print(" 2. Log in as your OTA account (e.g. your-ota-account@gmail.com) and click 'Allow'.")
print(" 3. Copy the authorization code shown on screen and paste it below.")
print("======================================================================\n")

code = input("Enter authorization code: ").strip()
flow.fetch_token(code=code)
creds = flow.credentials

print("\n================ SUCCESS ================")
print(f"E2E_CHAT_CLIENT_ID: {creds.client_id}")
print(f"E2E_CHAT_CLIENT_SECRET: {creds.client_secret}")
print(f"E2E_CHAT_REFRESH_TOKEN: {creds.refresh_token}")
print("=========================================")
