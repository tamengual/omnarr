"""Shared types for connectors.

A connector reads one app and yields Units: one Unit = one thing that app holds
(a Calibre book, an Audiobookshelf audiobook, a Jellyfin movie...). The indexer
then groups Units into Works (the same book in three apps = one Work).

Connectors only READ. SQLite sources are opened with mode=ro on read-only
mounts; API sources only use GET.
"""
import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Unit:
    source: str                 # calibre | abs | storyteller | jellyfin | komga | stash | romm
    source_id: str
    kind: str                   # book | movie | show | comic | game | scene
    format: str                 # ebook | audiobook | readalong | movie | series | comic | game | scene
    title: str
    authors: list = field(default_factory=list)
    narrators: list = field(default_factory=list)
    series: str = ""
    series_index: float = None
    year: int = None
    description: str = ""
    genres: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    url: str = ""               # where "Open" goes (browser-reachable)
    cover: str = ""             # cover key, served by /api/cover/<key>
    duration: float = None      # seconds
    added: str = ""             # ISO date
    progress: float = None      # 0..1 for the configured user
    finished: bool = False
    hidden: bool = False        # e.g. Calibre books tagged not-mine
    adult: bool = False
    library: str = ""           # app-side library/collection name (facet)
    rating: float = None
    ids: dict = field(default_factory=dict)      # isbn / asin / tmdb / imdb ...
    match: dict = field(default_factory=dict)    # exact-match keys used by the indexer
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.source}:{self.source_id}"

    @property
    def first_author(self):
        return self.authors[0] if self.authors else ""


def ro_connect(path):
    """Open an app's SQLite DB read-only. Works for WAL databases on a :ro mount
    as long as the owning app is running (its -wal/-shm files exist)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def jlist(value):
    """ABS/Storyteller store some lists as JSON text."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        v = json.loads(value)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def iso_time(value):
    """Any app timestamp (epoch ms or s, or an ISO string) -> "YYYY-MM-DDTHH:MM:SS" (UTC), or "".
    Progress is sorted by comparing these as strings, so every source must use this form."""
    import time as _t
    if value in (None, ""):
        return ""
    s = str(value).strip()
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        n = n / 1000 if n > 1e11 else n
        return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(n)) if n > 0 else ""
    return s.replace(" ", "T")[:19]


def year_of(value):
    if value is None:
        return None
    s = str(value)
    if len(s) >= 4 and s[:4].isdigit():
        y = int(s[:4])
        return y if 1000 < y < 2200 and y != 1601 else None    # Calibre's "unknown" date is 0101/1601
    return None
