"""
sync_playlists.py
-----------------
Migrates Spotify playlists to Tidal.

Spotify : OAuth2 Authorization Code Flow (copy-paste callback)
Tidal   : OAuth2 Device Flow via tidalapi (no redirect URI needed)

Usage:
    pip install -r requirements.txt
    python sync_playlists.py --auth-spotify   # one-time authentication
    python sync_playlists.py --auth-tidal     # one-time authentication
    python sync_playlists.py                  # start migration
"""

import argparse
import logging
import os
import pickle
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import tidalapi
from dotenv import load_dotenv, set_key

# ---------------------------------------------------------------------------
# Configuration & logging
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

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv(
    "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
)
SPOTIFY_ACCESS_TOKEN = os.getenv("SPOTIFY_ACCESS_TOKEN", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")

TIDAL_SESSION_FILE = "tidal_session.pkl"
TIDAL_COUNTRY_CODE = os.getenv("TIDAL_COUNTRY_CODE", "NL")
TRACK_CACHE_FILE = "track_cache.pkl"

REQUEST_DELAY = 0.3  # seconds between API calls
RETRY_AFTER_DEFAULT = 5  # seconds to wait when Retry-After header is missing
MAX_RETRIES = 5
ENV_FILE = ".env"
SPOTIFY_BASE = "https://api.spotify.com/v1"

# ---------------------------------------------------------------------------
# Generic HTTP helper
# ---------------------------------------------------------------------------


def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    """GET with automatic retry on 429 (rate limit) and 5xx (server error)."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT))
            log.warning("Rate limit hit on %s. Waiting %ds ...", url, wait)
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503, 504):
            wait = RETRY_AFTER_DEFAULT * attempt
            log.warning(
                "Server error %d (attempt %d/%d). Waiting %ds ...",
                resp.status_code,
                attempt,
                MAX_RETRIES,
                wait,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        return resp.json()
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts.")


# ---------------------------------------------------------------------------
# Spotify OAuth2 – Authorization Code Flow (copy-paste)
# ---------------------------------------------------------------------------

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"


def spotify_headers() -> dict:
    # Always read the most recent token value from the environment
    token = os.getenv("SPOTIFY_ACCESS_TOKEN", SPOTIFY_ACCESS_TOKEN)
    return {"Authorization": f"Bearer {token}"}


def spotify_refresh_access_token() -> None:
    """Refresh the Spotify access token using the stored refresh token."""
    global SPOTIFY_ACCESS_TOKEN
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.getenv("SPOTIFY_REFRESH_TOKEN", SPOTIFY_REFRESH_TOKEN),
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    SPOTIFY_ACCESS_TOKEN = resp.json()["access_token"]
    # Save to .env and env vars so spotify_headers() picks it up immediately
    set_key(ENV_FILE, "SPOTIFY_ACCESS_TOKEN", SPOTIFY_ACCESS_TOKEN, quote_mode="never")
    os.environ["SPOTIFY_ACCESS_TOKEN"] = SPOTIFY_ACCESS_TOKEN
    log.info("Spotify access token refreshed.")


def spotify_authorize() -> None:
    """
    Spotify Authorization Code Flow via copy-paste.
    Set Redirect URI in the Spotify Dashboard to: http://127.0.0.1:8888/callback
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
    print("SPOTIFY - Open this URL and grant access:")
    print("=" * 60)
    print(auth_url)
    print("=" * 60)
    webbrowser.open(auth_url)

    print("\nAfter granting access, Spotify redirects you to a URL that")
    print("cannot be reached. That is expected.")
    print("Copy the FULL URL from your browser and paste it below:\n")
    callback_url = input("Paste the callback URL here: ").strip()

    qs = parse_qs(urlparse(callback_url).query)
    if "error" in qs:
        raise RuntimeError(f"Spotify error: {qs['error']}")
    if "code" not in qs:
        raise RuntimeError("No 'code' found in URL. Did you paste the full URL?")

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
    os.environ["SPOTIFY_ACCESS_TOKEN"] = SPOTIFY_ACCESS_TOKEN
    os.environ["SPOTIFY_REFRESH_TOKEN"] = SPOTIFY_REFRESH_TOKEN
    log.info("Spotify tokens saved.")


# ---------------------------------------------------------------------------
# Tidal OAuth2 – Device Flow via tidalapi (no redirect URI needed)
# ---------------------------------------------------------------------------


def tidal_load_or_login() -> tidalapi.Session:
    """Load a saved Tidal session or start a new Device Flow login."""
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
                log.info("Tidal session loaded from %s.", TIDAL_SESSION_FILE)
                return session
            log.info("Tidal session expired, logging in again ...")
        except Exception as exc:
            log.warning("Could not load Tidal session: %s", exc)
    return tidal_authorize()


def tidal_authorize(session: tidalapi.Session | None = None) -> tidalapi.Session:
    """
    Start a Tidal Device Flow login.
    The user visits a short URL and enters a code — no redirect URI needed.
    """
    if session is None:
        session = tidalapi.Session()

    print("\n" + "=" * 60)
    print("TIDAL - Device Flow login")
    print("=" * 60)

    login, future = session.login_oauth()
    print(f"\nOpen this URL in your browser:\n  {login.verification_uri_complete}")
    print(f"\nOr go to {login.verification_uri} and enter code: {login.user_code}")
    webbrowser.open(login.verification_uri_complete)
    print("\nWaiting for login ...")
    future.result()  # blocks until login is complete

    if not session.check_login():
        raise RuntimeError("Tidal login failed.")

    _tidal_save_session(session)
    log.info("Tidal session saved.")
    return session


def _tidal_save_session(session: tidalapi.Session) -> None:
    """Persist Tidal OAuth tokens to disk as a pickle file."""
    with open(TIDAL_SESSION_FILE, "wb") as f:
        pickle.dump(
            {
                "token_type": session.token_type,
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expiry_time": session.expiry_time,
            },
            f,
        )


# ---------------------------------------------------------------------------
# Track cache – avoids repeated Tidal lookups across runs
# ---------------------------------------------------------------------------


def load_cache() -> dict:
    if os.path.exists(TRACK_CACHE_FILE):
        try:
            with open(TRACK_CACHE_FILE, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: dict) -> None:
    with open(TRACK_CACHE_FILE, "wb") as f:
        pickle.dump(cache, f)


# ---------------------------------------------------------------------------
# Spotify API
# ---------------------------------------------------------------------------


def spotify_check_and_refresh() -> None:
    """Check token validity via /me and refresh if a 401 is returned."""
    try:
        _get(f"{SPOTIFY_BASE}/me", headers=spotify_headers())
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            log.info("Spotify token expired, refreshing ...")
            spotify_refresh_access_token()
        else:
            raise


def spotify_get_current_user_id() -> str:
    data = _get(f"{SPOTIFY_BASE}/me", headers=spotify_headers())
    return data["id"]


def spotify_get_all_playlists() -> list[dict]:
    """Fetch all playlists of the authenticated user, handling pagination."""
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
            "Spotify: %d/%d playlists fetched ...",
            len(playlists),
            data.get("total", "?"),
        )
        if len(items) < limit or not data.get("next"):
            break
        offset += limit
        time.sleep(REQUEST_DELAY)

    return playlists


def spotify_get_tracks_in_playlist(playlist_id: str) -> list[dict]:
    """
    Fetch all tracks in a playlist using the /items endpoint.
    Skips null items (deleted tracks) and non-track items (e.g. podcast episodes).
    """
    tracks: list[dict] = []
    limit = 100
    offset = 0

    while True:
        data = _get(
            f"{SPOTIFY_BASE}/playlists/{playlist_id}/items",
            headers=spotify_headers(),
            params={"limit": limit, "offset": offset},
        )
        items = data.get("items", [])
        for item in items:
            # The /items endpoint uses "item" as the key (not "track")
            # item["item"] can be None for deleted tracks
            track = item.get("item") or item.get("track")
            if track and track.get("id") and track.get("type") == "track":
                tracks.append(track)

        if len(items) < limit or not data.get("next"):
            break
        offset += limit
        time.sleep(REQUEST_DELAY)

    return tracks


# ---------------------------------------------------------------------------
# Tidal search with cache and text-search fallback
# ---------------------------------------------------------------------------


def search_tidal(
    session: tidalapi.Session, isrc: str | None, name: str, artists: str, cache: dict
) -> int | None:
    """
    Search for a track on Tidal:
    1. Check local cache
    2. ISRC lookup (most accurate)
    3. Fallback: text search on track name + artist
    """
    cache_key_isrc = f"isrc:{isrc}" if isrc else None
    cache_key_query = f"q:{name}-{artists}"

    # 1. Cache
    if cache_key_isrc and cache_key_isrc in cache:
        return cache[cache_key_isrc]
    if cache_key_query in cache:
        return cache[cache_key_query]

    # 2. ISRC lookup
    if isrc:
        try:
            results = session.get_tracks_by_isrc(isrc)
            if results:
                tidal_id = results[0].id
                cache[cache_key_isrc] = tidal_id
                return tidal_id
        except Exception:
            pass

    # 3. Text search fallback
    try:
        query = f"{name} {artists}"
        results = session.search(query, models=[tidalapi.media.Track])
        tracks = results.get("tracks", [])
        if tracks:
            tidal_id = tracks[0].id
            cache[cache_key_query] = tidal_id
            return tidal_id
    except Exception:
        pass

    # Not found — cache None to avoid redundant lookups on future runs
    if cache_key_isrc:
        cache[cache_key_isrc] = None
    cache[cache_key_query] = None
    return None


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@dataclass
class MigrationStats:
    found: int = 0
    not_found: int = 0
    not_found_log: list[str] = field(default_factory=list)

    def record_found(self) -> None:
        self.found += 1

    def record_missing(self, label: str) -> None:
        self.not_found += 1
        self.not_found_log.append(label)

    def log_summary(self) -> None:
        log.info("\n" + "=" * 50)
        log.info("Migration complete!")
        log.info("Tracks found    : %d", self.found)
        log.info("Tracks not found: %d", self.not_found)
        if self.not_found_log:
            log.info("\nNot found:")
            for entry in self.not_found_log:
                log.info("  - %s", entry)


def _get_or_create_tidal_playlist(
    tidal_session: tidalapi.Session, name: str, spotify_id: str
) -> tidalapi.UserPlaylist:
    """Return an empty Tidal playlist: clear an existing one or create a new one."""
    existing = [p for p in tidal_session.user.playlists() if p.name == name]
    if existing:
        tidal_pl = existing[0]
        log.info("  Existing Tidal playlist found (id=%s), clearing ...", tidal_pl.id)
        current = tidal_pl.tracks()
        if current:
            tidal_pl.remove_by_indices(list(range(len(current))))
        log.info("  Playlist cleared (%d tracks removed).", len(current))
    else:
        tidal_pl = tidal_session.user.create_playlist(
            name, f"Migrated from Spotify ({spotify_id})"
        )
        log.info("  New Tidal playlist created (id=%s).", tidal_pl.id)
    return tidal_pl


def _resolve_tracks(
    tidal_session: tidalapi.Session,
    tracks: list[dict],
    cache: dict,
    stats: MigrationStats,
) -> list[int]:
    """Look up each Spotify track on Tidal. Returns a deduplicated list of Tidal IDs."""
    tidal_ids: list[int] = []
    for track in tracks:
        name = track.get("name", "Unknown")
        artists = ", ".join(a["name"] for a in track.get("artists", []))
        isrc = track.get("external_ids", {}).get("isrc")

        tidal_id = search_tidal(tidal_session, isrc, name, artists, cache)
        if tidal_id:
            tidal_ids.append(tidal_id)
            stats.record_found()
            log.info("  + %s - %s", name, artists)
        else:
            label = f"Not found: '{name}' - {artists} (ISRC={isrc})"
            log.warning("  x %s", label)
            stats.record_missing(label)

    return list(dict.fromkeys(tidal_ids))  # deduplicate, preserve order


def _push_tracks_to_tidal(
    tidal_pl: tidalapi.UserPlaylist, tidal_ids: list[int]
) -> None:
    """Add tracks to a Tidal playlist in batches of 50.

    Retries on 412 (ETag conflict) by re-fetching the playlist object,
    which forces tidalapi to pick up the latest ETag before retrying.
    """
    for i in range(0, len(tidal_ids), 50):
        chunk = tidal_ids[i : i + 50]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                tidal_pl.add(chunk)
                log.info("  -> Batch %d: %d tracks added.", i // 50 + 1, len(chunk))
                time.sleep(REQUEST_DELAY)
                break
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 412:
                    wait = RETRY_AFTER_DEFAULT * attempt
                    log.warning(
                        "  412 ETag conflict on batch %d (attempt %d/%d). "
                        "Waiting %ds ...",
                        i // 50 + 1,
                        attempt,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    # Re-fetch playlist so tidalapi gets the updated ETag
                    tidal_pl = tidal_pl.session.playlist(tidal_pl.id)
                else:
                    raise
        else:
            log.error(
                "  Batch %d failed after %d attempts, skipping.",
                i // 50 + 1,
                MAX_RETRIES,
            )


def _migrate_playlist(
    playlist: dict,
    idx: int,
    total: int,
    tidal_session: tidalapi.Session,
    cache: dict,
    stats: MigrationStats,
) -> None:
    """Migrate a single Spotify playlist to Tidal."""
    name = playlist.get("name", f"Playlist {idx}")
    pl_id = playlist.get("id")
    owner_id = playlist.get("owner", {}).get("id", "")

    log.info("\n-- Playlist %d/%d: '%s' (owner: %s) --", idx, total, name, owner_id)

    try:
        tracks = spotify_get_tracks_in_playlist(pl_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            log.warning(
                "  Skipped: no access to '%s' (followed playlist of '%s').",
                name,
                owner_id,
            )
        else:
            log.error("  Error fetching tracks of '%s': %s", name, exc)
        return
    except Exception as exc:
        log.error("  Error fetching tracks of '%s': %s", name, exc)
        return

    log.info("  %d tracks found.", len(tracks))
    if not tracks:
        log.info("  Empty playlist, skipping.")
        return

    try:
        tidal_pl = _get_or_create_tidal_playlist(tidal_session, name, pl_id)
    except Exception as exc:
        log.error("  Could not create/clear Tidal playlist: %s", exc)
        return

    tidal_ids = _resolve_tracks(tidal_session, tracks, cache, stats)
    _push_tracks_to_tidal(tidal_pl, tidal_ids)

    log.info("  OK: %d/%d tracks added to '%s'.", len(tidal_ids), len(tracks), name)


def migrate_playlists() -> None:
    """Entry point for migration: sets up sessions, iterates playlists, logs summary."""
    if not os.getenv("SPOTIFY_ACCESS_TOKEN", SPOTIFY_ACCESS_TOKEN):
        log.error("SPOTIFY_ACCESS_TOKEN missing. Run --auth-spotify first.")
        sys.exit(1)

    tidal_session = tidal_load_or_login()
    cache = load_cache()
    stats = MigrationStats()

    spotify_check_and_refresh()
    log.info("Logged in as Spotify user: %s", spotify_get_current_user_id())

    playlists = spotify_get_all_playlists()
    log.info("Found %d playlists on Spotify.", len(playlists))

    for idx, playlist in enumerate(playlists, start=1):
        if not playlist.get("id"):
            continue
        _migrate_playlist(playlist, idx, len(playlists), tidal_session, cache, stats)
        save_cache(cache)
        _tidal_save_session(tidal_session)

    stats.log_summary()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Spotify -> Tidal playlist migration")
    parser.add_argument(
        "--auth", action="store_true", help="Authenticate with both Spotify and Tidal."
    )
    parser.add_argument(
        "--auth-spotify", action="store_true", help="Authenticate with Spotify only."
    )
    parser.add_argument(
        "--auth-tidal", action="store_true", help="Authenticate with Tidal only."
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
