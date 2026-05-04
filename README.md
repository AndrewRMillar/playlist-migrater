# Spotify → Tidal Playlist Migrator

Migreert al jouw Spotify-afspeellijsten naar Tidal via ISRC-matching.

---

## Vereisten

| Vereiste | Versie |
|---|---|
| Python | 3.10 of hoger |
| Ubuntu | 20.04+ (of een andere moderne Linux-distro) |

---

## Stap 1 – Spotify Developer App aanmaken

1. Ga naar <https://developer.spotify.com/dashboard> en log in.
2. Klik op **"Create app"**.
3. Vul in:
   - **App name**: bijv. `Playlist Migrator`
   - **App description**: naar keuze
   - **Redirect URI**: `http://localhost:8888/callback`  
     _(klik op "Add" en sla op)_
4. Selecteer **Web API** als API-gebruik.
5. Klik op **Save**.
6. Open de app en kopieer de **Client ID** en **Client Secret** (via "View client secret").

**Benodigde scopes:**
- `playlist-read-private`
- `playlist-read-collaborative`

> De scopes worden automatisch aangevraagd tijdens de OAuth2-flow.

---

## Stap 2 – Tidal Developer App aanmaken

1. Ga naar <https://developer.tidal.com/> en log in met je Tidal-account.
2. Klik op **"New Application"**.
3. Vul in:
   - **Application Name**: bijv. `Playlist Migrator`
   - **Redirect URI**: `http://localhost:8889/callback`
4. Stel bij **Permissions** (scopes) in:
   - `playlists.read`
   - `playlists.write`
   - `user.read`
5. Klik op **Save** en kopieer de **Client ID** en **Client Secret**.

> **Let op:** De Tidal OpenAPI (`openapi.tidal.com/v2`) is beschikbaar via  
> het **"TIDAL API"**-programma. Zorg dat je applicatie hiervoor is  
> goedgekeurd. Bij twijfel: start een verzoek via het developer-portaal.

---

## Stap 3 – Installatie op Ubuntu

```bash
# 1. Kloon of kopieer de projectmap
cd ~/playlist-migrator

# 2. Maak een virtual environment aan
python3 -m venv .venv
source .venv/bin/activate

# 3. Installeer dependencies
pip install -r requirements.txt

# 4. Maak .env aan op basis van het voorbeeld
cp .env.example .env
```

Open `.env` in een editor en vul je **Client ID's** en **Client Secrets** in voor
zowel Spotify als Tidal. Laat de token-velden voorlopig leeg.

---

## Stap 4 – Authenticatie (eenmalig)

```bash
python sync_playlists.py --auth
```

Het script:
1. Opent de Spotify-autorisatiepagina in je browser.
2. Nadat je akkoord gaat, vangt het lokaal de callback op poort **8888** op.
3. Herhaalt dit voor Tidal op poort **8889**.
4. Slaat alle tokens op in `.env`.

> **Werkt de browser niet automatisch?** Kopieer de URL die in de terminal
> verschijnt en plak die handmatig in je browser.

Je kunt ook afzonderlijk authenticeren:

```bash
python sync_playlists.py --auth-spotify   # alleen Spotify
python sync_playlists.py --auth-tidal     # alleen Tidal
```

---

## Stap 5 – Migratie starten

```bash
python sync_playlists.py
```

De voortgang wordt live getoond én weggeschreven naar `sync_playlists.log`.

### Voorbeeld output

```
2025-01-15 14:32:01 [INFO] === Stap 1: Spotify-playlists ophalen ===
2025-01-15 14:32:02 [INFO] Totaal 12 playlists gevonden op Spotify.
2025-01-15 14:32:02 [INFO] ── Playlist 1/12: 'Mijn favorieten' ──
2025-01-15 14:32:02 [INFO] Tidal-playlist aangemaakt: 'Mijn favorieten' (id=abc123)
2025-01-15 14:32:03 [INFO]   42 tracks gevonden in 'Mijn favorieten'.
2025-01-15 14:32:15 [WARNING]   ✗  Niet gevonden op Tidal: 'Rare Track' – Artiest (ISRC=USRC12345678)
2025-01-15 14:32:16 [INFO]   ✓  41 tracks toegevoegd aan 'Mijn favorieten'.
...
2025-01-15 14:45:00 [INFO] === Migratie voltooid ===
2025-01-15 14:45:00 [INFO] Tracks gevonden en toegevoegd : 387
2025-01-15 14:45:00 [INFO] Tracks NIET gevonden op Tidal : 23
```

---

## Veelgestelde vragen

**Wat gebeurt er met tracks die niet op Tidal staan?**  
Ze worden gelogd in de terminal en in `sync_playlists.log`. De playlist
wordt gewoon aangemaakt met de tracks die wél gevonden zijn.

**Kan ik het script opnieuw draaien?**  
Ja, maar het script controleert niet op duplicaten — er worden dan nieuwe
playlists aangemaakt naast de bestaande. Verwijder de Tidal-playlists
handmatig vóór een herstart als je dat wilt vermijden.

**Mijn token is verlopen.**  
Het script vernieuwt het access-token automatisch met het refresh-token.
Als ook dat verlopen is, voer dan opnieuw `--auth` uit.

**Ik krijg een 403 van Tidal.**  
Controleer of de scopes `playlists.write` en `user.read` correct zijn
ingesteld in je Tidal Developer App. Een nieuwe `--auth-tidal` is daarna nodig.

---

## Projectstructuur

```
playlist-migrator/
├── sync_playlists.py   # Hoofdscript
├── requirements.txt    # Python-afhankelijkheden
├── .env.example        # Voorbeeld omgevingsbestand
├── .env                # Jouw credentials (nooit committen!)
└── sync_playlists.log  # Automatisch aangemaakt bij uitvoer
```

---

## Licentie

MIT – vrij te gebruiken en aan te passen.
