"""Bring your own apps: custom libraries, custom request webhooks and Python plug-ins.

Three levels, from no code to full control (docs/extending.md has the details):

1. Custom library (no code): Omnarr reads a URL that returns items in a small JSON format
   ({"items": [ITEM, ...]}). Anything that can produce JSON (a script, n8n, an app's own API
   behind a tiny adapter) can add its library to Omnarr.
2. Custom requests (no code): for the formats you pick, a request is sent to your URL as JSON
   instead of to Seerr/Shelfmark/ROMarr.
3. Plug-ins (Python): a file in the plug-ins folder that declares its settings and provides
   read(settings) -> [ITEM, ...] and/or request(settings, item) -> {...}. It shows up in
   Connections like a built-in app. Plug-ins run with Omnarr's own permissions: only install
   ones you trust (an admin has to put the file on the server).

ITEM: {"id" (required), "kind": book|movie|show|comic|game, "title" (required), "format"
(ebook|audiobook|readalong|movie|series|comic|game; defaults from kind), "authors": [...],
"series", "series_index", "year", "description", "genres": [...], "url" (where "Open" goes),
"cover_url", "duration" (seconds), "added" (ISO date), "progress" (0..1), "finished",
"rating", "adult", "ids": {"isbn", "asin", "tmdb", "tvdb", "imdb", ...}}
"""
import importlib.util
import logging
import os

import httpx

from .connectors.base import Unit, iso_time, year_of

log = logging.getLogger("omnarr.extend")
KINDS = {"book": "ebook", "movie": "movie", "show": "series", "comic": "comic", "game": "game"}
FORMATS = {"ebook", "audiobook", "readalong", "movie", "series", "comic", "game"}
# what a request can be for (what the request buttons send)
REQUEST_FORMATS = ("movie", "tv", "ebook", "audiobook", "comic", "game")
PLUGINS = {}                                      # source key -> {"module", "spec"}


# ── items ────────────────────────────────────────────────────────────────────
def unit_from_dict(source, d):
    """A Unit from the documented ITEM shape; None for anything unusable."""
    if not isinstance(d, dict) or not d.get("id") or not str(d.get("title") or "").strip():
        return None
    kind = d.get("kind") if d.get("kind") in KINDS else "book"
    fmt = d.get("format") if d.get("format") in FORMATS else KINDS[kind]
    lst = lambda v: [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])
    num = lambda v: float(v) if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit()) else None
    ids = {k: str(v) for k, v in (d.get("ids") or {}).items() if v} if isinstance(d.get("ids"), dict) else {}
    progress = num(d.get("progress"))
    return Unit(
        source=source, source_id=str(d["id"]), kind=kind, format=fmt, title=str(d["title"]).strip(),
        authors=lst(d.get("authors")), narrators=lst(d.get("narrators")), series=str(d.get("series") or ""),
        series_index=num(d.get("series_index")), year=year_of(str(d.get("year") or "")), description=str(d.get("description") or ""),
        genres=lst(d.get("genres")), url=str(d.get("url") or ""), cover=f"{source}:{d['id']}" if d.get("cover_url") else "",
        duration=num(d.get("duration")), added=str(d.get("added") or "")[:10],
        progress=min(1.0, max(0.0, progress)) if progress is not None else None, finished=bool(d.get("finished")),
        adult=bool(d.get("adult")), rating=num(d.get("rating")), library=str(d.get("library") or ""),
        ids=ids, extra={"cover_url": d.get("cover_url") or "", "last_listened": iso_time(d.get("last_activity"))},
    )


def _headers(s):
    name, value = (s.get("header_name") or "").strip(), s.get("header_value") or ""
    return {name: value} if name and value else {}


# ── 1. custom library ────────────────────────────────────────────────────────
def custom_items(s):
    r = httpx.get(s["url"], headers=_headers(s), timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("items", []) if isinstance(data, dict) else data if isinstance(data, list) else []


def read_custom(cfg):
    s = cfg.source("custom_library")
    if not s or not s.get("url"):
        return []
    return [u for u in (unit_from_dict("custom", d) for d in custom_items(s)) if u]


def test_custom(s):
    try:
        items = custom_items(s)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    good = [d for d in items if unit_from_dict("custom", d)]
    if items and not good:
        return False, f"{len(items)} items, but none have an id and a title"
    return True, f"Connected: {len(good)} items" + (f" ({len(items) - len(good)} skipped)" if len(good) < len(items) else "")


def cover_bytes(cfg, source, url):
    """Cover for a custom/plug-in item (fetched server-side, with the source's header)."""
    if not url:
        return None
    if source == "custom":
        headers = _headers(cfg.source("custom_library") or {})
    else:
        p = PLUGINS.get(source)
        if p and hasattr(p["module"], "cover"):
            return p["module"].cover(cfg.source(source) or {}, url)
        headers = {}
    r = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
    return r.content if r.status_code == 200 else None


# ── 2. custom requests (webhook) ─────────────────────────────────────────────
def webhook_formats(cfg):
    s = cfg.source("custom_requests")
    if not s or not s.get("url"):
        return set()
    return {f for f in (s.get("formats") or []) if f in REQUEST_FORMATS}


def send_webhook(cfg, item):
    s = cfg.source("custom_requests")
    r = httpx.post(s["url"], json={"event": "request", **item}, headers=_headers(s), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"your request URL answered HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    return {"ok": True, "message": (body or {}).get("message") or "Sent", "external_id": (body or {}).get("id")}


def test_webhook(s):
    if not s.get("formats"):
        return False, "Pick at least one format to send here"
    try:
        r = httpx.post(s["url"], json={"event": "test", "message": "Omnarr test request"}, headers=_headers(s), timeout=15)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return (r.status_code < 400, f"Your URL answered HTTP {r.status_code}")


# ── 3. plug-ins ──────────────────────────────────────────────────────────────
def plugin_dir():
    return os.environ.get("OMNARR_PLUGINS") or os.path.join(os.path.dirname(os.environ.get("OMNARR_STATE", "/data/state.db")), "plugins")


def load_plugins(folder=None):
    """Import every *.py in the plug-ins folder. A broken plug-in is logged and skipped; it never
    stops Omnarr. Returns {source key: {"module", "spec"}}."""
    from .connectors import registry
    folder = folder or plugin_dir()
    PLUGINS.clear()
    registry.APPS[:] = [a for a in registry.APPS if not a.get("plugin")]
    registry.BY_KEY.clear()
    registry.BY_KEY.update({a["key"]: a for a in registry.APPS})
    if not os.path.isdir(folder):
        return PLUGINS
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        path = os.path.join(folder, name)
        try:
            spec_ = importlib.util.spec_from_file_location(f"omnarr_plugin_{name[:-3]}", path)
            mod = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(mod)
            meta = dict(getattr(mod, "PLUGIN"))
            key = "plugin_" + "".join(c for c in str(meta["key"]).lower() if c.isalnum() or c == "_")
            if key in registry.BY_KEY:
                raise ValueError(f"key {meta['key']!r} is already used")
            requests_for = [f for f in meta.get("requests", []) if f in REQUEST_FORMATS] if hasattr(mod, "request") else []
            app = {"key": key, "label": f"{meta.get('label') or meta['key']} (plug-in)", "category": meta.get("category") or "Other",
                   "about": meta.get("about") or f"Plug-in from {name}.", "fields": list(meta.get("fields") or []),
                   "plugin": True, "requests": requests_for,
                   "test": (lambda s, m=mod: m.test(s)) if hasattr(mod, "test") else (lambda s: (True, "Saved (this plug-in has no test)"))}
            registry.APPS.append(app)
            registry.BY_KEY[key] = app
            PLUGINS[key] = {"module": mod, "spec": app}
            log.info("plug-in loaded: %s (%s)", key, name)
        except Exception as e:
            log.error("plug-in %s could not be loaded: %s", name, e)
    return PLUGINS


def plugin_readers():
    """[(source key, read(cfg) -> [Unit])] for the indexer."""
    out = []
    for key, p in PLUGINS.items():
        if hasattr(p["module"], "read"):
            def read(cfg, key=key, mod=p["module"]):
                return [u for u in (unit_from_dict(key, d) for d in (mod.read(cfg.source(key) or {}) or [])) if u]
            out.append((key, read))
    return out


# ── who handles a request ────────────────────────────────────────────────────
def requester(cfg, fmt):
    """Where a request for this format goes: ("plugin", key) | ("webhook", None) | ("readmeabook", None)
    | ("builtin", None). Custom choices win over the built-in apps."""
    for key, p in PLUGINS.items():
        if fmt in p["spec"]["requests"] and cfg.source(key):
            return "plugin", key
    if fmt in webhook_formats(cfg):
        return "webhook", None
    if fmt == "audiobook":
        from .connectors import readmeabook
        if readmeabook.handles_audiobooks(cfg):
            return "readmeabook", None
    return "builtin", None


def send(cfg, route, item):
    """Send a request to a webhook or plug-in. route: from requester(); item: {format, title,
    authors, year, ids, requested_by, ...}"""
    if route[0] == "webhook":
        return send_webhook(cfg, item)
    key = route[1]
    out = PLUGINS[key]["module"].request(cfg.source(key) or {}, item) or {}
    return {"ok": True, "message": out.get("message") or "Sent", "external_id": out.get("id")}


def any_requester(cfg, fmts):
    return any(requester(cfg, f)[0] != "builtin" for f in fmts)

