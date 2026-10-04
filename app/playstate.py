"""Omnarr's own playback progress, per account.

Everyone who plays through Omnarr gets their position, "finished" and "Continue" saved here,
so friends and family only need an Omnarr account. If someone has also linked their own
Jellyfin/Audiobookshelf account (or is an admin using the server's), progress is written there
too; the two are merged (furthest position, finished if either says so).
"""
import sqlite3
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS play_progress (
  account_id INTEGER, source TEXT, item_id TEXT, parent_id TEXT, position REAL, duration REAL,
  finished INTEGER, updated REAL, PRIMARY KEY (account_id, source, item_id))"""


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def record(state_path, account_id, source, item_id, position, duration, finished, parent_id=None):
    if not account_id or not item_id:
        return
    finished = bool(finished or (duration and position >= duration * (0.92 if source == "jellyfin" else 1) - 30))
    con = _con(state_path)
    with con:
        con.execute("""INSERT INTO play_progress VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id, source, item_id) DO UPDATE SET
                         parent_id=COALESCE(excluded.parent_id, parent_id), position=excluded.position,
                         duration=excluded.duration, finished=MAX(finished, excluded.finished), updated=excluded.updated""",
                    (account_id, source, str(item_id), parent_id, max(0.0, position or 0), duration or 0,
                     int(finished), time.time()))
    con.close()


def get(state_path, account_id, source, item_id):
    """(position, finished) or (0, False)."""
    if not account_id:
        return 0, False
    con = _con(state_path)
    try:
        r = con.execute("SELECT position, finished FROM play_progress WHERE account_id=? AND source=? AND item_id=?",
                        (account_id, source, str(item_id))).fetchone()
    finally:
        con.close()
    return (r["position"] or 0, bool(r["finished"])) if r else (0, False)


def for_parent(state_path, account_id, parent_id):
    """{episode item id: (position, finished)} for one show."""
    if not account_id:
        return {}
    con = _con(state_path)
    try:
        return {r["item_id"]: (r["position"] or 0, bool(r["finished"])) for r in con.execute(
            "SELECT item_id, position, finished FROM play_progress WHERE account_id=? AND parent_id=?",
            (account_id, str(parent_id)))}
    finally:
        con.close()


def all_rows(state_path):
    try:
        con = _con(state_path)
    except sqlite3.Error:
        return []
    try:
        return [dict(r) for r in con.execute("SELECT * FROM play_progress")]
    finally:
        con.close()
