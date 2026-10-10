"""Who's watching, listening to or reading what, for admins.

Built only from Omnarr's own progress (playstate.play_progress), so it covers everything played
or read inside Omnarr and nothing watched straight in the Jellyfin/Audiobookshelf apps. Players
report every 20 s while something is playing; the readers report on page turns, so "now" means
a recent report: 2 minutes for video and audio, 15 for reading.
"""
import sqlite3
import time

from . import playstate

LIVE = {"jellyfin": 120, "abs": 120, "plex": 120}
READING = 900
VERB = {"audiobook": "listening to", "book": "reading", "comic": "reading"}       # everything else: watching


def _titles(index_path, keys):
    """{(source, source_id): {work_id, title, kind, cover, format}} for the given keys."""
    if not keys:
        return {}
    try:
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    con.row_factory = sqlite3.Row
    out = {}
    try:
        keys = list(keys)
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            where = " OR ".join("(e.source=? AND e.source_id=?)" for _ in chunk)
            for r in con.execute(f"""SELECT e.source, e.source_id, e.format, w.id work_id, w.title, w.kind, w.cover
                                     FROM editions e JOIN works w ON w.id = e.work_id WHERE {where}""",
                                 [v for k in chunk for v in k]):
                out.setdefault((r["source"], r["source_id"]), dict(r))
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def _names(state_path):
    try:
        con = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        try:
            return {r[0]: r[1] for r in con.execute("SELECT id, username FROM accounts")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def activity(state_path, index_path, now=None, per_person=15, days=60):
    """{"now": [entry], "people": [{account_id, username, last, recent: [entry]}]}."""
    now = now or time.time()
    rows = [r for r in playstate.all_rows(state_path) if (r["updated"] or 0) >= now - days * 86400]
    keys = {(r["source"], str(r["item_id"])) for r in rows}
    keys |= {(r["source"], str(r["parent_id"])) for r in rows if r["parent_id"]}
    titles = _titles(index_path, keys)
    names = _names(state_path)

    def entry(r):
        work = titles.get((r["source"], str(r["item_id"]))) or titles.get((r["source"], str(r["parent_id"] or ""))) or {}
        kind = work.get("kind") or ("video" if r["source"] in ("jellyfin", "plex") else "audiobook" if r["source"] == "abs" else "book")
        if work.get("format") == "comic" or r["source"] == "komga":
            kind = "comic"
        elif r["source"] == "abs":
            kind = "audiobook"
        age = now - (r["updated"] or 0)
        live = (not r["finished"]) and age <= LIVE.get(r["source"], READING)
        dur = r["duration"] or 0
        return {"account_id": r["account_id"], "username": names.get(r["account_id"], f"#{r['account_id']}"),
                "source": r["source"], "kind": kind, "verb": VERB.get(kind, "watching"),
                "title": work.get("title") or r.get("label") or "Something not in the library index",
                "detail": r.get("label") if r.get("label") and r.get("label") != work.get("title") else "",
                "work_id": work.get("work_id"), "cover": work.get("cover"),
                "percent": round(min(1.0, (r["position"] or 0) / dur) * 100) if dur else None,
                "finished": bool(r["finished"]), "updated": r["updated"], "live": live}

    entries = sorted((entry(r) for r in rows), key=lambda e: e["updated"] or 0, reverse=True)
    people = {}
    for e in entries:
        p = people.setdefault(e["account_id"], {"account_id": e["account_id"], "username": e["username"],
                                                "last": e["updated"], "recent": []})
        if len(p["recent"]) < per_person:
            p["recent"].append(e)
    return {"now": [e for e in entries if e["live"]],
            "people": sorted(people.values(), key=lambda p: p["last"] or 0, reverse=True)}
