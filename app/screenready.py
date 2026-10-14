"""Movie/TV requests: tell the person who asked once it's actually watchable.

Seerr only knows the request came from Omnarr's key, so it can't tell the right person. Omnarr
records who asked for what (by TMDB id) and checks its own index: a request is ready when the
title has a playable copy in Jellyfin or Plex, not merely when Radarr/Sonarr has a file.
"""
import json
import logging
import sqlite3
import time

from . import search

log = logging.getLogger("omnarr.screenready")
GIVE_UP_DAYS = 180
PLAYERS = ("jellyfin", "plex")

SCHEMA = """CREATE TABLE IF NOT EXISTS screen_requests (
  id INTEGER PRIMARY KEY, account_id INTEGER, kind TEXT, tmdb TEXT, title TEXT, created REAL,
  status TEXT, ready_at REAL, work_id TEXT, partial INTEGER DEFAULT 0)"""


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def record(state_path, account_id, kind, tmdb, title=""):
    """Remember a movie/TV request (asking again for the same title just keeps the first)."""
    kind = "movie" if kind == "movie" else "tv"
    con = _con(state_path)
    with con:
        dup = con.execute("SELECT id FROM screen_requests WHERE account_id IS ? AND kind=? AND tmdb=? AND status='waiting'",
                          (account_id, kind, str(tmdb))).fetchone()
        if not dup:
            con.execute("INSERT INTO screen_requests (account_id, kind, tmdb, title, created, status) VALUES (?,?,?,?,?,'waiting')",
                        (account_id, kind, str(tmdb), (title or "")[:200], time.time()))
    con.close()


def find_watchable(index_path, kind, tmdb):
    """(work_id, title, partial) when the title can be played in Jellyfin/Plex, else None."""
    con = search.connect(index_path)
    try:
        rows = con.execute("""SELECT DISTINCT w.id, w.title, w.sources, w.info FROM editions e JOIN works w ON w.id = e.work_id
                              WHERE w.kind = ? AND json_extract(e.extra, '$.ids.tmdb') = ?""",
                           ("movie" if kind == "movie" else "show", str(tmdb))).fetchall()
    finally:
        con.close()
    for r in rows:
        if set(json.loads(r["sources"] or "[]")) & set(PLAYERS):
            info = json.loads(r["info"] or "{}")
            return r["id"], r["title"], info.get("availability") == "partial"
    return None


def tick(cfg, state_path, index_path, public_url=""):
    """Mark waiting requests ready (and tell whoever asked, once); give up after GIVE_UP_DAYS."""
    from . import notify
    con = _con(state_path)
    rows = [dict(r) for r in con.execute("SELECT * FROM screen_requests WHERE status='waiting'")]
    con.close()
    now, done = time.time(), []
    for r in rows:
        try:
            hit = find_watchable(index_path, r["kind"], r["tmdb"])
        except sqlite3.Error as e:                  # the index is being rebuilt: next time
            log.info("ready check skipped: %s", e)
            return done
        if hit:
            wid, title, partial = hit
            title = title or r["title"]
            con = _con(state_path)
            with con:
                con.execute("UPDATE screen_requests SET status='ready', ready_at=?, work_id=?, title=?, partial=? WHERE id=?",
                            (now, wid, title, int(partial), r["id"]))
            con.close()
            what = "The first episodes are" if partial else ("It's" if r["kind"] == "movie" else "The show is")
            url = f"{public_url.rstrip('/')}/#work={wid}" if public_url else ""
            notify.send(cfg, state_path, "request_ready", f"Ready to watch: {title}",
                        f"{title} is ready. {what} in your library and can be played now.",
                        to=[r["account_id"]] if r["account_id"] else (), url=url)
            done.append(title)
        elif now - (r["created"] or now) > GIVE_UP_DAYS * 86400:
            con = _con(state_path)
            with con:
                con.execute("UPDATE screen_requests SET status='expired' WHERE id=?", (r["id"],))
            con.close()
    return done


def mine(state_path, account_id, limit=30):
    """Recent movie/TV requests for one person (all of them for account_id=None)."""
    con = _con(state_path)
    try:
        q = "SELECT * FROM screen_requests" + ("" if account_id is None else " WHERE account_id=?")
        args = () if account_id is None else (account_id,)
        return [dict(r) for r in con.execute(q + " ORDER BY status='ready', created DESC LIMIT ?", (*args, limit))]
    finally:
        con.close()
