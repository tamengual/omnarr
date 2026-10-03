"""Configuration.

Two layers:
  * config.yml (optional, OMNARR_CONFIG, default /config/config.yml) — bootstrap only:
    where the index/state databases live, refresh interval, and shared-world lists
    (adaptations, universes, series aliases). Anything under `sources:`/`links:` there is
    imported ONCE into the database on first start and ignored afterwards.
  * the `connections` table in the state database — one row per app, edited from the
    setup wizard in the web UI. This is the source of truth for every integration.

Secrets live only in the state database (and config.yml if you use it): keep /config and
/data private (mode 0700) and never commit them.
"""
import json
import logging
import os
import sqlite3
import threading
import time

import yaml

log = logging.getLogger("omnarr.config")
DEFAULT_PATH = "/config/config.yml"
# Old `links:` names -> app keys (Calibre's browser address is its Calibre-Web).
LINK_ALIASES = {"cwa": "calibre"}


class Config:
    def __init__(self, raw=None, state_path=None):
        self.raw = raw or {}
        self.state_path = state_path or self.get("index.state", "/data/state.db")
        self._cache, self._cache_at = None, 0.0
        self._lock = threading.Lock()

    # ── bootstrap values (config.yml) ────────────────────────────────────────
    def get(self, dotted, default=None):
        cur = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # ── connections (state DB) ───────────────────────────────────────────────
    def _con(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        con = sqlite3.connect(self.state_path, timeout=15)
        con.execute("""CREATE TABLE IF NOT EXISTS connections (
                         app TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, settings TEXT, updated REAL)""")
        return con

    def connections(self, fresh=False):
        """{app: {"enabled": bool, **settings}} — cached for 5 s (read on every request)."""
        with self._lock:
            if not fresh and self._cache is not None and time.time() - self._cache_at < 5:
                return self._cache
            con = self._con()
            try:
                rows = con.execute("SELECT app, enabled, settings FROM connections").fetchall()
            finally:
                con.close()
            self._cache = {a: {**json.loads(s or "{}"), "enabled": bool(e)} for a, e, s in rows}
            self._cache_at = time.time()
            return self._cache

    def save_connection(self, app, settings, enabled=True):
        con = self._con()
        with con:
            con.execute("INSERT OR REPLACE INTO connections VALUES (?,?,?,?)",
                        (app, int(bool(enabled)), json.dumps(settings), time.time()))
        con.close()
        self._cache = None

    def delete_connection(self, app):
        con = self._con()
        with con:
            con.execute("DELETE FROM connections WHERE app=?", (app,))
        con.close()
        self._cache = None

    def source(self, name):
        """An app's settings, or None when it isn't connected / is switched off."""
        s = self.connections().get(name)
        if not s or not s.get("enabled", True):
            return None
        return s

    def link(self, name):
        """Browser-reachable base address for "Open in <app>" links."""
        app = LINK_ALIASES.get(name, name)
        s = self.connections().get(app) or {}
        return (s.get("browser_url") or s.get("url") or "").rstrip("/")

    # ── one-time import of the old file-based setup ──────────────────────────
    def import_legacy(self):
        if self.connections(fresh=True):
            return 0
        sources = self.get("sources") or {}
        links = self.get("links") or {}
        n = 0
        for app, s in sources.items():
            if not isinstance(s, dict):
                continue
            s = dict(s)
            enabled = s.pop("enabled", True) is not False
            browser = links.get(app) or (links.get("cwa") if app == "calibre" else None)
            if browser:
                s["browser_url"] = browser
            self.save_connection(app, s, enabled)
            n += 1
        if n:
            log.info("imported %d connections from config.yml (the database is the source of truth from now on)", n)
        return n


def load(path=None):
    path = path or os.environ.get("OMNARR_CONFIG") or DEFAULT_PATH
    raw = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    cfg = Config(raw)
    cfg.import_legacy()
    return cfg
