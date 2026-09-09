#!/usr/bin/env python3
"""
Jednorazowa autoryzacja Spotify.
1. Ustaw SPOTIFY_CLIENT_ID i SPOTIFY_CLIENT_SECRET.
2. W Spotify Developer Dashboard dodaj Redirect URI:
   http://127.0.0.1:8888/callback
3. Uruchom: python auth_setup.py
4. Zaloguj się w otwartej karcie.
5. Skopiuj wyświetlony SPOTIFY_REFRESH_TOKEN do GitHub Secrets.
"""
import base64
import os
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-private playlist-modify-public"
STATE = secrets.token_urlsafe(24)
result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if qs.get("state", [None])[0] != STATE:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"State mismatch")
            return
        if "error" in qs:
            result["error"] = qs["error"][0]
        else:
            result["code"] = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            "Autoryzacja zakonczona. Mozesz zamknac te karte.".encode("utf-8")
        )

    def log_message(self, fmt, *args):
        pass


auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
    "client_id": CLIENT_ID,
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
    "scope": SCOPES,
    "state": STATE,
})

server = HTTPServer(("127.0.0.1", 8888), Handler)

print("\nOtwieram autoryzację Spotify...")
print("Jeżeli przeglądarka nie otworzy się automatycznie, wklej ten adres:\n")
print(auth_url, "\n")
webbrowser.open(auth_url)
server.handle_request()

if result.get("error"):
    raise SystemExit("Spotify odmówiło autoryzacji: " + result["error"])
if not result.get("code"):
    raise SystemExit("Nie otrzymano kodu autoryzacyjnego.")

r = requests.post(
    "https://accounts.spotify.com/api/token",
    data={
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
    },
    auth=(CLIENT_ID, CLIENT_SECRET),
    timeout=30,
)
r.raise_for_status()
data = r.json()

print("\n=== GOTOWE ===")
print("Dodaj do GitHub Secrets:")
print("SPOTIFY_CLIENT_ID =", CLIENT_ID)
print("SPOTIFY_CLIENT_SECRET = [Twój Client Secret]")
print("SPOTIFY_REFRESH_TOKEN =", data["refresh_token"])
print("\nRefresh token traktuj jak hasło — nie publikuj go.")
