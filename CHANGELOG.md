# Changelog

## 0.1.0 — unreleased

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
