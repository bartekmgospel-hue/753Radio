# Radio 357 → Spotify

Automatyczna playlista Spotify z utworami granymi w **Radio 357**.

## Co robi

- pobiera historię utworów Radio 357 z publicznej strony radio111.pl,
- wyszukuje wykonawcę i tytuł w Spotify,
- stosuje dopasowanie tekstowe, aby ograniczyć błędne wersje,
- nie dodaje duplikatów obecnych na playliście,
- utrzymuje maksymalnie 1000 pozycji,
- może działać automatycznie co 15 minut przez GitHub Actions,
- korzysta z aktualnego API Spotify `/playlists/{id}/items` (2026).

## 1. Wymagania

- konto Spotify Premium (wymagane dla właściciela aplikacji Development Mode),
- darmowe konto GitHub,
- Python 3.10+ tylko do jednorazowej konfiguracji.

## 2. Utwórz aplikację Spotify

Wejdź do Spotify Developer Dashboard i utwórz aplikację.

W konfiguracji aplikacji dodaj dokładnie ten Redirect URI:

    http://127.0.0.1:8888/callback

Zapisz:
- Client ID
- Client Secret

## 3. Jednorazowo uzyskaj Refresh Token

Windows PowerShell:

    $env:SPOTIFY_CLIENT_ID="TWÓJ_CLIENT_ID"
    $env:SPOTIFY_CLIENT_SECRET="TWÓJ_CLIENT_SECRET"
    pip install -r requirements.txt
    python auth_setup.py

macOS/Linux:

    export SPOTIFY_CLIENT_ID="TWÓJ_CLIENT_ID"
    export SPOTIFY_CLIENT_SECRET="TWÓJ_CLIENT_SECRET"
    pip install -r requirements.txt
    python auth_setup.py

Skrypt otworzy Spotify w przeglądarce. Po akceptacji wyświetli `SPOTIFY_REFRESH_TOKEN`.

**Nie publikuj Client Secret ani Refresh Token.**

## 4. GitHub

Utwórz prywatne repozytorium i wgraj zawartość tego katalogu.

W repozytorium otwórz:

Settings → Secrets and variables → Actions → New repository secret

Dodaj trzy sekrety:

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REFRESH_TOKEN`

`SPOTIFY_PLAYLIST_ID` jest opcjonalny. Jeśli go nie ustawisz, skrypt znajdzie playlistę
o nazwie `753Radio`, a jeśli jej nie ma — utworzy prywatną playlistę.

## 5. Pierwsze uruchomienie

GitHub → Actions → `Radio 357 -> Spotify` → Run workflow.

Po chwili w Spotify powinna pojawić się playlista:

**753Radio**

Od tej chwili GitHub będzie synchronizował ją automatycznie co 15 minut.

## Test lokalny

    python sync_radio357.py

Do testu bez zmian w Spotify:

    DRY_RUN=1 python sync_radio357.py

## Zmiana limitu

Domyślnie playlista przechowuje 5000 ostatnich dostępnych, unikalnych utworów.

W pliku `.github/workflows/radio357.yml` zmień:

    MAX_TRACKS: "5000"

np. na 500, 2000 itd.

## Ważna uwaga

Źródłem historii jest zewnętrzna publiczna strona radio111.pl. Jeśli kiedyś zmieni HTML
lub przestanie publikować historię Radia 357, parser trzeba będzie dostosować.
