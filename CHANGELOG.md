# Changelog

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
