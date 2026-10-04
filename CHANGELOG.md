# Changelog

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
