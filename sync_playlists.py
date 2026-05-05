"""
sync_playlists.py
-----------------
Migreert Spotify-afspeellijsten naar Tidal.

Spotify : OAuth2 Authorization Code Flow (copy-paste callback)
Tidal   : OAuth2 Device Flow via tidalapi (geen redirect URI nodig)

Gebruik:
    pip install -r requirements.txt

    # Eenmalig authenticeren:
    python sync_playlists.py --auth

    # Of afzonderlijk:
    python sync_playlists.py --auth-spotify
    python sync_playlists.py --auth-tidal

    # Migratie starten:
    python sync_playlists.py
"""

import argparse
import logging
import os
import pickle
import sys
import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import tidalapi
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

# Spotify credentials (uit .env)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv(
    "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
)
SPOTIFY_ACCESS_TOKEN = os.getenv("SPOTIFY_ACCESS_TOKEN", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")

# Tidal sessie wordt opgeslagen als pickle (geen losse tokens in .env nodig)
TIDAL_SESSION_FILE = "tidal_session.pkl"
TIDAL_COUNTRY_CODE = os.getenv("TIDAL_COUNTRY_CODE", "NL")

# Rate-limit instellingen
REQUEST_DELAY = 0.3
RETRY_AFTER_DEFAULT = 5
MAX_RETRIES = 3
ENV_FILE = ".env"

# ---------------------------------------------------------------------------
# Generieke HTTP-hulpfunctie (voor Spotify)
# ---------------------------------------------------------------------------


def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    """GET met automatische retry bij 429 (rate limit) en 5xx (serverfout)."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT))
            log.warning("Rate-limit (GET %s). Wacht %ds ...", url, wait)
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503, 504):
            wait = RETRY_AFTER_DEFAULT * attempt
            log.warning(
                "Serverfout %d op %s (poging %d/%d). Wacht %ds ...",
                resp.status_code,
                url,
                attempt,
                MAX_RETRIES,
                wait,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        return resp.json()
    raise RuntimeError(f"GET {url} mislukt na {MAX_RETRIES} pogingen.")


# ---------------------------------------------------------------------------
# Spotify OAuth2 - Authorization Code Flow (copy-paste)
# ---------------------------------------------------------------------------

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"


def spotify_headers() -> dict:
    # Lees altijd de meest recente waarde van SPOTIFY_ACCESS_TOKEN
    return {
        "Authorization": f"Bearer {os.getenv('SPOTIFY_ACCESS_TOKEN', SPOTIFY_ACCESS_TOKEN)}"
    }


def spotify_refresh_access_token() -> None:
    global SPOTIFY_ACCESS_TOKEN
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": SPOTIFY_REFRESH_TOKEN,
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    SPOTIFY_ACCESS_TOKEN = resp.json()["access_token"]
    # Sla op in .env én in omgevingsvariabele zodat spotify_headers() het direct oppikt
    set_key(ENV_FILE, "SPOTIFY_ACCESS_TOKEN", SPOTIFY_ACCESS_TOKEN, quote_mode="never")
    os.environ["SPOTIFY_ACCESS_TOKEN"] = SPOTIFY_ACCESS_TOKEN
    log.info("Spotify access-token vernieuwd.")


def spotify_authorize() -> None:
    """
    Spotify Authorization Code Flow via copy-paste.
    Stel in de Spotify Dashboard in: Redirect URI = http://127.0.0.1:8888/callback
    """
    global SPOTIFY_ACCESS_TOKEN, SPOTIFY_REFRESH_TOKEN

    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPES,
    }
    auth_url = SPOTIFY_AUTH_URL + "?" + urlencode(params)

    print("\n" + "=" * 60)
    print("SPOTIFY - Stap 1: Open deze URL en ga akkoord:")
    print("=" * 60)
    print(auth_url)
    print("=" * 60)
    webbrowser.open(auth_url)

    print("\nStap 2: Na akkoord stuurt Spotify je naar een URL die")
    print("        'kan niet worden bereikt' geeft. Dat klopt.")
    print("        Kopieer de VOLLEDIGE URL en plak hem hieronder:\n")
    callback_url = input("Plak de callback-URL hier: ").strip()

    qs = parse_qs(urlparse(callback_url).query)
    if "error" in qs:
        raise RuntimeError(f"Spotify fout: {qs['error']}")
    if "code" not in qs:
        raise RuntimeError("Geen 'code' in de URL. Volledige URL geplakt?")

    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": qs["code"][0],
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()
    SPOTIFY_ACCESS_TOKEN = tokens["access_token"]
    SPOTIFY_REFRESH_TOKEN = tokens.get("refresh_token", SPOTIFY_REFRESH_TOKEN)

    set_key(ENV_FILE, "SPOTIFY_ACCESS_TOKEN", SPOTIFY_ACCESS_TOKEN, quote_mode="never")
    set_key(
        ENV_FILE, "SPOTIFY_REFRESH_TOKEN", SPOTIFY_REFRESH_TOKEN, quote_mode="never"
    )
    log.info("Spotify-tokens opgeslagen in %s.", ENV_FILE)


# ---------------------------------------------------------------------------
# Tidal - via tidalapi (Device Flow, geen redirect URI nodig)
# ---------------------------------------------------------------------------


def tidal_load_or_login() -> tidalapi.Session:
    """
    Laad een bestaande Tidal-sessie of start een nieuwe Device Flow login.
    De sessie wordt opgeslagen in tidal_session.pkl.
    """
    session = tidalapi.Session()

    if os.path.exists(TIDAL_SESSION_FILE):
        try:
            with open(TIDAL_SESSION_FILE, "rb") as f:
                data = pickle.load(f)
            ok = session.load_oauth_session(
                data["token_type"],
                data["access_token"],
                data["refresh_token"],
                data["expiry_time"],
            )
            if ok and session.check_login():
                log.info("Tidal-sessie geladen uit %s.", TIDAL_SESSION_FILE)
                return session
            log.info("Opgeslagen Tidal-sessie verlopen, opnieuw inloggen ...")
        except Exception as exc:
            log.warning("Kon Tidal-sessie niet laden: %s", exc)

    return tidal_authorize()


def tidal_authorize(session: tidalapi.Session | None = None) -> tidalapi.Session:
    """
    Start een Tidal Device Flow login.
    Geen redirect URI of lokale server nodig - de gebruiker bezoekt
    link.tidal.com en voert een code in.
    """
    if session is None:
        session = tidalapi.Session()

    print("\n" + "=" * 60)
    print("TIDAL - Device Flow login (geen redirect URI nodig)")
    print("=" * 60)

    login, future = session.login_oauth()

    print(f"\nOpen deze URL in je browser:")
    print(f"  {login.verification_uri_complete}")
    print(f"\nOf ga naar:  {login.verification_uri}")
    print(f"En voer in:  {login.user_code}")
    webbrowser.open(login.verification_uri_complete)
    print("\nWachten tot je ingelogd bent ...")

    future.result()  # blokkeert tot login voltooid

    if not session.check_login():
        raise RuntimeError("Tidal login mislukt.")

    _tidal_save_session(session)
    log.info("Tidal-sessie opgeslagen in %s.", TIDAL_SESSION_FILE)
    return session


def _tidal_save_session(session: tidalapi.Session) -> None:
    data = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time,
    }
    with open(TIDAL_SESSION_FILE, "wb") as f:
        pickle.dump(data, f)


# ---------------------------------------------------------------------------
# Spotify API-wrappers
# ---------------------------------------------------------------------------

SPOTIFY_BASE = "https://api.spotify.com/v1"


def spotify_get_current_user_id() -> str:
    return _get(f"{SPOTIFY_BASE}/me", headers=spotify_headers())["id"]


def spotify_get_all_playlists() -> list[dict]:
    playlists: list[dict] = []
    limit = 50
    offset = 0

    while True:
        data = _get(
            f"{SPOTIFY_BASE}/me/playlists",
            headers=spotify_headers(),
            params={"limit": limit, "offset": offset},
        )
        items = data.get("items", [])
        playlists.extend(items)
        log.info(
            "Spotify: %d/%d playlists opgehaald ...",
            len(playlists),
            data.get("total", "?"),
        )

        # Stop als er geen volgende pagina is of we alles hebben
        if len(items) < limit or data.get("next") is None:
            break
        offset += limit
        time.sleep(REQUEST_DELAY)

    return playlists


def spotify_get_tracks_in_playlist(playlist_id: str) -> list[dict]:
    tracks: list[dict] = []
    url = f"{SPOTIFY_BASE}/playlists/{playlist_id}/tracks"
    params: dict | None = {
        "limit": 100,
        "fields": "next,items(track(name,artists,external_ids))",
    }

    while url:
        data = _get(url, headers=spotify_headers(), params=params)
        for item in data.get("items", []):
            if item.get("track"):
                tracks.append(item["track"])
        url = data.get("next")
        params = None

    return tracks


# ---------------------------------------------------------------------------
# Tidal API-wrappers (via tidalapi)
# ---------------------------------------------------------------------------


def tidal_search_by_isrc(session: tidalapi.Session, isrc: str) -> int | None:
    """Zoek een track via ISRC. Geeft Tidal track-ID (int) of None terug."""
    try:
        tracks = session.get_tracks_by_isrc(isrc)
        if tracks:
            return tracks[0].id
    except Exception as exc:
        log.debug("ISRC-zoekopdracht mislukt voor %s: %s", isrc, exc)
    return None


def tidal_create_playlist(
    session: tidalapi.Session, name: str, description: str = ""
) -> tidalapi.UserPlaylist:
    playlist = session.user.create_playlist(name, description)
    log.info("Tidal-playlist aangemaakt: '%s' (id=%s)", name, playlist.id)
    return playlist


def tidal_add_tracks(playlist: tidalapi.UserPlaylist, track_ids: list[int]) -> None:
    CHUNK_SIZE = 50
    for i in range(0, len(track_ids), CHUNK_SIZE):
        chunk = track_ids[i : i + CHUNK_SIZE]
        playlist.add(chunk)
        log.info(
            "  -> %d tracks toegevoegd (batch %d).", len(chunk), i // CHUNK_SIZE + 1
        )
        time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Hoofd-migratiefunctie
# ---------------------------------------------------------------------------


def migrate_playlists() -> None:
    if not SPOTIFY_ACCESS_TOKEN:
        log.error(
            "SPOTIFY_ACCESS_TOKEN ontbreekt.\n"
            "Voer eerst uit: python sync_playlists.py --auth-spotify"
        )
        sys.exit(1)

    log.info("Tidal-sessie laden ...")
    tidal_session = tidal_load_or_login()

    # Stap 1: Spotify playlists ophalen
    log.info("=== Stap 1: Spotify-playlists ophalen ===")

    # Controleer of het token nog geldig is via /me
    try:
        _get(f"{SPOTIFY_BASE}/me", headers=spotify_headers())
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            log.info("Spotify-token verlopen, vernieuwen ...")
            spotify_refresh_access_token()
        else:
            raise

    playlists = spotify_get_all_playlists()
    log.info("Totaal %d playlists gevonden op Spotify.", len(playlists))

    total_found = 0
    total_not_found = 0
    not_found_log: list[str] = []

    for idx, playlist in enumerate(playlists, start=1):
        pl_name = playlist.get("name", f"Playlist {idx}")
        pl_id = playlist["id"]
        log.info("\n-- Playlist %d/%d: '%s' --", idx, len(playlists), pl_name)

        # Stap 2: Tidal playlist aanmaken
        try:
            tidal_pl = tidal_create_playlist(
                tidal_session,
                name=pl_name,
                description=f"Gemigreerd vanuit Spotify (id={pl_id})",
            )
        except Exception as exc:
            log.error("Kan Tidal-playlist '%s' niet aanmaken: %s", pl_name, exc)
            continue

        # Stap 3: Spotify tracks ophalen
        try:
            tracks = spotify_get_tracks_in_playlist(pl_id)
        except requests.HTTPError:
            log.warning("Kan tracks van '%s' niet ophalen, sla over.", pl_name)
            continue

        log.info("  %d tracks gevonden in '%s'.", len(tracks), pl_name)

        # Stap 3b: ISRC opzoeken op Tidal
        tidal_track_ids: list[int] = []

        for track in tracks:
            name = track.get("name", "Onbekend")
            artists = ", ".join(a["name"] for a in track.get("artists", []))
            isrc = track.get("external_ids", {}).get("isrc")

            if not isrc:
                msg = f"Geen ISRC: '{name}' - {artists}"
                log.warning("  !  %s", msg)
                not_found_log.append(msg)
                total_not_found += 1
                continue

            tidal_id = tidal_search_by_isrc(tidal_session, isrc)

            if tidal_id:
                tidal_track_ids.append(tidal_id)
                total_found += 1
            else:
                msg = f"Niet op Tidal: '{name}' - {artists} (ISRC={isrc})"
                log.warning("  x  %s", msg)
                not_found_log.append(msg)
                total_not_found += 1

        # Stap 4: Tracks toevoegen
        if tidal_track_ids:
            tidal_add_tracks(tidal_pl, tidal_track_ids)
            log.info(
                "  OK %d tracks toegevoegd aan '%s'.", len(tidal_track_ids), pl_name
            )
        else:
            log.info("  -  Geen tracks om toe te voegen voor '%s'.", pl_name)

        # Sessie periodiek opslaan
        _tidal_save_session(tidal_session)

    # Eindrapport
    log.info("\n" + "=" * 50)
    log.info("Migratie voltooid!")
    log.info("Tracks toegevoegd : %d", total_found)
    log.info("Niet gevonden     : %d", total_not_found)

    if not_found_log:
        log.info("\nOntbrekende tracks:")
        for entry in not_found_log:
            log.info("  - %s", entry)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Spotify -> Tidal migratie")
    parser.add_argument(
        "--auth", action="store_true", help="Authenticeer bij Spotify en Tidal."
    )
    parser.add_argument(
        "--auth-spotify", action="store_true", help="Authenticeer alleen bij Spotify."
    )
    parser.add_argument(
        "--auth-tidal", action="store_true", help="Authenticeer alleen bij Tidal."
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
