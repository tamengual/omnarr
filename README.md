# Omnarr

**One place to search, browse, request and play everything on your media server.**

Omnarr sits in front of the self-hosted apps you already run (Calibre, Audiobookshelf,
Storyteller, Jellyfin, Sonarr, Radarr, Komga, RomM and others). It builds one search
index across all of them, groups the copies of a work together, and lets you act on
anything from one screen. Your apps stay in charge of storage, downloads and metadata.
Omnarr is the front end.

![Omnarr's home page: search, filters, and Continue for books, audiobooks and TV in progress](docs/images/start.webp)

> Status: early (v0.2). It runs every day on one home server. Expect rough edges, and
> please file issues.

## What it does

![Library shelves: shows and movies on their way; read-alongs with audio, ebook and read-along formats grouped as one book](docs/images/home.webp)

- **One search across every app**, with facets (kind, format, genre, year, "have it" or "missing").
- **Works, not files.** The ebook (Calibre), audiobook (Audiobookshelf) and read-along
  (Storyteller) of one book appear as **one Book**. Screen adaptations show as separate,
  linked entries. Series and shared universes are shown in reading order.

  <img src="docs/images/book.webp" width="620" alt="The Hobbit's page: the rest of The Lord of the Rings in order, then every screen and game adaptation, each with a Request button">

- **Live details** from the owning app: which episodes you have or are missing, download
  progress, listening and reading positions.
- **Request from anything:** missing episodes and movies (Sonarr/Radarr/Seerr), the
  audiobook of an ebook you own (Shelfmark), the screen adaptation of a book, and games (ROMarr).
- **Keep looking.** Wanted books are re-searched on a schedule until an acceptable copy turns up.
  "Search harder" runs a free-text Prowlarr search and pushes the release to Sonarr or Radarr.
- **Play in the browser.** Video plays from Jellyfin (direct or transcoded HLS), and audiobooks
  from Audiobookshelf with chapters. Progress is saved back to the owning app.
  App credentials never reach the browser.
- **An optional private section** (Stash, adult Jellyfin libraries), off by default and PIN-locked when on.
- **Read-only by design.** Omnarr never writes to another app's files. Changes go through
  each app's own API, and every change is logged on the Activity page.

## Supported apps

Every integration is optional. Connect the ones you use.

| App | What Omnarr uses it for | How it connects |
|---|---|---|
| Calibre (+ Calibre-Web) | Ebooks | `metadata.db`, mounted read-only |
| Audiobookshelf | Audiobooks, progress, playback | API key |
| Storyteller | Read-along books | `storyteller.db`, mounted read-only |
| BookBridge | Cross-app reading positions, format links | `database.db`, mounted read-only |
| Komga | Comics and manga | API key |
| Jellyfin | Movies, shows, watched state, playback | API key |
| Sonarr / Radarr | Episodes and movies, downloads, search | API key |
| Seerr / Jellyseerr / Overseerr | Movie and TV requests | API key |
| Prowlarr | "Search harder" | API key |
| Shelfmark | Book and audiobook requests | API key |
| RomM | Games | Client API token |
| ROMarr | Game requests | API key |
| Stash | Private section | API key |

## Install (Docker)

```yaml
# compose.yml
services:
  omnarr:
    image: ghcr.io/tamengual/omnarr:latest
    container_name: omnarr
    restart: unless-stopped
    ports: ["8765:8765"]
    environment: [PUID=1000, PGID=1000, TZ=Etc/UTC]
    volumes:
      - ./data:/data
      # Only for apps that connect through a database file (see the table above):
      # - /path/to/calibre-library:/src/calibre:ro
```

```bash
docker compose up -d
```

Open `http://your-server:8765`, then:

1. Choose a password.
2. **Settings → Connections:** for each app you use, enter its address and API key, then press **Test**.
3. Omnarr builds its index (about a minute) and rebuilds it every 15 minutes.

`compose.example.yml` has the full list of optional mounts. `config.example.yml` covers
the optional extras: book-to-screen adaptations, shared universes and series name aliases.

### Addresses

The address you enter in Connections is how **Omnarr's container** reaches the app.
If the apps share a Docker network, that's `http://sonarr:8989`; otherwise it's
`http://<server-ip>:8989`. If the browser needs a different address (for example a
reverse proxy), put it in the optional *Open-in-browser address* field so the "Open in…"
links work.

### Behind a reverse proxy at a sub-path

To serve Omnarr at something like `https://example.com/omnarr/`, set `OMNARR_BASE_PATH=/omnarr`
and have the proxy strip that prefix before forwarding. The UI uses relative links, so
nothing else needs changing.

### Requested comics → Komga

Shelfmark saves every download to one folder (usually your Calibre-Web-Automated ingest
folder). To have requested comics land in Komga instead:

1. In CWA → Settings → CWA Settings, add `cbz, cbr, cb7, cbt` to **formats to ignore during
   ingest**, so CWA leaves comic archives where they are.
2. Move those files into Komga's library folder on a schedule. For example, a cron entry
   every 5 minutes:
   `find /path/to/ingest -type f \( -iname '*.cbz' -o -iname '*.cbr' -o -iname '*.cb7' \) -mmin +2 -exec mv -n {} /path/to/komga/comics/ \;`
3. Connect Komga in Omnarr with an API key. Omnarr asks Komga to rescan as soon as a requested
   comic finishes downloading.

## Install (Home Assistant add-on)

On Home Assistant OS or Supervised, add the add-on repository
`https://github.com/tamengual/omnarr-ha` under **Settings → Add-ons → Add-on store → ⋮ →
Repositories**, then install **Omnarr**. It opens from the sidebar, and you're signed in
through Home Assistant. See the [add-on docs](https://github.com/tamengual/omnarr-ha/blob/main/omnarr/DOCS.md).

### Security notes

- Put Omnarr behind your reverse proxy or VPN. Don't expose it to the internet directly.
- `/data/state.db` holds your app API keys. Keep `/data` private and include it in your backups.
- Media streams and cover images are proxied through Omnarr, so API keys never reach the browser.

## Development

```bash
pip install -r requirements.txt pytest
OMNARR_CONFIG=./config.yml uvicorn app.main:app --reload --port 8765
pytest
```

There's no frontend build step: the UI is plain HTML, CSS and JavaScript in `app/static`.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE). Bundled third-party code is listed in [NOTICE](NOTICE).
