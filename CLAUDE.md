# Omnarr — contributor notes

A FastAPI app plus a no-build vanilla-JS front end that fronts self-hosted media apps.

## Layout
- `app/main.py`: HTTP API, auth (one password; sessions in state.db), the PIN-gated private section,
  cover proxy and thumbnails, playback proxy, and the setup endpoints.
- `app/config.py`: optional bootstrap `config.yml`, plus the `connections` table (one row per app) in state.db.
- `app/connectors/`: one module per app, each yielding `Unit`s. `registry.py` describes every app
  for the setup screen (fields plus a `test()` function).
- `app/indexer.py`: builds the SQLite FTS5 index and groups units into works (union-find).
- `app/search.py`: queries and facets. `app/live.py`: live details and audited actions.
- `app/requests_.py`, `wanted.py`, `harder.py`, `play.py`: requests, keep-looking,
  Prowlarr search, and playback.
- `app/static/`: `index.html`, `app.js`, `style.css`, `player.js`, plus the vendored hls.js.

## Rules
- Never write to another app's files. Database-mode sources are opened read-only (`ro_connect`),
  and every change goes through the owning app's API and is logged with `live._audit`.
- Every integration is optional: check `cfg.source(name)` before using it.
- Secrets never reach the browser. Mask them in API responses, and proxy streams and covers server-side.
- Private (adult) units must stay hidden unless `_adult_ok(session)` is true, including in cached covers.
- Tests: `pytest`. JS syntax: `node --check app/static/*.js`.
