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
- **Everything related, in one place.** A show, movie, book or comic page lists the rest of
  its world from Wikidata: the same franchise, adaptations, and what it was based on.
  Open *Avatar: The Last Airbender* and you get Korra, the films, the live-action series,
  the novels, the comics and the games.
- **Request from anything:** missing episodes and movies (Seerr → Sonarr/Radarr), books,
  audiobooks and comics (Shelfmark), and games (ROMarr). That includes anything in a
  related list.
- **Keep looking.** Wanted books are re-searched on a schedule until an acceptable copy turns up.
  "Search harder" runs a free-text Prowlarr search and pushes the release to Sonarr or Radarr.
- **Play in the browser.** Video plays from Jellyfin (direct or transcoded HLS), and audiobooks
  from Audiobookshelf with chapters. Progress is saved back to the owning app.
  App credentials never reach the browser.
- **Share it.** Give family and friends their own accounts with exactly the permissions you
  choose: request things, save files to their own device, upload, and the private section.
  Invite them with a link you text or email. Everyone's progress is their own.
- **Save and upload.** Download the original ebook, read-along, audiobook, comic, movie or
  game. People you allow can upload their own books and audiobooks to drop-off folders you choose.
- **An optional private section** (Stash, adult Jellyfin libraries), off by default and PIN-locked, with a PIN per person.
- **Read-only by design.** Omnarr never writes to another app's files. Changes go through
  each app's own API, and every change is logged on the Activity page. The only exception
  is the upload folders, and only if you set them.

## How it fits in

Omnarr is the front end. Your existing apps keep storing, downloading and organising, and
Omnarr reads from them, plays through them, and sends requests to them.

For a fuller example (read-alongs synced across a Kobo and phone apps), see
[docs/example-setup.md](docs/example-setup.md).

![Diagram: you use Omnarr; Omnarr reads and plays from your library apps (Jellyfin, Sonarr/Radarr, Calibre, Audiobookshelf, Storyteller, Komga, RomM) and sends requests through request apps, indexers and a download client, which deliver into those libraries](docs/images/architecture.svg)

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

1. Create the admin account (a username and password).
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

### How requested books arrive (and comics → Komga)

When you request a book, audiobook or comic, Omnarr keeps looking through Shelfmark until a
good copy turns up. Shelfmark saves ebooks and comics to one folder (usually your
Calibre-Web-Automated ingest folder) and audiobooks to another:

![Diagram: Omnarr → Shelfmark → download client; ebooks and comics land in the ingest folder (ebooks ingested into Calibre, comic archives moved to Komga), audiobooks land in their own folder that Audiobookshelf scans](docs/images/book-requests.svg)

To have requested comics land in Komga instead of Calibre:

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

## Users, sharing and permissions

The first account (created on first run) is an **admin**. Admins add people in
**Settings → Users**, or send an **invitation link** (copy it, or let Omnarr email it once
you've connected an *Email* server in Connections).

| Switch | What it allows |
|---|---|
| Can request downloads | Requests and searches that make your server download things (Seerr, books, games, Sonarr/Radarr actions) |
| Can save files to their device | Downloading the original ebook, audiobook, comic, movie or game |
| Can upload files | Uploading their own books and audiobooks to your drop-off folders |
| Private section | Using the PIN-locked private section (each person sets their own PIN) |

With every switch off, a person is a **guest**: they can browse and play, and that's all.

**Their own progress:** each person links their own Jellyfin user and Audiobookshelf API key
in **Settings → My account**. Playback progress is then saved to *their* accounts, and their
"Continue" row is theirs. Until they link them, they can still play, but nothing is saved.

**Uploads** need writable folders. Mount them into the container and set them in Settings:

```yaml
    volumes:
      - /path/to/cwa-book-ingest:/uploads/books          # ebooks and comics
      - /path/to/audiobookshelf/library:/uploads/audiobooks
```

**Sharing outside your home network:** Omnarr has no built-in remote access. Put it behind
something that does: a VPN such as Tailscale (sharing just Omnarr with someone's own
Tailscale account is the simplest safe option), or a reverse proxy with HTTPS.

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
