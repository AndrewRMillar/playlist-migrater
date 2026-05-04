"""
sync_playlists.py
-----------------
Migreert Spotify-afspeellijsten naar Tidal via ISRC-matching.

Vereisten:
  - Python 3.10+
  - pip install -r requirements.txt
  - Gevulde .env (zie .env.example)

Authenticatie:
  Voer eerst `python sync_playlists.py --auth` uit om tokens op te halen,
  of sla ze handmatig op in .env (zie README).
"""

import argparse
import logging
import os
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv, set_key

# ---------------------------------------------------------------------------
# Configuratie & logging
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync_playlists.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Spotify
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")
SPOTIFY_ACCESS_TOKEN  = os.getenv("SPOTIFY_ACCESS_TOKEN", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")

# Tidal
TIDAL_CLIENT_ID       = os.getenv("TIDAL_CLIENT_ID", "")
TIDAL_CLIENT_SECRET   = os.getenv("TIDAL_CLIENT_SECRET", "")
TIDAL_REDIRECT_URI    = os.getenv("TIDAL_REDIRECT_URI", "http://localhost:8889/callback")
TIDAL_ACCESS_TOKEN    = os.getenv("TIDAL_ACCESS_TOKEN", "")
TIDAL_REFRESH_TOKEN   = os.getenv("TIDAL_REFRESH_TOKEN", "")
TIDAL_USER_ID         = os.getenv("TIDAL_USER_ID", "")
TIDAL_COUNTRY_CODE    = os.getenv("TIDAL_COUNTRY_CODE", "NL")

# Rate-limit pauzes (seconden)
REQUEST_DELAY         = 0.3   # tussen gewone API-calls
RETRY_AFTER_DEFAULT   = 5     # als Retry-After header ontbreekt
MAX_RETRIES           = 3

ENV_FILE = ".env"

# ---------------------------------------------------------------------------
# Generieke HTTP-hulpfuncties
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    """GET met automatische retry bij 429 (rate limit)."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT))
            log.warning("Rate-limit geraakt (GET %s). Wacht %ds …", url, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        return resp.json()
    raise RuntimeError(f"GET {url} mislukt na {MAX_RETRIES} pogingen.")


def _post(url: str, headers: dict, json_body: dict | None = None,
          data: dict | None = None) -> dict:
    """POST met automatische retry bij 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post(url, headers=headers, json=json_body,
                             data=data, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT))
            log.warning("Rate-limit geraakt (POST %s). Wacht %ds …", url, wait)
            time.sleep(wait)
            continue
        if resp.status_code in (200, 201, 204):
            time.sleep(REQUEST_DELAY)
            try:
                return resp.json()
            except Exception:
                return {}
        resp.raise_for_status()
    raise RuntimeError(f"POST {url} mislukt na {MAX_RETRIES} pogingen.")


# ---------------------------------------------------------------------------
# OAuth2 – Spotify
# ---------------------------------------------------------------------------

SPOTIFY_AUTH_URL  = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES    = "playlist-read-private playlist-read-collaborative"


def spotify_headers() -> dict:
    return {"Authorization": f"Bearer {SPOTIFY_ACCESS_TOKEN}"}


def spotify_refresh_access_token() -> None:
    """Vernieuw het Spotify access-token met het refresh-token."""
    global SPOTIFY_ACCESS_TOKEN
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": SPOTIFY_REFRESH_TOKEN,
            "client_id":     SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    SPOTIFY_ACCESS_TOKEN = data["access_token"]
    set_key(ENV_FILE, "SPOTIFY_ACCESS_TOKEN", SPOTIFY_ACCESS_TOKEN)
    log.info("Spotify access-token vernieuwd.")


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Eenmalige HTTP-server om de OAuth-callback op te vangen."""

    auth_code: str | None = None

    def do_GET(self):  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        _OAuthCallbackHandler.auth_code = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Autorisatie gelukt! Je kunt dit venster sluiten.")

    def log_message(self, *_):  # suppress server logs
        pass


def _run_local_callback_server(port: int) -> str:
    server = HTTPServer(("localhost", port), _OAuthCallbackHandler)
    server.handle_request()          # wacht op één verzoek
    return _OAuthCallbackHandler.auth_code or ""


def spotify_authorize() -> None:
    """Voer de volledige Authorization Code Flow uit voor Spotify."""
    global SPOTIFY_ACCESS_TOKEN, SPOTIFY_REFRESH_TOKEN

    params = {
        "client_id":     SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "scope":         SPOTIFY_SCOPES,
    }
    url = SPOTIFY_AUTH_URL + "?" + urlencode(params)
    log.info("Open Spotify-autorisatie-URL:\n%s", url)
    webbrowser.open(url)

    port = int(urlparse(SPOTIFY_REDIRECT_URI).port or 8888)
    log.info("Wacht op callback op poort %d …", port)
    code = _run_local_callback_server(port)
    if not code:
        raise RuntimeError("Geen autorisatiecode ontvangen van Spotify.")

    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "client_id":    SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    SPOTIFY_ACCESS_TOKEN  = tokens["access_token"]
    SPOTIFY_REFRESH_TOKEN = tokens.get("refresh_token", SPOTIFY_REFRESH_TOKEN)

    set_key(ENV_FILE, "SPOTIFY_ACCESS_TOKEN",  SPOTIFY_ACCESS_TOKEN)
    set_key(ENV_FILE, "SPOTIFY_REFRESH_TOKEN", SPOTIFY_REFRESH_TOKEN)
    log.info("Spotify-tokens opgeslagen in %s.", ENV_FILE)


# ---------------------------------------------------------------------------
# OAuth2 – Tidal
# ---------------------------------------------------------------------------

TIDAL_AUTH_URL  = "https://login.tidal.com/oauth2/authorize"
TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_SCOPES    = "playlists.read playlists.write user.read"


def tidal_headers() -> dict:
    return {
        "Authorization": f"Bearer {TIDAL_ACCESS_TOKEN}",
        "Content-Type":  "application/vnd.api+json",
    }


def tidal_refresh_access_token() -> None:
    """Vernieuw het Tidal access-token met het refresh-token."""
    global TIDAL_ACCESS_TOKEN
    resp = requests.post(
        TIDAL_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": TIDAL_REFRESH_TOKEN,
            "client_id":     TIDAL_CLIENT_ID,
            "client_secret": TIDAL_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    TIDAL_ACCESS_TOKEN = data["access_token"]
    set_key(ENV_FILE, "TIDAL_ACCESS_TOKEN", TIDAL_ACCESS_TOKEN)
    log.info("Tidal access-token vernieuwd.")


def tidal_authorize() -> None:
    """Voer de volledige Authorization Code Flow uit voor Tidal."""
    global TIDAL_ACCESS_TOKEN, TIDAL_REFRESH_TOKEN, TIDAL_USER_ID

    params = {
        "response_type": "code",
        "client_id":     TIDAL_CLIENT_ID,
        "redirect_uri":  TIDAL_REDIRECT_URI,
        "scope":         TIDAL_SCOPES,
    }
    url = TIDAL_AUTH_URL + "?" + urlencode(params)
    log.info("Open Tidal-autorisatie-URL:\n%s", url)
    webbrowser.open(url)

    port = int(urlparse(TIDAL_REDIRECT_URI).port or 8889)
    log.info("Wacht op callback op poort %d …", port)
    code = _run_local_callback_server(port)
    if not code:
        raise RuntimeError("Geen autorisatiecode ontvangen van Tidal.")

    resp = requests.post(
        TIDAL_TOKEN_URL,
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": TIDAL_REDIRECT_URI,
            "client_id":    TIDAL_CLIENT_ID,
            "client_secret": TIDAL_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    TIDAL_ACCESS_TOKEN  = tokens["access_token"]
    TIDAL_REFRESH_TOKEN = tokens.get("refresh_token", TIDAL_REFRESH_TOKEN)

    # Haal user-ID op
    me = _get("https://openapi.tidal.com/v2/users/me",
              headers=tidal_headers())
    TIDAL_USER_ID = str(me.get("data", {}).get("id", ""))

    set_key(ENV_FILE, "TIDAL_ACCESS_TOKEN",  TIDAL_ACCESS_TOKEN)
    set_key(ENV_FILE, "TIDAL_REFRESH_TOKEN", TIDAL_REFRESH_TOKEN)
    set_key(ENV_FILE, "TIDAL_USER_ID",       TIDAL_USER_ID)
    log.info("Tidal-tokens en user-ID opgeslagen in %s.", ENV_FILE)


# ---------------------------------------------------------------------------
# Spotify API-wrappers
# ---------------------------------------------------------------------------

SPOTIFY_BASE = "https://api.spotify.com/v1"


def spotify_get_current_user_id() -> str:
    data = _get(f"{SPOTIFY_BASE}/me", headers=spotify_headers())
    return data["id"]


def spotify_get_all_playlists(user_id: str) -> list[dict]:
    """Haalt alle playlists op van de ingelogde gebruiker (met paginering)."""
    playlists: list[dict] = []
    url = f"{SPOTIFY_BASE}/users/{user_id}/playlists"
    params = {"limit": 50, "offset": 0}

    while url:
        data = _get(url, headers=spotify_headers(), params=params)
        playlists.extend(data.get("items", []))
        url    = data.get("next")   # next is al een volledige URL
        params = None               # params zijn al in de next-URL verwerkt
        log.info("Spotify: %d playlists opgehaald tot nu toe …", len(playlists))

    return playlists


def spotify_get_tracks_in_playlist(playlist_id: str) -> list[dict]:
    """Haalt alle tracks van een playlist op, inclusief ISRC-codes."""
    tracks: list[dict] = []
    url    = f"{SPOTIFY_BASE}/playlists/{playlist_id}/tracks"
    params = {"limit": 100, "offset": 0,
              "fields": "next,items(track(name,artists,external_ids))"}

    while url:
        data = _get(url, headers=spotify_headers(), params=params)
        for item in data.get("items", []):
            track = item.get("track")
            if track:
                tracks.append(track)
        url    = data.get("next")
        params = None

    return tracks


# ---------------------------------------------------------------------------
# Tidal API-wrappers
# ---------------------------------------------------------------------------

TIDAL_BASE = "https://openapi.tidal.com/v2"


def tidal_search_by_isrc(isrc: str) -> str | None:
    """Zoek een track op Tidal via ISRC. Geeft de Tidal track-ID terug of None."""
    try:
        data = _get(
            f"{TIDAL_BASE}/tracks",
            headers=tidal_headers(),
            params={
                "filter[isrc]":    isrc,
                "countryCode":     TIDAL_COUNTRY_CODE,
                "include":         "artists",
            },
        )
        items = data.get("data", [])
        if items:
            return items[0]["id"]
    except requests.HTTPError as exc:
        log.debug("Tidal ISRC-zoekopdracht mislukt voor %s: %s", isrc, exc)
    return None


def tidal_create_playlist(name: str, description: str = "") -> str:
    """Maak een nieuwe Tidal-playlist aan en geef het ID terug."""
    body = {
        "data": {
            "type": "playlists",
            "attributes": {
                "name":        name,
                "description": description,
                "privacy":     "PRIVATE",
            },
        }
    }
    data = _post(
        f"{TIDAL_BASE}/users/{TIDAL_USER_ID}/playlists",
        headers=tidal_headers(),
        json_body=body,
    )
    playlist_id = data["data"]["id"]
    log.info("Tidal-playlist aangemaakt: '%s' (id=%s)", name, playlist_id)
    return playlist_id


def tidal_add_tracks_to_playlist(playlist_id: str, track_ids: list[str]) -> None:
    """Voeg tracks toe aan een Tidal-playlist (maximaal 50 per keer)."""
    # Tidal accepteert max 50 tracks per request
    CHUNK_SIZE = 50
    for i in range(0, len(track_ids), CHUNK_SIZE):
        chunk = track_ids[i : i + CHUNK_SIZE]
        body = {
            "data": [
                {"type": "tracks", "id": tid} for tid in chunk
            ]
        }
        _post(
            f"{TIDAL_BASE}/playlists/{playlist_id}/relationships/items",
            headers=tidal_headers(),
            json_body=body,
        )
        log.info("  → %d tracks toegevoegd aan playlist %s (batch %d).",
                 len(chunk), playlist_id, i // CHUNK_SIZE + 1)


# ---------------------------------------------------------------------------
# Hoofd-migratiefunctie
# ---------------------------------------------------------------------------

def migrate_playlists() -> None:
    """Volledige migratie: Spotify → Tidal."""

    # Valideer vereiste tokens
    missing = []
    for var in ("SPOTIFY_ACCESS_TOKEN", "TIDAL_ACCESS_TOKEN", "TIDAL_USER_ID"):
        if not globals()[var]:
            missing.append(var)
    if missing:
        log.error(
            "Ontbrekende omgevingsvariabelen: %s\n"
            "Voer eerst `python sync_playlists.py --auth` uit.",
            ", ".join(missing),
        )
        sys.exit(1)

    # ── Stap 1: Haal Spotify-playlists op ───────────────────────────────────
    log.info("=== Stap 1: Spotify-playlists ophalen ===")
    try:
        user_id = spotify_get_current_user_id()
    except requests.HTTPError:
        log.info("Access-token verlopen, token vernieuwen …")
        spotify_refresh_access_token()
        user_id = spotify_get_current_user_id()

    playlists = spotify_get_all_playlists(user_id)
    log.info("Totaal %d playlists gevonden op Spotify.", len(playlists))

    # Statistieken
    total_tracks_found     = 0
    total_tracks_not_found = 0
    not_found_log: list[str] = []

    for pl_index, playlist in enumerate(playlists, start=1):
        pl_name = playlist.get("name", f"Playlist {pl_index}")
        pl_id   = playlist["id"]
        log.info("\n── Playlist %d/%d: '%s' ──", pl_index, len(playlists), pl_name)

        # ── Stap 2: Maak Tidal-playlist aan ─────────────────────────────────
        try:
            tidal_pl_id = tidal_create_playlist(
                name=pl_name,
                description=f"Gemigreerd vanuit Spotify (id={pl_id})",
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                tidal_refresh_access_token()
                tidal_pl_id = tidal_create_playlist(pl_name)
            else:
                log.error("Kan Tidal-playlist '%s' niet aanmaken: %s", pl_name, exc)
                continue

        # ── Stap 3: Haal Spotify-tracks op ──────────────────────────────────
        try:
            tracks = spotify_get_tracks_in_playlist(pl_id)
        except requests.HTTPError:
            log.warning("Kan tracks van '%s' niet ophalen, sla over.", pl_name)
            continue

        log.info("  %d tracks gevonden in '%s'.", len(tracks), pl_name)

        # ── Stap 3b: Zoek via ISRC op Tidal ─────────────────────────────────
        tidal_track_ids: list[str] = []

        for track in tracks:
            track_name    = track.get("name", "Onbekend")
            artists       = ", ".join(a["name"] for a in track.get("artists", []))
            isrc          = track.get("external_ids", {}).get("isrc")

            if not isrc:
                msg = f"Geen ISRC voor: '{track_name}' – {artists}"
                log.warning("  ⚠  %s", msg)
                not_found_log.append(msg)
                total_tracks_not_found += 1
                continue

            tidal_id = tidal_search_by_isrc(isrc)

            if tidal_id:
                tidal_track_ids.append(tidal_id)
                total_tracks_found += 1
            else:
                msg = f"Niet gevonden op Tidal: '{track_name}' – {artists} (ISRC={isrc})"
                log.warning("  ✗  %s", msg)
                not_found_log.append(msg)
                total_tracks_not_found += 1

        # ── Stap 4: Voeg tracks toe aan Tidal-playlist ───────────────────────
        if tidal_track_ids:
            tidal_add_tracks_to_playlist(tidal_pl_id, tidal_track_ids)
            log.info("  ✓  %d tracks toegevoegd aan '%s'.",
                     len(tidal_track_ids), pl_name)
        else:
            log.info("  –  Geen tracks om toe te voegen voor '%s'.", pl_name)

    # ── Eindrapport ──────────────────────────────────────────────────────────
    log.info("\n=== Migratie voltooid ===")
    log.info("Tracks gevonden en toegevoegd : %d", total_tracks_found)
    log.info("Tracks NIET gevonden op Tidal : %d", total_tracks_not_found)

    if not_found_log:
        log.info("\nOntbrekende tracks (ook in sync_playlists.log):")
        for entry in not_found_log:
            log.info("  • %s", entry)


# ---------------------------------------------------------------------------
# CLI-entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spotify → Tidal playlist-migratie"
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Voer de OAuth2-flow uit voor Spotify én Tidal en sla tokens op.",
    )
    parser.add_argument(
        "--auth-spotify",
        action="store_true",
        help="Voer alleen de Spotify OAuth2-flow uit.",
    )
    parser.add_argument(
        "--auth-tidal",
        action="store_true",
        help="Voer alleen de Tidal OAuth2-flow uit.",
    )
    args = parser.parse_args()

    if args.auth or args.auth_spotify:
        spotify_authorize()
    if args.auth or args.auth_tidal:
        tidal_authorize()
    if not (args.auth or args.auth_spotify or args.auth_tidal):
        migrate_playlists()


if __name__ == "__main__":
    main()
