"""Omnarr's own playback progress, per account.

Everyone who plays through Omnarr gets their position, "finished" and "Continue" saved here,
so friends and family only need an Omnarr account. If someone has also linked their own
Jellyfin/Audiobookshelf account (or is an admin using the server's), progress is written there
too; the two are merged (furthest position, finished if either says so).

Video and audio store seconds. The readers store pages (Komga: position=page, duration=pages)
or a fraction (ebooks: position 0..1, duration 1) plus `locator`, the exact place (an EPUB CFI).
"""
import sqlite3
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS play_progress (
  account_id INTEGER, source TEXT, item_id TEXT, parent_id TEXT, position REAL, duration REAL,
  finished INTEGER, updated REAL, PRIMARY KEY (account_id, source, item_id))"""
TIMED = ("jellyfin", "abs")                       # positions in seconds; "nearly at the end" counts


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    if "locator" not in {r[1] for r in con.execute("PRAGMA table_info(play_progress)")}:
        con.execute("ALTER TABLE play_progress ADD COLUMN locator TEXT")
    return con


def record(state_path, account_id, source, item_id, position, duration, finished, parent_id=None, locator=None):
    if not account_id or not item_id:
        return
    if source in TIMED:
        finished = bool(finished or (duration and position >= duration * (0.92 if source == "jellyfin" else 1) - 30))
    else:
        finished = bool(finished or (duration and position >= duration))
    con = _con(state_path)
    with con:
        con.execute("""INSERT INTO play_progress (account_id, source, item_id, parent_id, position, duration,
                                                  finished, updated, locator) VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id, source, item_id) DO UPDATE SET
                         parent_id=COALESCE(excluded.parent_id, parent_id), position=excluded.position,
                         duration=excluded.duration, finished=MAX(finished, excluded.finished), updated=excluded.updated,
                         locator=COALESCE(excluded.locator, locator)""",
                    (account_id, source, str(item_id), parent_id, max(0.0, position or 0), duration or 0,
                     int(finished), time.time(), locator))
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


def get_place(state_path, account_id, source, item_id):
    """{position, duration, finished, locator} or None (for the readers)."""
    if not account_id:
        return None
    con = _con(state_path)
    try:
        r = con.execute("""SELECT position, duration, finished, locator FROM play_progress
                           WHERE account_id=? AND source=? AND item_id=?""", (account_id, source, str(item_id))).fetchone()
    finally:
        con.close()
    return {"position": r["position"] or 0, "duration": r["duration"] or 0, "finished": bool(r["finished"]),
            "locator": r["locator"]} if r else None


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
