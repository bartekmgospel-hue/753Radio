#!/usr/bin/env python3
"""
753Radio — Radio 357 -> Spotify

Zmiany w tej wersji:
- parser bierze wyłącznie właściwą historię Radia 357,
  a nie sekcję "Teraz na stacjach radiowych";
- retry dla Spotify 429 oraz 500/502/503/504;
- tworzy lub odnajduje playlistę 753Radio;
- limit 5000 utworów;
- unika duplikatów;
- bardziej odporne dopasowanie wykonawca/tytuł.
"""

import os
import re
import sys
import time
import random
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

RADIO_URL = "https://radio111.pl/stations/radio357-pl-radio-357/playlist/"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()
PLAYLIST_NAME = os.getenv("PLAYLIST_NAME", "753Radio")
MAX_TRACKS = int(os.getenv("MAX_TRACKS", "5000"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; 753RadioSpotify/2.0)"
})


def refresh_access_token():
    r = session.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


ACCESS_TOKEN = refresh_access_token()
session.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})


def spotify(method, path, **kwargs):
    url = API + path
    max_attempts = 6

    for attempt in range(1, max_attempts + 1):
        r = session.request(method, url, timeout=30, **kwargs)

        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", "5"))
            retry = max(1, retry)
            print(f"Spotify 429; ponawiam za {retry}s")
            time.sleep(retry)
            continue

        if r.status_code in (500, 502, 503, 504):
            if attempt == max_attempts:
                print(f"Spotify error: {r.status_code} {r.text[:1000]}", file=sys.stderr)
                r.raise_for_status()

            delay = min(30, (2 ** (attempt - 1)) + random.uniform(0, 1))
            print(
                f"Spotify {r.status_code}; próba {attempt}/{max_attempts}, "
                f"ponawiam za {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        if r.status_code >= 400:
            print(f"Spotify error: {r.status_code} {r.text[:1000]}", file=sys.stderr)
            r.raise_for_status()

        return r

    raise RuntimeError("Spotify API: przekroczono liczbę ponowień")


def normalize(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"\b(feat|ft)\.?\b.*$", "", s)
    s = re.sub(r"\((remaster(ed)?|live|radio edit|edit|version)[^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_track_line(text):
    text = " ".join(text.split())
    m = re.match(r"^(\d{1,2}:\d{2})\s+(.+?)\s+-\s+(.+?)$", text)
    if not m:
        return None
    hhmm, artist, title = m.groups()
    artist = artist.strip()
    title = title.strip()
    if not artist or not title:
        return None
    return hhmm, artist, title


def parse_radio357():
    r = requests.get(
        RADIO_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; 753RadioSpotify/2.0)"},
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    history = []

    # Najpewniejszy wariant: główna lista Radio 357 kończy się przed
    # nagłówkiem "Popularne stacje radiowe".
    stop_heading = None
    for h in soup.find_all(["h2", "h3"]):
        txt = " ".join(h.stripped_strings).lower()
        if "popularne stacje radiowe" in txt:
            stop_heading = h
            break

    if stop_heading:
        # Zbieramy tylko elementy <li> będące przed tym nagłówkiem.
        for li in soup.find_all("li"):
            after_stop = False
            for prev in li.find_all_previous():
                if prev is stop_heading:
                    after_stop = True
                    break
            if after_stop:
                continue

            parsed = parse_track_line(" ".join(li.stripped_strings))
            if parsed:
                history.append(parsed)

    # Fallback tekstowy — też odcinamy sekcję innych stacji.
    if not history:
        text = soup.get_text("\n", strip=True)
        text = re.split(
            r"Popularne stacje radiowe",
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        for line in text.splitlines():
            parsed = parse_track_line(line)
            if parsed:
                history.append(parsed)

    unique = []
    seen = set()
    for row in history:
        if row not in seen:
            seen.add(row)
            unique.append(row)

    if not unique:
        raise RuntimeError("Nie udało się odczytać historii Radio 357.")

    # radio111 pokazuje najnowsze u góry; dodajemy od najstarszych.
    unique.reverse()
    return unique


def get_or_create_playlist():
    global PLAYLIST_ID
    if PLAYLIST_ID:
        return PLAYLIST_ID

    offset = 0
    while True:
        data = spotify("GET", f"/me/playlists?limit=50&offset={offset}").json()
        items = data.get("items", [])
        for p in items:
            if p and p.get("name") == PLAYLIST_NAME:
                PLAYLIST_ID = p["id"]
                print(f"Znaleziono playlistę: {PLAYLIST_NAME} ({PLAYLIST_ID})")
                return PLAYLIST_ID

        if not data.get("next"):
            break
        offset += len(items)

    if DRY_RUN:
        print(f"[DRY RUN] Utworzyłbym playlistę: {PLAYLIST_NAME}")
        return "DRY_RUN"

    data = spotify(
        "POST",
        "/me/playlists",
        json={
            "name": PLAYLIST_NAME,
            "public": False,
            "description": "Automatycznie aktualizowana playlista na podstawie utworów granych w Radio 357.",
        },
    ).json()

    PLAYLIST_ID = data["id"]
    print(f"Utworzono playlistę: {PLAYLIST_NAME} ({PLAYLIST_ID})")
    return PLAYLIST_ID


def playlist_items(playlist_id):
    items = []
    offset = 0
    while True:
        data = spotify(
            "GET",
            f"/playlists/{playlist_id}/items?limit=100&offset={offset}",
        ).json()
        batch = data.get("items", [])
        for row in batch:
            item = row.get("item") or row.get("track") or {}
            uri = item.get("uri")
            if uri:
                items.append(uri)

        if not data.get("next"):
            break
        offset += len(batch)
    return items


def candidate_score(target_artist, target_title, item):
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    title = item.get("name", "")

    ta = normalize(target_artist)
    tt = normalize(target_title)
    ca = normalize(artists)
    ct = normalize(title)

    title_score = SequenceMatcher(None, tt, ct).ratio()
    artist_score = SequenceMatcher(None, ta, ca).ratio()
    score = 0.70 * title_score + 0.30 * artist_score

    if tt and (tt in ct or ct in tt):
        score += 0.08
    if ta and (ta in ca or ca in ta):
        score += 0.05

    return min(score, 1.0)


def search_track(artist, title):
    queries = [
        f'track:"{title}" artist:"{artist}"',
        f"{artist} {title}",
    ]

    best = None
    best_score = 0.0

    for q in queries:
        data = spotify(
            "GET",
            f"/search?q={quote(q)}&type=track&limit=10",
        ).json()

        for item in data.get("tracks", {}).get("items", []):
            score = candidate_score(artist, title, item)
            if score > best_score:
                best_score = score
                best = item

        if best_score >= 0.93:
            break

    if best and best_score >= 0.75:
        return best, best_score
    return None, best_score


def add_items(playlist_id, uris):
    if not uris:
        return

    for i in range(0, len(uris), 100):
        batch = uris[i:i + 100]
        if DRY_RUN:
            print(f"[DRY RUN] Dodałbym {len(batch)} utworów")
            continue

        spotify(
            "POST",
            f"/playlists/{playlist_id}/items",
            json={"uris": batch},
        )


def trim_playlist(playlist_id):
    uris = playlist_items(playlist_id)
    extra = len(uris) - MAX_TRACKS
    if extra <= 0:
        return

    keep = uris[extra:]
    print(f"Limit {MAX_TRACKS}: usuwam {extra} najstarszych pozycji.")

    if DRY_RUN:
        return

    first = keep[:100]
    spotify(
        "PUT",
        f"/playlists/{playlist_id}/items",
        json={"uris": first},
    )

    if len(keep) > 100:
        add_items(playlist_id, keep[100:])


def main():
    history = parse_radio357()
    print(f"Radio 357: odczytano {len(history)} pozycji.")

    playlist_id = get_or_create_playlist()

    if DRY_RUN and playlist_id == "DRY_RUN":
        existing = set()
    else:
        existing = set(playlist_items(playlist_id))

    print(f"Spotify: playlista ma {len(existing)} pozycji.")

    new_uris = []
    seen_this_run = set(existing)
    misses = []

    for hhmm, artist, title in history:
        try:
            track, score = search_track(artist, title)
        except requests.RequestException as e:
            print(f"SKIP {hhmm} | {artist} - {title} | błąd Spotify: {e}")
            continue

        if not track:
            misses.append((hhmm, artist, title, score))
            print(f"MISS {hhmm} | {artist} - {title} (score={score:.2f})")
            continue

        uri = track["uri"]
        spotify_artist = ", ".join(a["name"] for a in track.get("artists", []))
        spotify_title = track.get("name", "")

        if uri in seen_this_run:
            continue

        seen_this_run.add(uri)
        new_uris.append(uri)
        print(
            f"ADD  {hhmm} | {artist} - {title} -> "
            f"{spotify_artist} - {spotify_title} ({score:.2f})"
        )

    add_items(playlist_id, new_uris)
    print(f"Dodano: {len(new_uris)} nowych utworów.")

    if not (DRY_RUN and playlist_id == "DRY_RUN"):
        trim_playlist(playlist_id)

    if misses:
        print(f"Nie znaleziono pewnego dopasowania dla {len(misses)} pozycji.")


if __name__ == "__main__":
    main()
