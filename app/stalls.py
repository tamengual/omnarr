"""Private downloads that stop moving get replaced with another copy.

Sonarr/Radarr downloads are left to decluttarr (or the apps themselves); Private downloads go
straight from Prowlarr to qBittorrent, so nothing else watches them. A download counts as stalled
when its downloaded bytes haven't grown for STALL_HOURS while qBittorrent was trying (waiting in
line or paused doesn't count). A stalled one is removed with its partial files; if it was for a
known scene, that scene goes back on the wanted list with the dead copy excluded, so the next
search picks a different one. Finished downloads are never touched (they seed for your ratio).
"""
import json
import logging
import sqlite3
import time

from . import adultfind as af
from . import adulttidy

log = logging.getLogger("omnarr.stalls")
STALL_HOURS = 6
MAX_PER_TICK = 3           # never clear out a whole queue at once
MAX_TRIES = 3              # copies tried for one scene before giving up on it
WAITING = {"queuedDL", "pausedDL", "stoppedDL", "checkingDL", "checkingResumeData", "moving", "allocating"}

SCHEMA = "CREATE TABLE IF NOT EXISTS stall_watch (hash TEXT PRIMARY KEY, name TEXT, downloaded INTEGER, changed REAL)"


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def stalled(state_path, torrents, now=None):
    """Update the progress clock for each unfinished torrent; return the ones stalled STALL_HOURS."""
    now = now or time.time()
    con = _con(state_path)
    seen = {r["hash"]: dict(r) for r in con.execute("SELECT * FROM stall_watch")}
    out = []
    with con:
        live = set()
        for t in torrents:
            h = t["hash"]
            if (t.get("progress") or 0) >= 1:
                continue                                # finished: seeding, never ours to touch
            live.add(h)
            got = t.get("downloaded") or 0
            row = seen.get(h)
            if row is None:
                # nothing ever arrived: the clock starts when it was added; otherwise from now
                start = (t.get("added_on") or now) if got == 0 else now
                con.execute("INSERT INTO stall_watch VALUES (?,?,?,?)", (h, t.get("name"), got, start))
                row = {"downloaded": got, "changed": start}
            if t.get("state") in WAITING or got > (row["downloaded"] or 0):
                con.execute("UPDATE stall_watch SET downloaded=?, changed=? WHERE hash=?", (got, now, h))
                continue
            if now - row["changed"] >= STALL_HOURS * 3600:
                out.append(t)
        for h in set(seen) - live:                      # finished or removed: stop watching
            con.execute("DELETE FROM stall_watch WHERE hash=?", (h,))
    con.close()
    return out


def _grab_for(con, t):
    """The adult_grabs row this torrent came from (exact hash, else the closest name near the time)."""
    rows = [dict(r) for r in con.execute("SELECT * FROM adult_grabs WHERE at > ?", (time.time() - 30 * 86400,))]
    exact = next((g for g in rows if g.get("hash") == t["hash"]), None)
    if exact:
        return exact
    near = [g for g in rows if not g.get("hash") and abs((t.get("added_on") or 0) - g["at"]) < 6 * 3600]
    scored = sorted(((adulttidy._match_score(t, g), g) for g in near), key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] >= 0.4 else None


def tick(cfg, state_path, audit=lambda *a: None, now=None):
    """Replace stalled Private downloads. Returns [{name, scene, action}]."""
    s = cfg.source("qbittorrent")
    if not s:
        return []
    category = s.get("category") or "stash"
    done = []
    with adulttidy.qbit(cfg) as q:
        torrents = q.get("/torrents/info", params={"category": category}).json()
        for t in stalled(state_path, torrents, now)[:MAX_PER_TICK]:
            con = af.grabs_con(state_path)
            g = _grab_for(con, t)
            r = q.post("/torrents/delete", data={"hashes": t["hash"], "deleteFiles": "true"})
            if r.status_code >= 400:
                con.close()
                log.warning("couldn't remove stalled %s (%s)", t["name"], r.status_code)
                continue
            hours = STALL_HOURS
            action = f"removed after {hours} hours without progress"
            scene = json.loads(g["scene"]) if g and g.get("scene") else None
            if g:
                with con:
                    con.execute("UPDATE adult_grabs SET hash=?, tidied=?, tidy_note=? WHERE guid=?",
                                (t["hash"], time.time(), f"stalled: {action}", g["guid"]))
            con.close()
            if scene and scene.get("id"):
                tries = af.retry_scene(state_path, scene, g["guid"], g["title"], g.get("account_id"))
                action += ("; looking for another copy" if tries < MAX_TRIES
                           else f"; {tries} copies failed, so it's no longer being looked for")
            c2 = _con(state_path)
            with c2:
                c2.execute("DELETE FROM stall_watch WHERE hash=?", (t["hash"],))
            c2.close()
            log.info("stalled private download %s: %s", t["name"], action)
            audit("private_stalled", {"name": t["name"][:200]}, action)
            done.append({"name": t["name"], "scene": (scene or {}).get("title"), "action": action})
    if done:
        af.work_wanted(cfg, state_path, audit)          # look for the replacements now, not in 6 hours
    return done
