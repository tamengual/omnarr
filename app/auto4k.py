"""Big visual movies in 4K: newly added Radarr movies that are big sci-fi / fantasy / animation /
adventure releases move to a 4K quality profile, and Radarr is asked to find a 4K copy.

Turn it on by naming the profile in the Radarr connection ("4K quality profile for big visual
movies", e.g. "Ultra-HD"). Only movies added after that are touched, each one once, so moving a
movie back by hand sticks.
"""
import datetime
import json
import logging
import sqlite3
import time

from .connectors import arr

log = logging.getLogger("omnarr.auto4k")
VISUAL = {"Science Fiction", "Fantasy", "Animation", "Adventure"}
MIN_YEAR = 2008                      # older films rarely have a 4K master worth the space
MIN_VOTES = 2000                     # a big, widely seen release (TMDB votes)
NEW_RELEASE_POPULARITY = 10          # brand-new films haven't collected votes yet


def qualifies(m, this_year=None):
    """(yes, why) for one Radarr movie."""
    this_year = this_year or datetime.date.today().year
    genres = set(m.get("genres") or []) & VISUAL
    year = m.get("year") or 0
    votes = ((m.get("ratings") or {}).get("tmdb") or {}).get("votes") or 0
    big = votes >= MIN_VOTES or (year >= this_year - 1 and (m.get("popularity") or 0) >= NEW_RELEASE_POPULARITY)
    if genres and year >= MIN_YEAR and big:
        return True, f"{', '.join(sorted(genres))}, {year}, {'widely seen' if votes >= MIN_VOTES else 'big new release'}"
    return False, ""


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.execute("CREATE TABLE IF NOT EXISTS auto4k (movie_id INTEGER PRIMARY KEY, title TEXT, decided REAL, moved INTEGER, why TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS auto4k_meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


def run(cfg, state_path, audit=lambda *a: None):
    s = cfg.source("radarr") or {}
    target = (s.get("uhd_profile") or "").strip()
    if not target:
        return []
    con = _con(state_path)
    since = con.execute("SELECT v FROM auto4k_meta WHERE k='since'").fetchone()
    if not since:                                       # switched on now: only movies added from here on
        since = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with con:
            con.execute("INSERT INTO auto4k_meta VALUES ('since', ?)", (since,))
    else:
        since = since[0]
    done = {r[0] for r in con.execute("SELECT movie_id FROM auto4k")}
    moved = []
    with arr.client(s) as c:
        profile = next((p for p in c.get("/qualityprofile").json() if p["name"].lower() == target.lower()), None)
        if not profile:
            log.warning("auto 4K: Radarr has no quality profile called %r", target)
            con.close()
            return []
        for m in c.get("/movie").json():
            if m["id"] in done or (m.get("added") or "") < since:
                continue
            yes, why = qualifies(m)
            if yes and m["qualityProfileId"] != profile["id"]:
                m["qualityProfileId"] = profile["id"]
                c.put(f"/movie/{m['id']}", json=m).raise_for_status()
                if m.get("monitored"):
                    c.post("/command", json={"name": "MoviesSearch", "movieIds": [m["id"]]})
                moved.append({"title": m["title"], "why": why})
                audit("auto_4k", {"movie": m["title"]}, why)
            with con:
                con.execute("INSERT OR REPLACE INTO auto4k VALUES (?,?,?,?,?)", (m["id"], m["title"], time.time(), int(yes), why))
    con.close()
    if moved:
        log.info("auto 4K moved: %s", json.dumps(moved)[:300])
    return moved
