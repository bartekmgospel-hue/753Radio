#!/usr/bin/env python3
import os
import re
import sys
import time
import random
import unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

TODAY_URL = "https://radio357.pl/twoje-357/playlista/"
DAY_URL = "https://radio357.pl/twoje-357/playlista/dzien/{day}/"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()
PLAYLIST_NAME = os.getenv("PLAYLIST_NAME", "753Radio")
MAX_TRACKS = int(os.getenv("MAX_TRACKS", "5000"))
BACKFILL_DAYS_PER_RUN = int(os.getenv("BACKFILL_DAYS_PER_RUN", "1"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

MARKER_RE = re.compile(r"\[753Radio-backfill:(\d{4}-\d{2}-\d{2})\]")

http = requests.Session()
http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; 753RadioSpotify/3.0)"})


def refresh_access_token():
    r = http.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


ACCESS_TOKEN = refresh_access_token()
http.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})


def spotify(method, path, **kwargs):
    url = API + path
    for attempt in range(1, 8):
        r = http.request(method, url, timeout=35, **kwargs)

        if r.status_code == 429:
            delay = max(1, int(r.headers.get("Retry-After", "5")))
            print(f"Spotify 429; czekam {delay}s")
            time.sleep(delay)
            continue

        if r.status_code in (500, 502, 503, 504):
            if attempt == 7:
                print(f"Spotify {r.status_code}: {r.text[:800]}", file=sys.stderr)
                r.raise_for_status()
            delay = min(35, 2 ** (attempt - 1) + random.random())
            print(f"Spotify {r.status_code}; retry za {delay:.1f}s")
            time.sleep(delay)
            continue

        if r.status_code >= 400:
            print(f"Spotify {r.status_code}: {r.text[:1000]}", file=sys.stderr)
            r.raise_for_status()

        return r

    raise RuntimeError("Spotify API: przekroczono liczbę prób")


def normalize(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"\b(feat|ft)\.?\b.*$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title_for_search(s):
    s = s.strip()
    return re.sub(
        r"\s*\((?:remaster(?:ed)?(?:\s+\d{4})?|radio edit|edit)\)\s*$",
        "",
        s,
        flags=re.I,
    ).strip()


def parse_radio357_page(url):
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; 753RadioSpotify/3.0)"},
        timeout=35,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    lines = [" ".join(x.split()) for x in soup.stripped_strings]
    out = []

    for i, line in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}:\d{2}", line):
            continue
        if i + 2 >= len(lines):
            continue

        artist = lines[i + 1].strip()
        title = lines[i + 2].strip()

        if not artist or not title:
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}", artist):
            continue

        out.append((line, artist, title))

    unique, seen = [], set()
    for row in out:
        key = (row[0], normalize(row[1]), normalize(row[2]))
        if key not in seen:
            seen.add(key)
            unique.append(row)
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
        print(f"[DRY RUN] Utworzyłbym playlistę {PLAYLIST_NAME}")
        return "DRY_RUN"

    data = spotify(
        "POST",
        "/me/playlists",
        json={
            "name": PLAYLIST_NAME,
            "public": False,
            "description": "Automatyczna playlista utworów granych w Radio 357.",
        },
    ).json()
    PLAYLIST_ID = data["id"]
    print(f"Utworzono playlistę: {PLAYLIST_NAME} ({PLAYLIST_ID})")
    return PLAYLIST_ID


def get_playlist_meta(pid):
    return spotify("GET", f"/playlists/{pid}").json()


def playlist_uris(pid):
    uris = []
    offset = 0
    while True:
        data = spotify("GET", f"/playlists/{pid}/items?limit=100&offset={offset}").json()
        batch = data.get("items", [])
        for row in batch:
            item = row.get("item") or row.get("track") or {}
            if item.get("uri"):
                uris.append(item["uri"])
        if not data.get("next"):
            break
        offset += len(batch)
    return uris


def score_candidate(artist, title, item):
    item_artist = ", ".join(a["name"] for a in item.get("artists", []))
    item_title = item.get("name", "")

    a1, t1 = normalize(artist), normalize(title)
    a2, t2 = normalize(item_artist), normalize(item_title)

    ascore = SequenceMatcher(None, a1, a2).ratio()
    tscore = SequenceMatcher(None, t1, t2).ratio()

    score = 0.32 * ascore + 0.68 * tscore
    if a1 and (a1 in a2 or a2 in a1):
        score += 0.06
    if t1 and (t1 in t2 or t2 in t1):
        score += 0.08
    return min(1.0, score)


def search_track(artist, title):
    simple = clean_title_for_search(title)
    queries = [
        f'track:"{title}" artist:"{artist}"',
        f'track:"{simple}" artist:"{artist}"',
        f"{artist} {simple}",
    ]

    best, best_score = None, 0.0
    for q in dict.fromkeys(queries):
        data = spotify("GET", f"/search?q={quote(q)}&type=track&limit=10").json()
        for item in data.get("tracks", {}).get("items", []):
            score = score_candidate(artist, title, item)
            if score > best_score:
                best, best_score = item, score
        if best_score >= 0.95:
            break

    if best is not None and best_score >= 0.76:
        return best, best_score
    return None, best_score


def resolve_rows(rows, existing):
    new, misses = [], []
    for hhmm, artist, title in rows:
        try:
            track, score = search_track(artist, title)
        except requests.RequestException as e:
            print(f"SKIP {hhmm} | {artist} - {title} | {e}")
            continue

        if not track:
            misses.append((hhmm, artist, title, score))
            print(f"MISS {hhmm} | {artist} - {title} ({score:.2f})")
            continue

        uri = track["uri"]
        if uri in existing:
            continue

        existing.add(uri)
        new.append(uri)

        sa = ", ".join(a["name"] for a in track.get("artists", []))
        st = track.get("name", "")
        print(f"ADD  {hhmm} | {artist} - {title} -> {sa} - {st} ({score:.2f})")
    return new, misses


def add_items(pid, uris, position=None):
    if not uris:
        return

    for i in range(0, len(uris), 100):
        batch = uris[i:i + 100]
        body = {"uris": batch}
        if position is not None:
            body["position"] = position

        if DRY_RUN:
            print(f"[DRY RUN] add {len(batch)}")
        else:
            spotify("POST", f"/playlists/{pid}/items", json=body)


def replace_items(pid, uris):
    if DRY_RUN:
        return

    spotify("PUT", f"/playlists/{pid}/items", json={"uris": uris[:100]})
    for i in range(100, len(uris), 100):
        add_items(pid, uris[i:i + 100])


def set_backfill_marker(pid, marker_day):
    if DRY_RUN:
        return

    desc = (
        "Automatyczna playlista utworów granych w Radio 357. "
        f"[753Radio-backfill:{marker_day.isoformat()}]"
    )
    spotify("PUT", f"/playlists/{pid}", json={"description": desc})


def current_backfill_day(meta):
    desc = meta.get("description") or ""
    m = MARKER_RE.search(desc)
    if m:
        return date.fromisoformat(m.group(1)) - timedelta(days=1)
    return date.today() - timedelta(days=1)


def main():
    pid = get_or_create_playlist()

    if DRY_RUN and pid == "DRY_RUN":
        existing_list = []
        meta = {"description": ""}
    else:
        existing_list = playlist_uris(pid)
        meta = get_playlist_meta(pid)

    existing = set(existing_list)
    print(f"Spotify: playlista ma {len(existing_list)} pozycji.")

    # Dzisiejsze utwory
    today_rows = parse_radio357_page(TODAY_URL)
    print(f"Dzisiaj: odczytano {len(today_rows)} pozycji.")
    live_new, live_misses = resolve_rows(reversed(today_rows), existing)

    if live_new:
        add_items(pid, live_new, position=0)
        existing_list = live_new + existing_list
        print(f"Dodano bieżących: {len(live_new)}")

    # Archiwum
    archive_misses = []
    if len(existing_list) < MAX_TRACKS:
        day = current_backfill_day(meta)

        for _ in range(BACKFILL_DAYS_PER_RUN):
            if len(existing_list) >= MAX_TRACKS:
                break

            rows = parse_radio357_page(DAY_URL.format(day=day.isoformat()))
            print(f"Archiwum {day}: odczytano {len(rows)} pozycji.")

            if rows:
                archive_new, misses = resolve_rows(reversed(rows), existing)
                archive_misses.extend(misses)

                remaining = MAX_TRACKS - len(existing_list)
                archive_new = archive_new[:remaining]

                if archive_new:
                    add_items(pid, archive_new)
                    existing_list.extend(archive_new)
                    print(f"Archiwum {day}: dodano {len(archive_new)}")

            set_backfill_marker(pid, day)
            day -= timedelta(days=1)

    # Limit
    if len(existing_list) > MAX_TRACKS:
        existing_list = existing_list[:MAX_TRACKS]
        replace_items(pid, existing_list)
        print(f"Przycięto playlistę do {MAX_TRACKS} utworów.")

    print(f"GOTOWE. 753Radio: {len(existing_list)} utworów.")
    print(f"Niedopasowane w tym uruchomieniu: {len(live_misses) + len(archive_misses)}")


if __name__ == "__main__":
    main()
