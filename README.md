# Spotify to Tidal Playlist Migrator

Migrates all your Spotify playlists to Tidal using ISRC-based track matching, with a text-search fallback for tracks without an ISRC.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.10 or higher |
| Ubuntu | 20.04+ (or any modern Linux distro) |

---

## Step 1 – Create a Spotify Developer App

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. Click **"Create app"**.
3. Fill in:
   - **App name**: e.g. `Playlist Migrator`
   - **Redirect URI**: `http://127.0.0.1:8888/callback` — click **Add**, then **Save**
4. Select **Web API** as the API type.
5. Open the app settings and copy the **Client ID** and **Client Secret**.

**Required scopes** (requested automatically during auth):
- `playlist-read-private`
- `playlist-read-collaborative`

---

## Step 2 – Create a Tidal Developer App

1. Go to <https://developer.tidal.com/> and log in with your Tidal account.
2. Click **"New Application"**.
3. Fill in:
   - **Application Name**: e.g. `Playlist Migrator`
   - **Redirect URI**: `http://127.0.0.1:8889/callback`
4. Enable these **scopes**:
   - `playlists.read`
   - `playlists.write`
   - `user.read`
5. Save and copy the **Client ID** and **Client Secret**.

> **Note:** Tidal authentication uses the **Device Flow** — no redirect URI handling is needed at runtime. The script opens `link.tidal.com` in your browser where you log in directly.

---

## Step 3 – Installation

```bash
# Clone or copy the project folder
cd ~/playlist-migrator

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env from the example
cp .env.example .env
```

Open `.env` and fill in your **Client ID** and **Client Secret** for both Spotify and Tidal. Leave the token fields empty for now.

---

## Step 4 – Authentication (one-time)

**Spotify:**
```bash
python sync_playlists.py --auth-spotify
```
The script opens the Spotify authorization page. After granting access, Spotify redirects you to a URL that cannot be reached — copy the full URL from your browser address bar and paste it into the terminal.

**Tidal:**
```bash
python sync_playlists.py --auth-tidal
```
The script opens `link.tidal.com` in your browser. Log in with your Tidal account. The script waits automatically and continues once login is complete.

**Or authenticate with both at once:**
```bash
python sync_playlists.py --auth
```

Tokens are saved locally (`tidal_session.pkl` and `.env`) and reused on subsequent runs.

---

## Step 5 – Run the migration

```bash
python sync_playlists.py
```

Progress is shown in the terminal and written to `sync_playlists.log`.

### Example output

```
2026-05-06 12:03:01,000 [INFO] Tidal session loaded from tidal_session.pkl.
2026-05-06 12:03:01,500 [INFO] Logged in as Spotify user: 1139266769
2026-05-06 12:03:02,000 [INFO] Spotify: 50/55 playlists fetched ...
2026-05-06 12:03:02,500 [INFO] Spotify: 55/55 playlists fetched ...
2026-05-06 12:03:02,500 [INFO] Found 55 playlists on Spotify.

-- Playlist 1/55: 'Radiohead' (owner: 1139266769) --
2026-05-06 12:03:03,000 [INFO]   8 tracks found.
2026-05-06 12:03:03,100 [INFO]   New Tidal playlist created (id=abc123).
2026-05-06 12:03:03,200 [INFO]   + Creep - Radiohead
2026-05-06 12:03:03,400 [WARNING]   x Not found: 'Rare B-side' - Radiohead (ISRC=GBUM71234567)
2026-05-06 12:03:04,000 [INFO]   -> Batch 1: 7 tracks added.
2026-05-06 12:03:04,000 [INFO]   OK: 7/8 tracks added to 'Radiohead'.
...
==================================================
2026-05-06 12:45:00,000 [INFO] Migration complete!
2026-05-06 12:45:00,000 [INFO] Tracks found    : 412
2026-05-06 12:45:00,000 [INFO] Tracks not found: 18
```

---

## Behaviour

**Existing Tidal playlists**
If a Tidal playlist with the same name already exists, it is cleared and repopulated. Running the script multiple times will not create duplicate playlists.

**Followed playlists**
Playlists you follow from other Spotify users are skipped with a warning — Spotify's API does not allow fetching tracks from playlists you do not own.

**Track matching**
Tracks are matched in order of reliability:
1. **Cache** — tracks already looked up in a previous run are reused instantly
2. **ISRC** — the International Standard Recording Code gives an exact match
3. **Text search** — fallback using track name + artist name if no ISRC is available

Tracks not found on Tidal are logged to both the terminal and `sync_playlists.log`.

**Re-running the script**
Safe to re-run at any time. The local cache (`track_cache.pkl`) ensures already-matched tracks are not looked up again, which speeds up subsequent runs significantly.

---

## Project structure

```
playlist-migrator/
├── sync_playlists.py     # Main script
├── requirements.txt      # Python dependencies
├── .env.example          # Example environment file
├── .env                  # Your credentials (never commit this)
├── tidal_session.pkl     # Saved Tidal session (auto-generated)
├── track_cache.pkl       # Track lookup cache (auto-generated)
└── sync_playlists.log    # Migration log (auto-generated)
```

---

## Troubleshooting

**Spotify 401 Unauthorized**
Your access token has expired. Run `--auth-spotify` again to get a fresh token.

**Spotify 403 Forbidden on a playlist**
The playlist is owned by another user (a followed playlist). These are skipped automatically.

**Tidal login prompt on every run**
The `tidal_session.pkl` file is missing or corrupted. Run `--auth-tidal` to create a fresh session.

**Many tracks not found on Tidal**
Some tracks may not be available in your country (`TIDAL_COUNTRY_CODE` in `.env`). Try changing the country code, or check if the tracks exist on Tidal manually.

---

## License

MIT — free to use and modify.
