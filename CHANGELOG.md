# Changelog

## 0.3.4 — 2026-10-04

- **Comics you're reading in Komga show up in Continue.** Omnarr now reads Komga's reading
  position (for the account whose API key Omnarr uses), so comics count as in progress or
  finished like books, audiobooks and shows.

## 0.3.3 — 2026-10-04

- **Adult comics and books go in the private section.** Komga books whose series is rated
  18+ (ComicInfo "Adults Only 18+") and Calibre books tagged NSFW, XXX or 18+ now only show
  in the PIN-locked private section. The Calibre tags can be changed (or turned off) in the
  Calibre connection.
- Notifications can never interrupt the request or upload that triggered them.

## 0.3.2 — 2026-10-04

- **Request approvals.** A new *can ask* permission sits between guest and family. Those
  people's requests (movies, shows, books, comics, games) wait in **Activity → Requests**
  until an admin approves or declines them, and they can withdraw their own. Invitations have
  a *Friend (asks first)* preset.
- **Notifications (optional).**
  - A webhook connection sends events to ntfy, a Discord channel, a Home Assistant webhook,
    or anything that takes JSON. You can limit which events it sends.
  - People can add an email address and get "your request is ready / was approved / was
    declined" emails, which needs the Email connection.
  - Admins are emailed when something needs approval.
  - Events: request ready, approval needed, request decided, someone joined, upload received.
- **Add to Home Screen.** Omnarr has an app icon and opens full screen when saved to a phone's
  home screen (iPhone and Android).
- Requested comics: Komga is asked to rescan again 10 minutes after the download, in case
  the file arrived late.
- Security headers are on every response, including sign-in errors.

## 0.3.1 — 2026-10-04

- **One Omnarr account is enough.** Playback position, finished items and "Continue" are now
  saved by Omnarr itself for everyone. Nobody needs their own Jellyfin or Audiobookshelf
  account. Linking one (Settings → My account) is optional and also keeps it in sync there.
  Watched episodes and movies on a show's page come from the same record.
- **Ready for a public address** (e.g. Tailscale Funnel or a reverse proxy):
  - secure cookies over HTTPS;
  - security headers, and "don't index" for search engines;
  - a *Public address* setting that invitation links always use;
  - docs for `FORWARDED_ALLOW_IPS`, so the sign-in lockout counts each visitor separately.

## 0.3.0 — 2026-10-03

**Accounts, sharing and files.** One release covering what was planned as 0.3 and 0.4.

- **Multiple accounts.** Each person signs in with their own username and password.
  Upgrading is seamless: your existing password becomes the `admin` account, and every
  signed-in browser stays signed in.
- **Roles and permissions.** Admins manage connections, users and the library. Members get
  switches: *can request downloads*, *can save files to their device*, *can upload*, and
  *private section*. A guest (everything off) can browse and play but can't make the server
  download anything.
- **Invitations.** Create a single-use sign-up link that carries the permissions it grants.
  Copy it and text it, or have Omnarr email it (new optional *Email* connection: any SMTP
  server, Gmail with an app password).
- **Per-person progress.** "Continue", In progress and Finished are each person's own, read
  from their own Jellyfin user and Audiobookshelf API key (Settings → My account). Members
  who haven't linked theirs see no progress, never the owner's.
- **Playback writes to your own accounts.** Without a linked account, playback still works
  but nothing is saved, and the player says so.
- **Private section per person.** Each person has their own PIN, and admins choose who can use it.
- **Save to device.** Download the original file of anything you can see: an ebook (any
  Calibre format), a read-along EPUB, an audiobook, a comic, a movie or a game.
- **Uploads.** People with permission can upload ebooks, comics and audiobooks to drop-off
  folders an admin chooses (e.g. the Calibre-Web-Automated ingest and an Audiobookshelf
  library). There are type and size limits, and every upload is logged.
- **Home Assistant:** each HA user gets their own Omnarr account automatically. The first one
  becomes admin.
- The Activity log records who did what: requests, downloads, uploads and account changes.

## 0.2.5 — 2026-10-03

- **Requested comics go to Komga.** The comic format now only accepts comic archives
  (cbz/cbr/cb7), which a small server setup routes to Komga (see "Requested comics → Komga"
  in the README). When Shelfmark finishes a comic, Omnarr asks Komga to rescan, so it
  shows up now rather than at Komga's next scheduled scan.

## 0.2.4 — 2026-10-03

- **Request related books and comics.** Books and comics in a work's related list now have a
  Request button, like movies and shows. Books ask ebook or audiobook; comics request in one
  click. Omnarr then keeps looking (Shelfmark) until a good copy arrives, retries failed
  downloads with the next-best copy, and searches again every few days. "Choose a copy myself"
  is there for picking by hand.
- New **comic** format for the keep-looking list. It's searched as an ebook and accepts
  cbz/cbr/cb7/epub/pdf, and it counts as arrived once it shows up in Komga or Calibre.
- Fixed: cancelling the copy picker could throw an error.

## 0.2.3 — 2026-10-03

- Related shows and movies with a TMDB id match your library only by that id. The 2010
  "The Last Airbender" film no longer shows as owned because you have the animated series.
- Book lookups also try the subtitle, so "Avatar, the Last Airbender: The Reckoning of Roku"
  is found as "The Reckoning of Roku".

## 0.2.2 — 2026-10-03

- **Related works for everything, not just books.** A show, movie, book or comic page now lists
  the rest of its world: the same media franchise or series, works based on it, and what it was
  based on. Results are grouped under "On screen & in games" and "Books & comics". Shows and
  movies are looked up by their exact TMDB/TVDB/IMDb ids. For Avatar: The Last Airbender, that
  finds The Legend of Korra, the 2010 film, the 2024 live-action series, the 2026 film, the
  games, the Kyoshi and Yangchen novels and the Dark Horse comics.
- Items you already own open directly. Shows and movies can be requested (Seerr), and so can
  games (ROMarr).
- Fixed: book lookups missed authors whose Wikidata name is stored as a language-neutral
  ("mul") label, such as J. K. Rowling, so their books found no adaptations.
- Filters out franchise noise: characters, seasons, soundtracks, parodies and series/collection entries.

## 0.2.1 — 2026-10-03

- **Starter lists.** About 70 well-known book→screen adaptations (Silo, Dune, Harry Potter,
  The Hunger Games, The Expanse, Game of Thrones…) and 8 shared universes (Middle-earth, the
  Cosmere, Asimov's Foundation universe, Dune, the Enderverse, Realm of the Elderlings, Riftwar,
  Hainish) work out of the box. Your own `config.yml` lists are added on top, and
  `starter_lists: false` turns the built-in ones off.
- Universes order their series as listed, then by number, so a world's series no longer interleave.
- Housekeeping: modern FastAPI startup (lifespan), and current GitHub Actions versions.
- The Home Assistant add-on has been tested on a real Home Assistant OS install.

## 0.2.0 — 2026-10-03

- **Home Assistant add-on** ([omnarr-ha](https://github.com/tamengual/omnarr-ha)). Omnarr opens
  from the sidebar, and you're signed in through Home Assistant. That sign-in is trusted only
  for requests from HA's ingress proxy, and only when running as the add-on.
- **Works under any URL path.** The UI uses relative links. For reverse proxies, set `OMNARR_BASE_PATH`.
- **Settings → Password** sets or changes the sign-in password, and changing it signs out other browsers.
  In the add-on, the direct-access password can only be set from inside Home Assistant.
- **Komga:** each book is now its own entry (grouped by series) with its own cover, instead of one entry per series.
- **Cleaner titles.** Nested series names in brackets are stripped, e.g. "The Sworn Sword (A Game of Thrones) (The Hedge Knight…)".
- **No stale pages after upgrades.** Browsers now re-check the UI files on each load.

## 0.1.0 — 2026-10-03

The first public release.

- One search index across Calibre, Audiobookshelf, Storyteller, BookBridge, Komga,
  Jellyfin, Sonarr, Radarr, RomM and Stash.
- Copies of a work are grouped together: ebook, audiobook and read-along form one book,
  with linked screen adaptations, series and shared universes.
- Live details and actions: episodes, downloads, search and monitor, all recorded in an audit log.
- Requests through Seerr, Shelfmark and ROMarr. Wanted books are re-searched until found,
  and "Search harder" goes through Prowlarr.
- Video plays in the browser through Jellyfin, and audiobooks through Audiobookshelf.
  Progress is written back to the owning app.
- An optional private section, PIN-locked and off by default.
- A first-run setup screen. App connections are stored in the database, and each one can be tested.
  Audiobookshelf, Komga and Stash connect over their APIs.
