#!/usr/bin/env python3
"""
753Radio v4 — Radio 357 -> Spotify

Najważniejsze zmiany:
- oficjalna playlista Radio 357: dziś + archiwum dzienne,
- marker LIVE w opisie playlisty: kolejne uruchomienia analizują tylko nowe pozycje
  (z bezpiecznym 90-minutowym nakładaniem),
- sprawdzenie istniejących utworów PRZED Spotify Search,
- zwykle 1 zapytanie Search na nowy utwór; drugie tylko jako fallback,
- kontrolowany limit czasu skryptu, zanim GitHub Actions zabije job,
- bez wielominutowego czekania na Spotify 429,
- niebuforowane logi — w Actions od razu widać postęp,
- backfill kontynuuje dzień po dniu aż do 5000 unikalnych utworów.
"""

import os
import re
import sys
import time
import random
import unicodedata
from datetime import datetime, date, timedelta
from difflib import SequenceMatcher
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


TODAY_URL = "https://radio357.pl/twoje-357/playlista/"
DAY_URL = "https://radio357.pl/twoje-357/playlista/dzien/{day}/"

SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

WARSAW = ZoneInfo("Europe/Warsaw")

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()
PLAYLIST_NAME = os.getenv("PLAYLIST_NAME", "753Radio").strip() or "753Radio"
MAX_TRACKS = int(os.getenv("MAX_TRACKS", "5000"))

# Ile pełnych dni archiwum próbujemy dołożyć w jednym uruchomieniu.
BACKFILL_DAYS_PER_RUN = int(os.getenv("BACKFILL_DAYS_PER_RUN", "1"))

# Skrypt sam kończy się przed timeoutem GitHub Actions.
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "1500"))  # 25 minut

# Jeśli Spotify każe czekać dłużej, kończymy run bez błędu i próbujemy następnym razem.
MAX_RETRY_AFTER = int(os.getenv("MAX_RETRY_AFTER", "90"))

# Ponownie sprawdzamy niewielkie okno wstecz, żeby nie zgubić utworu
# przy tej samej minucie albo opóźnionej aktualizacji strony.
LIVE_OVERLAP_MINUTES = int(os.getenv("LIVE_OVERLAP_MINUTES", "90"))

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

BACKFILL_RE = re.compile(r"\[753Radio-backfill:(\d{4}-\d{2}-\d{2})\]")
LIVE_RE = re.compile(r"\[753Radio-live:(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})\]")

STARTED = time.monotonic()

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

http = requests.Session()
http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; 753RadioSpotify/4.0)"
})


class GracefulStop(Exception):
    """Kontrolowane zakończenie przed timeoutem GitHub Actions."""


def now_pl():
    return datetime.now(WARSAW)


def runtime_guard():
    elapsed = time.monotonic() - STARTED
    if elapsed >= MAX_RUNTIME_SECONDS:
        raise GracefulStop(
            f"Osiągnięto limit {MAX_RUNTIME_SECONDS}s dla tego uruchomienia."
        )


def refresh_access_token():
    r = http.post(
        SPOTIFY_TOKEN_URL,
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
http.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})


def spotify(method, path, **kwargs):
    """Spotify request z retry, ale bez czekania dłuższego niż rozsądny limit."""
    url = SPOTIFY_API + path

    for attempt in range(1, 6):
        runtime_guard()
        r = http.request(method, url, timeout=30, **kwargs)

        if r.status_code == 429:
            raw_retry = r.headers.get("Retry-After", "5")
            try:
                delay = max(1, int(float(raw_retry)))
            except ValueError:
                delay = 5

            print(f"Spotify 429 — Retry-After={delay}s")

            if delay > MAX_RETRY_AFTER:
                raise GracefulStop(
                    f"Spotify ograniczył zapytania na {delay}s. "
                    "Kończę ten run bez błędu; następny będzie kontynuował."
                )

            time.sleep(delay)
            continue

        if r.status_code in (500, 502, 503, 504):
            if attempt == 5:
                print(
                    f"Spotify {r.status_code}: {r.text[:800]}",
                    file=sys.stderr,
                )
                r.raise_for_status()

            delay = min(20, (2 ** (attempt - 1)) + random.random())
            print(
                f"Spotify {r.status_code} — próba {attempt}/5, "
                f"ponawiam za {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        if r.status_code >= 400:
            print(
                f"Spotify {r.status_code}: {r.text[:1000]}",
                file=sys.stderr,
            )
            r.raise_for_status()

        return r

    raise RuntimeError("Spotify API: przekroczono liczbę prób")


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(feat|ft)\.?\b.*$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title_for_search(text):
    text = (text or "").strip()
    return re.sub(
        r"\s*\((?:remaster(?:ed)?(?:\s+\d{4})?|radio edit|edit)\)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()


def title_key(title):
    return normalize(clean_title_for_search(title))


def source_key(artist, title):
    return normalize(artist), title_key(title)


def parse_radio357_page(url):
    runtime_guard()

    r = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; 753RadioSpotify/4.0)"
        },
        timeout=30,
    )

    if r.status_code == 404:
        return []

    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    lines = [" ".join(x.split()) for x in soup.stripped_strings]
    rows = []

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

        hh, mm = map(int, line.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue

        hhmm = f"{hh:02d}:{mm:02d}"
        rows.append((hhmm, artist, title))

    # Usuwamy identyczne duplikaty z HTML.
    unique = []
    seen = set()

    for hhmm, artist, title in rows:
        key = (hhmm, normalize(artist), title_key(title))
        if key in seen:
            continue
        seen.add(key)
        unique.append((hhmm, artist, title))

    return unique


def get_or_create_playlist():
    global PLAYLIST_ID

    if PLAYLIST_ID:
        return PLAYLIST_ID

    offset = 0

    while True:
        data = spotify(
            "GET",
            f"/me/playlists?limit=50&offset={offset}",
        ).json()

        items = data.get("items", [])

        for playlist in items:
            if playlist and playlist.get("name") == PLAYLIST_NAME:
                PLAYLIST_ID = playlist["id"]
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
            "description": (
                "Automatyczna playlista utworów granych w Radio 357."
            ),
        },
    ).json()

    PLAYLIST_ID = data["id"]
    print(f"Utworzono playlistę: {PLAYLIST_NAME} ({PLAYLIST_ID})")
    return PLAYLIST_ID


def get_playlist_meta(pid):
    return spotify("GET", f"/playlists/{pid}").json()


def playlist_items(pid):
    """Pobiera URI + metadane potrzebne do sprawdzania duplikatów bez Search."""
    output = []
    offset = 0

    while True:
        runtime_guard()

        data = spotify(
            "GET",
            f"/playlists/{pid}/items?limit=100&offset={offset}",
        ).json()

        batch = data.get("items", [])

        for row in batch:
            item = row.get("item") or row.get("track") or {}
            uri = item.get("uri")

            if not uri:
                continue

            artists = [
                a.get("name", "")
                for a in item.get("artists", [])
                if a.get("name")
            ]

            output.append({
                "uri": uri,
                "artists": artists,
                "title": item.get("name", ""),
            })

        if not data.get("next"):
            break

        offset += len(batch)

    return output


def build_existing_keys(items):
    keys = set()

    for item in items:
        title = title_key(item.get("title", ""))
        artists = item.get("artists", [])

        if not title or not artists:
            continue

        # Główny artysta.
        keys.add((normalize(artists[0]), title))

        # Pełna lista artystów.
        keys.add((normalize(", ".join(artists)), title))

        # Każdy artysta osobno — pomaga przy feat./duetach.
        for artist in artists:
            keys.add((normalize(artist), title))

    return keys


def score_candidate(target_artist, target_title, item):
    spotify_artists = item.get("artists", [])
    first_artist = spotify_artists[0]["name"] if spotify_artists else ""
    all_artists = ", ".join(a["name"] for a in spotify_artists)
    spotify_title = item.get("name", "")

    a1 = normalize(target_artist)
    t1 = title_key(target_title)
    a2_first = normalize(first_artist)
    a2_all = normalize(all_artists)
    t2 = title_key(spotify_title)

    artist_score = max(
        SequenceMatcher(None, a1, a2_first).ratio(),
        SequenceMatcher(None, a1, a2_all).ratio(),
    )

    title_score = SequenceMatcher(None, t1, t2).ratio()

    score = (0.32 * artist_score) + (0.68 * title_score)

    if a1 and (
        a1 in a2_first
        or a2_first in a1
        or a1 in a2_all
    ):
        score += 0.06

    if t1 and (t1 in t2 or t2 in t1):
        score += 0.08

    return min(1.0, score)


def search_track(artist, title):
    """
    Najpierw jedno precyzyjne wyszukiwanie.
    Drugie wykonujemy wyłącznie, gdy pierwsze nie daje dobrego dopasowania.
    """
    simple_title = clean_title_for_search(title)

    queries = [
        f'track:"{simple_title}" artist:"{artist}"',
        f"{artist} {simple_title}",
    ]

    best = None
    best_score = 0.0

    for index, query in enumerate(dict.fromkeys(queries)):
        runtime_guard()

        data = spotify(
            "GET",
            f"/search?q={quote(query)}&type=track&limit=10",
        ).json()

        for item in data.get("tracks", {}).get("items", []):
            score = score_candidate(artist, title, item)
            if score > best_score:
                best = item
                best_score = score

        # Przy dobrym wyniku nie robimy drugiego requestu.
        if best is not None and best_score >= 0.82:
            break

        if index >= 1:
            break

    if best is not None and best_score >= 0.76:
        return best, best_score

    return None, best_score


def resolve_rows(rows, existing_uris, existing_keys):
    new_uris = []
    misses = []
    skipped_existing = 0
    searched = 0

    for hhmm, artist, title in rows:
        runtime_guard()

        key = source_key(artist, title)

        # Najważniejsza optymalizacja:
        # jeżeli wykonawca + tytuł są już na playliście,
        # NIE wykonujemy Spotify Search.
        if key in existing_keys:
            skipped_existing += 1
            continue

        searched += 1

        try:
            track, score = search_track(artist, title)
        except requests.RequestException as exc:
            print(f"SKIP {hhmm} | {artist} - {title} | {exc}")
            continue

        if not track:
            misses.append((hhmm, artist, title, score))
            print(f"MISS {hhmm} | {artist} - {title} ({score:.2f})")
            continue

        uri = track["uri"]
        matched_artists = [
            a.get("name", "")
            for a in track.get("artists", [])
            if a.get("name")
        ]
        matched_title = track.get("name", "")

        # Zapamiętujemy również klucz źródłowy.
        existing_keys.add(key)

        if matched_artists:
            matched_title_key = title_key(matched_title)
            existing_keys.add(
                (normalize(matched_artists[0]), matched_title_key)
            )
            existing_keys.add(
                (normalize(", ".join(matched_artists)), matched_title_key)
            )
            for matched_artist in matched_artists:
                existing_keys.add(
                    (normalize(matched_artist), matched_title_key)
                )

        if uri in existing_uris:
            continue

        existing_uris.add(uri)
        new_uris.append(uri)

        artists_text = ", ".join(matched_artists)
        print(
            f"ADD  {hhmm} | {artist} - {title} "
            f"-> {artists_text} - {matched_title} ({score:.2f})"
        )

    print(
        f"Podsumowanie: search={searched}, "
        f"pominięte_jako_istniejące={skipped_existing}, "
        f"nowe={len(new_uris)}, miss={len(misses)}"
    )

    return new_uris, misses


def add_items(pid, uris, position=None):
    if not uris:
        return

    for i in range(0, len(uris), 100):
        runtime_guard()

        batch = uris[i:i + 100]
        body = {"uris": batch}

        if position is not None:
            body["position"] = position

        if DRY_RUN:
            print(f"[DRY RUN] add {len(batch)}")
        else:
            spotify(
                "POST",
                f"/playlists/{pid}/items",
                json=body,
            )


def replace_items(pid, uris):
    if DRY_RUN:
        return

    runtime_guard()
    spotify(
        "PUT",
        f"/playlists/{pid}/items",
        json={"uris": uris[:100]},
    )

    for i in range(100, len(uris), 100):
        add_items(pid, uris[i:i + 100])


def parse_markers(description):
    backfill = None
    live_day = None
    live_time = None

    match = BACKFILL_RE.search(description or "")
    if match:
        backfill = date.fromisoformat(match.group(1))

    match = LIVE_RE.search(description or "")
    if match:
        live_day = date.fromisoformat(match.group(1))
        live_time = match.group(2)

    return backfill, live_day, live_time


def build_description(backfill_day=None, live_day=None, live_time=None):
    parts = [
        "Automatyczna playlista utworów granych w Radio 357."
    ]

    if backfill_day:
        parts.append(f"[753Radio-backfill:{backfill_day.isoformat()}]")

    if live_day and live_time:
        parts.append(
            f"[753Radio-live:{live_day.isoformat()}T{live_time}]"
        )

    return " ".join(parts)


def update_markers(
    pid,
    *,
    backfill_day=None,
    live_day=None,
    live_time=None,
):
    if DRY_RUN:
        return

    description = build_description(
        backfill_day=backfill_day,
        live_day=live_day,
        live_time=live_time,
    )

    spotify(
        "PUT",
        f"/playlists/{pid}",
        json={"description": description},
    )


def next_backfill_day(last_backfill_day):
    if last_backfill_day:
        return last_backfill_day - timedelta(days=1)

    return now_pl().date() - timedelta(days=1)


def minutes_from_hhmm(hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def rows_since_live_marker(rows, marker_day, marker_time):
    """
    Pierwszy run dnia: wszystkie pozycje.
    Kolejne runy: marker minus LIVE_OVERLAP_MINUTES.
    """
    today = now_pl().date()

    if marker_day != today or not marker_time:
        return rows

    threshold = max(
        0,
        minutes_from_hhmm(marker_time) - LIVE_OVERLAP_MINUTES,
    )

    filtered = [
        row
        for row in rows
        if minutes_from_hhmm(row[0]) >= threshold
    ]

    return filtered


def newest_time(rows):
    if not rows:
        return None

    return max(
        (row[0] for row in rows),
        key=minutes_from_hhmm,
    )


def main():
    print("=== 753Radio v4 ===")
    print(f"Czas PL: {now_pl().isoformat(timespec='seconds')}")

    pid = get_or_create_playlist()

    if DRY_RUN and pid == "DRY_RUN":
        items = []
        meta = {"description": ""}
    else:
        items = playlist_items(pid)
        meta = get_playlist_meta(pid)

    existing_list = [item["uri"] for item in items]
    existing_uris = set(existing_list)
    existing_keys = build_existing_keys(items)

    last_backfill_day, live_day, live_time = parse_markers(
        meta.get("description") or ""
    )

    print(f"Spotify: playlista ma {len(existing_list)} pozycji.")
    print(
        f"Markery: backfill={last_backfill_day}, "
        f"live={live_day} {live_time}"
    )

    live_misses = []
    archive_misses = []

    # Markery, które mamy zapisać po wykonaniu danego etapu.
    current_backfill_marker = last_backfill_day
    current_live_day = live_day
    current_live_time = live_time

    try:
        # ---------------------------------------------------------
        # 1. BIEŻĄCY DZIEŃ
        # ---------------------------------------------------------
        today_rows_all = parse_radio357_page(TODAY_URL)

        print(
            f"Dzisiaj: strona zawiera {len(today_rows_all)} pozycji."
        )

        today_rows = rows_since_live_marker(
            today_rows_all,
            live_day,
            live_time,
        )

        print(
            f"Dzisiaj: do sprawdzenia po markerze/overlapie "
            f"{len(today_rows)} pozycji."
        )

        # Radio 357 zwykle pokazuje najnowsze pozycje u góry.
        # Przetwarzamy je w kolejności widocznej na stronie.
        live_new, live_misses = resolve_rows(
            today_rows,
            existing_uris,
            existing_keys,
        )

        if live_new:
            add_items(pid, live_new, position=0)
            existing_list = live_new + existing_list
            print(f"Dodano bieżących: {len(live_new)}")

        # Marker LIVE przesuwamy dopiero po pełnym przetworzeniu tego etapu.
        latest = newest_time(today_rows_all)

        if latest:
            current_live_day = now_pl().date()
            current_live_time = latest

            update_markers(
                pid,
                backfill_day=current_backfill_marker,
                live_day=current_live_day,
                live_time=current_live_time,
            )

            print(
                f"Zapisano marker LIVE: "
                f"{current_live_day} {current_live_time}"
            )

        # ---------------------------------------------------------
        # 2. ARCHIWUM
        # ---------------------------------------------------------
        if (
            len(existing_list) < MAX_TRACKS
            and BACKFILL_DAYS_PER_RUN > 0
        ):
            day = next_backfill_day(current_backfill_marker)

            for _ in range(BACKFILL_DAYS_PER_RUN):
                runtime_guard()

                if len(existing_list) >= MAX_TRACKS:
                    break

                rows = parse_radio357_page(
                    DAY_URL.format(day=day.isoformat())
                )

                print(
                    f"Archiwum {day}: odczytano {len(rows)} pozycji."
                )

                archive_new = []
                misses = []

                if rows:
                    archive_new, misses = resolve_rows(
                        rows,
                        existing_uris,
                        existing_keys,
                    )

                    archive_misses.extend(misses)

                    remaining = MAX_TRACKS - len(existing_list)
                    archive_new = archive_new[:remaining]

                    if archive_new:
                        add_items(pid, archive_new)
                        existing_list.extend(archive_new)

                        print(
                            f"Archiwum {day}: "
                            f"dodano {len(archive_new)}"
                        )

                # Tylko po pełnym przejściu dnia przesuwamy marker.
                current_backfill_marker = day

                update_markers(
                    pid,
                    backfill_day=current_backfill_marker,
                    live_day=current_live_day,
                    live_time=current_live_time,
                )

                print(
                    f"Zapisano marker BACKFILL: "
                    f"{current_backfill_marker}"
                )

                day -= timedelta(days=1)

    except GracefulStop as exc:
        print(f"STOP KONTROLOWANY: {exc}")
        print(
            "Stan playlisty został zachowany. "
            "Następne uruchomienie będzie kontynuować."
        )

    # -------------------------------------------------------------
    # 3. LIMIT 5000
    # -------------------------------------------------------------
    try:
        if len(existing_list) > MAX_TRACKS:
            existing_list = existing_list[:MAX_TRACKS]
            replace_items(pid, existing_list)
            print(
                f"Przycięto playlistę do {MAX_TRACKS} utworów."
            )
    except GracefulStop as exc:
        print(f"Pomijam przycięcie w tym runie: {exc}")

    elapsed = int(time.monotonic() - STARTED)

    print(
        f"GOTOWE. 753Radio: {len(existing_list)} utworów. "
        f"Czas: {elapsed}s."
    )

    print(
        "Niedopasowane w tym uruchomieniu: "
        f"{len(live_misses) + len(archive_misses)}"
    )


if __name__ == "__main__":
    main()
