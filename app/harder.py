"""Search harder: when Sonarr/Radarr's own search can't find something, ask every indexer
directly (Prowlarr free-text search) and hand promising releases back to Sonarr/Radarr with
"push release" — they still apply every rule (quality, size, language, custom formats) and
only grab what passes.

Why it's needed: the *arr search ("<title> S03" by TVDB/TMDB id) can be crowded out on
public indexers — e.g. "The L Word" results were all "The L Word Generation Q", so the
original's season packs never reached Sonarr, though a plain search found them at once.

Title rule (protects against sequels/spin-offs): after the exact title (+ optional year /
country tag) the release name must go straight to the season ("S03", "Season 3") for TV, or
to the year/quality for movies.
"""
import json
import logging
import re
import sqlite3
import time
import urllib.parse

import httpx

from .connectors import arr

log = logging.getLogger("omnarr.harder")
AUTO_EVERY_DAYS = 7          # automatic: each season/movie at most weekly
AUTO_MIN_AGE_DAYS = 3        # give Sonarr/Radarr's normal search a few days first
AUTO_MAX_PER_RUN = 8         # be gentle with public indexers


def _prowlarr(cfg):
    s = cfg.source("prowlarr")
    return httpx.Client(base_url=s["url"].rstrip("/") + "/api/v1", timeout=120, headers={"X-Api-Key": s["api_key"]})


def _norm(s):
    s = re.sub(r"[\(\)\[\]]", " ", s or "")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def title_ok_tv(release, series_title, year, season):
    rt = _norm(release)
    base = _norm(re.sub(r"\(\d{4}\)", "", series_title))
    variants = {base, _norm(re.sub(r"\s*\((us|uk|au|ca|nz)\)", "", series_title, flags=re.I))}
    for v in variants:
        if not rt.startswith(v + " "):
            continue
        rest = rt[len(v) + 1:]
        rest = re.sub(rf"^({year}|us|uk|au|ca|nz)\s+", "", rest) if year else re.sub(r"^(us|uk|au|ca|nz)\s+", "", rest)
        rest = re.sub(rf"^({year})\s+", "", rest) if year else rest
        if re.match(rf"^(s0?{season}(\s|e\d)|season\s+0?{season}\b)", rest):
            return True
    return False


def title_ok_movie(release, title, year):
    rt, base = _norm(release), _norm(title)
    if not rt.startswith(base + " "):
        return False
    rest = rt[len(base) + 1:]
    return bool(year) and rest.startswith(str(year))


def _prowlarr_search(cfg, query, categories):
    with _prowlarr(cfg) as c:
        q = [("query", query), ("type", "search"), ("limit", "100")] + [("categories", str(x)) for x in categories]
        r = c.get("/search?" + urllib.parse.urlencode(q))
        r.raise_for_status()
        return r.json()


def _push(cfg, source, rel):
    body = {"title": rel["title"], "downloadUrl": rel.get("downloadUrl") or rel.get("magnetUrl"),
            "protocol": rel.get("protocol", "torrent"), "publishDate": rel.get("publishDate"),
            "indexer": rel.get("indexer"), "indexerId": 0, "size": rel.get("size"), "seeders": rel.get("seeders")}
    with arr.client(cfg.source(source)) as c:
        r = c.post("/release/push", json=body)
    if r.status_code >= 400:
        return False, f"push failed {r.status_code}"
    out = r.json()
    o = out[0] if isinstance(out, list) and out else out
    return bool(o.get("approved")), "; ".join(o.get("rejections") or [])[:120]


def harder_tv(cfg, series_id, season=None):
    with arr.client(cfg.source("sonarr")) as c:
        s = c.get(f"/series/{int(series_id)}").json()
        eps = c.get("/episode", params={"seriesId": int(series_id)}).json()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    missing = sorted({e["seasonNumber"] for e in eps if e["seasonNumber"] > 0 and e.get("monitored")
                      and not e.get("hasFile") and (e.get("airDateUtc") or "9") <= now})
    seasons = [int(season)] if season is not None else missing
    title, year = s["title"], s.get("year")
    results = []
    for sn in seasons:
        found, grabbed, reasons = 0, None, {}
        seen = set()
        for query in (f"{_norm(re.sub(r'[()]', '', title))} S{sn:02d}", f"{_norm(re.sub(r'[()]', '', title))} Season {sn}"):
            for rel in sorted(_prowlarr_search(cfg, query, [5000]), key=lambda r: -(r.get("seeders") or 0)):
                if rel["title"] in seen or not title_ok_tv(rel["title"], title, year, sn):
                    continue
                seen.add(rel["title"])
                found += 1
                ok, why = _push(cfg, "sonarr", rel)
                if ok:
                    grabbed = rel["title"]
                    break
                reasons[why.split(":")[0][:60]] = reasons.get(why.split(":")[0][:60], 0) + 1
                if found >= 6:
                    break
            if grabbed or found >= 6:
                break
        results.append({"season": sn, "candidates": found, "grabbed": grabbed,
                        "rejected": dict(sorted(reasons.items(), key=lambda x: -x[1])[:3])})
    return {"title": title, "seasons": results}


def harder_movie(cfg, movie_id):
    with arr.client(cfg.source("radarr")) as c:
        m = c.get(f"/movie/{int(movie_id)}").json()
    title, year = m["title"], m.get("year")
    found, grabbed, reasons = 0, None, {}
    for rel in sorted(_prowlarr_search(cfg, f"{_norm(title)} {year}", [2000]), key=lambda r: -(r.get("seeders") or 0)):
        if not title_ok_movie(rel["title"], title, year):
            continue
        found += 1
        ok, why = _push(cfg, "radarr", rel)
        if ok:
            grabbed = rel["title"]
            break
        reasons[why.split(":")[0][:60]] = reasons.get(why.split(":")[0][:60], 0) + 1
        if found >= 6:
            break
    return {"title": title, "candidates": found, "grabbed": grabbed,
            "rejected": dict(sorted(reasons.items(), key=lambda x: -x[1])[:3])}


# ── automatic: weekly for anything still missing after a few days ────────────
def _log_con(state_path):
    con = sqlite3.connect(state_path)
    con.execute("CREATE TABLE IF NOT EXISTS harder_log (target TEXT PRIMARY KEY, last REAL, result TEXT)")
    return con


def auto(cfg, state_path, audit):
    if not cfg.source("prowlarr"):
        return
    con = _log_con(state_path)
    last = {r[0]: r[1] for r in con.execute("SELECT target, last FROM harder_log")}
    con.close()
    now, done = time.time(), 0
    cutoff_added = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - AUTO_MIN_AGE_DAYS * 86400))
    todo = []
    if cfg.source("sonarr"):
        with arr.client(cfg.source("sonarr")) as c:
            for s in c.get("/series").json():
                st = s.get("statistics") or {}
                if s.get("monitored") and (s.get("added") or "") <= cutoff_added and \
                        (st.get("episodeFileCount") or 0) < (st.get("episodeCount") or 0):
                    todo.append(("sonarr", s["id"]))
    if cfg.source("radarr"):
        with arr.client(cfg.source("radarr")) as c:
            for m in c.get("/movie").json():
                if m.get("monitored") and not m.get("hasFile") and m.get("isAvailable") and (m.get("added") or "") <= cutoff_added:
                    todo.append(("radarr", m["id"]))
    for source, sid in todo:
        key = f"{source}:{sid}"
        if last.get(key, 0) > now - AUTO_EVERY_DAYS * 86400:
            continue
        if done >= AUTO_MAX_PER_RUN:
            break
        try:
            res = harder_tv(cfg, sid) if source == "sonarr" else harder_movie(cfg, sid)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"[:200]}
        con = _log_con(state_path)
        with con:
            con.execute("INSERT OR REPLACE INTO harder_log VALUES (?,?,?)", (key, now, json.dumps(res)))
        con.close()
        audit("search_harder_auto", {"target": key}, json.dumps(res)[:300])
        log.info("search harder %s: %s", key, json.dumps(res)[:200])
        done += 1
