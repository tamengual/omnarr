"""Live detail + actions: asked of the owning apps when a work is opened (never indexed —
episode and queue state goes stale within minutes of a download).

Reads: Sonarr/Radarr (episodes, files, queue), Jellyfin (watched state), BookBridge
(per-app reading positions), Audiobookshelf (chapters), plus an Activity overview.
Writes: Sonarr/Radarr search + monitor commands only. Every write is recorded in the
state DB `actions` table (when, what, target, result).
"""
import json
import logging
import os
import sqlite3
import time

import httpx

from .connectors import arr
from .connectors.base import ro_connect

log = logging.getLogger("omnarr.live")
_cache = {}
TTL = 45


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time() - TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def invalidate(prefix):
    for k in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(k, None)


# ── Jellyfin helpers ─────────────────────────────────────────────────────────
def _jf(cfg):
    s = cfg.source("jellyfin")
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=60,
                        headers={"Authorization": f'MediaBrowser Token="{s["api_key"]}"'})


def _jf_user(cfg, owner=False):
    """Jellyfin user id for the current request's identity (see identity.py): the signed-in
    person's own user, the server's configured user (OWNER), or None for no identity.
    owner=True always returns the configured user (for reading shared metadata)."""
    from . import identity
    want = identity.OWNER if owner else identity.jellyfin_user.get()
    if want is None:
        return None
    name = (cfg.source("jellyfin").get("user") or "") if want == identity.OWNER else want

    def fetch():
        with _jf(cfg) as c:
            users = c.get("/Users").json()
        u = next((u for u in users if u["Name"].lower() == name.lower()), None)
        if u is None and want == identity.OWNER:
            u = users[0]                       # legacy behaviour for the configured user
        return u["Id"] if u else None
    return _cached(f"jf:user:{name.lower()}", fetch)


def _jf_episodes(cfg, series_id):
    uid = _jf_user(cfg)
    params = {"UserId": uid, "EnableUserData": "true"} if uid else {}
    with _jf(cfg) as c:
        r = c.get(f"/Shows/{series_id}/Episodes", params=params)
        items = r.json().get("Items", []) if r.status_code == 200 else []
    out = {}
    for it in items:
        s, e = it.get("ParentIndexNumber"), it.get("IndexNumber")
        if s is None or e is None:
            continue
        ud = it.get("UserData") or {}
        out[(s, e)] = {"jellyfin_id": it["Id"], "watched": bool(ud.get("Played")),
                       "position": (ud.get("PlaybackPositionTicks") or 0) / 1e7}
    from . import identity, playstate                 # Omnarr's own record for this person
    mine = playstate.for_parent(cfg.state_path, identity.account_id.get(), series_id)
    for w in out.values():
        pos, fin = mine.get(w["jellyfin_id"], (0, False))
        w["watched"] = w["watched"] or fin
        w["position"] = max(w["position"], pos)
    return out


# ── TV ───────────────────────────────────────────────────────────────────────
def show_detail(cfg, editions):
    son = next((e for e in editions if e["source"] == "sonarr"), None)
    jf = next((e for e in editions if e["source"] == "jellyfin"), None)
    jf_link = cfg.link("jellyfin")
    watched = {}
    if jf and cfg.source("jellyfin"):
        try:
            watched = _cached(f"jfeps:{_jf_user(cfg)}:{jf['source_id']}", lambda: _jf_episodes(cfg, jf["source_id"]))   # per person
        except Exception as e:
            log.warning("jellyfin episodes failed: %s", e)
    if not son or not cfg.source("sonarr"):
        # Jellyfin only: what's on disk, with watched state
        seasons = {}
        for (s, e), w in sorted(watched.items()):
            seasons.setdefault(s, []).append({"season": s, "episode": e, "title": "", "has_file": True, **w,
                                              "url": f"{jf_link}/web/#/details?id={w['jellyfin_id']}" if jf_link else ""})
        return {"source": "jellyfin", "seasons": [_season(n, eps) for n, eps in seasons.items()], "queue": []}

    sid = int(son["source_id"])

    def fetch():
        with arr.client(cfg.source("sonarr")) as c:
            series = c.get(f"/series/{sid}").json()
            eps = c.get("/episode", params={"seriesId": sid, "includeEpisodeFile": "true"}).json()
            q = c.get("/queue/details", params={"seriesId": sid, "includeEpisode": "true"}).json()
        return series, eps, q
    series, eps, queue = _cached(f"sonarr:{sid}", fetch)
    dl = {}
    for x in queue if isinstance(queue, list) else []:
        size, left = x.get("size") or 0, x.get("sizeleft") or 0
        rec = {"progress": round(100 * (1 - left / size)) if size else 0, "status": x.get("status"),
               "tracked": x.get("trackedDownloadStatus"), "eta": x.get("timeleft"),
               "messages": [m for s_ in x.get("statusMessages") or [] for m in s_.get("messages") or []][:3]}
        if x.get("episodeId"):
            dl[x["episodeId"]] = rec
    seasons = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for e in eps:
        s, n = e.get("seasonNumber"), e.get("episodeNumber")
        f = e.get("episodeFile") or {}
        w = watched.get((s, n), {})
        aired = bool(e.get("airDateUtc")) and e["airDateUtc"] <= now
        seasons.setdefault(s, []).append({
            "season": s, "episode": n, "title": e.get("title") or "", "air_date": (e.get("airDateUtc") or "")[:10],
            "aired": aired, "has_file": bool(e.get("hasFile")), "monitored": bool(e.get("monitored")),
            "quality": ((f.get("quality") or {}).get("quality") or {}).get("name") or "",
            "size": f.get("size") or 0, "overview": (e.get("overview") or "")[:400],
            "sonarr_episode_id": e["id"], "downloading": dl.get(e["id"]),
            "watched": w.get("watched", False), "position": w.get("position", 0),
            "jellyfin_id": w.get("jellyfin_id"),
            "url": f"{jf_link}/web/#/details?id={w['jellyfin_id']}" if (jf_link and w.get("jellyfin_id")) else "",
        })
    season_mon = {x["seasonNumber"]: x.get("monitored") for x in series.get("seasons") or []}
    out = [_season(n, sorted(eps_, key=lambda x: x["episode"]), season_mon.get(n)) for n, eps_ in sorted(seasons.items())]
    st = series.get("statistics") or {}
    return {"source": "sonarr", "series_id": sid, "monitored": bool(series.get("monitored")),
            "status": series.get("status"), "network": series.get("network"),
            "quality_profile_id": series.get("qualityProfileId"),
            "have": st.get("episodeFileCount"), "aired": st.get("episodeCount"), "total": st.get("totalEpisodeCount"),
            "size": st.get("sizeOnDisk"), "seasons": out,
            "queue": [{"episode_id": k, **v} for k, v in dl.items()]}


def _season(n, eps, monitored=None):
    aired = [e for e in eps if e.get("aired", True)]
    return {"season": n, "monitored": monitored, "episodes": eps,
            "have": sum(1 for e in eps if e.get("has_file")), "aired": len(aired), "total": len(eps),
            "missing": sum(1 for e in aired if not e.get("has_file") and e.get("monitored", True)),
            "watched": sum(1 for e in eps if e.get("watched"))}


# ── Movies ───────────────────────────────────────────────────────────────────
def movie_detail(cfg, editions):
    rad = next((e for e in editions if e["source"] == "radarr"), None)
    jf = next((e for e in editions if e["source"] == "jellyfin"), None)
    out = {"source": "jellyfin" if jf else "radarr"}
    if jf and cfg.source("jellyfin"):
        try:
            uid = _jf_user(cfg)
            with _jf(cfg) as c:
                it = c.get(f"/Users/{uid or _jf_user(cfg, owner=True)}/Items/{jf['source_id']}").json()
            ud = (it.get("UserData") or {}) if uid else {}
            from . import identity, playstate
            pos, fin = playstate.get(cfg.state_path, identity.account_id.get(), "jellyfin", jf["source_id"])
            out.update(watched=bool(ud.get("Played")) or fin,
                       position=max((ud.get("PlaybackPositionTicks") or 0) / 1e7, pos),
                       runtime=(it.get("RunTimeTicks") or 0) / 1e7)
        except Exception as e:
            log.warning("jellyfin movie failed: %s", e)
    if rad and cfg.source("radarr"):
        mid = int(rad["source_id"])

        def fetch():
            with arr.client(cfg.source("radarr")) as c:
                return c.get(f"/movie/{mid}").json(), c.get("/queue/details", params={"movieId": mid}).json()
        m, q = _cached(f"radarr:{mid}", fetch)
        f = m.get("movieFile") or {}
        mi = f.get("mediaInfo") or {}
        out.update(radarr_id=mid, monitored=bool(m.get("monitored")), has_file=bool(m.get("hasFile")),
                   available=bool(m.get("isAvailable")),
                   quality=((f.get("quality") or {}).get("quality") or {}).get("name") or "",
                   size=f.get("size") or 0,
                   video=" ".join(x for x in (mi.get("videoCodec"), mi.get("videoDynamicRangeType")) if x),
                   audio=" ".join(x for x in (mi.get("audioCodec"), str(mi.get("audioChannels") or "")) if x),
                   downloading=[{"progress": round(100 * (1 - (x.get("sizeleft") or 0) / x["size"])) if x.get("size") else 0,
                                 "status": x.get("status"), "eta": x.get("timeleft")} for x in (q if isinstance(q, list) else [])])
    return out


# ── Books: where am I in each app ────────────────────────────────────────────
def book_detail(cfg, editions):
    out = {"positions": [], "chapters": []}
    abs_e = next((e for e in editions if e["source"] == "abs"), None)
    bb = cfg.source("bookbridge")
    if abs_e and bb:
        try:
            con = ro_connect(bb["db"])
            try:
                rows = con.execute("""SELECT client_name, percentage, last_updated FROM states
                                      WHERE abs_id=? ORDER BY last_updated DESC""", (abs_e["source_id"],)).fetchall()
            finally:
                con.close()
            label = {"abs": "Audiobookshelf", "cwa": "Kobo (Calibre-Web)", "storyteller": "Storyteller",
                     "kosync": "KOReader sync", "storygraph": "StoryGraph", "hardcover": "Hardcover"}
            out["positions"] = [{"app": label.get(r["client_name"], r["client_name"]), "client": r["client_name"],
                                 "percent": round(100 * (r["percentage"] or 0), 1),
                                 "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_updated"] or 0))}
                                for r in rows]
        except Exception as e:
            log.warning("bookbridge states failed: %s", e)
    if abs_e and cfg.source("abs"):
        try:
            from .connectors import abs as abs_c
            s = cfg.source("abs")
            if abs_c.api_mode(s):
                with abs_c.client(s) as c:
                    chs = (abs_c.item_detail(cfg, c, abs_e["source_id"]).get("media") or {}).get("chapters") or []
            else:
                con = ro_connect(s["db"])
                try:
                    r = con.execute("SELECT b.chapters FROM libraryItems li JOIN books b ON b.id=li.mediaId WHERE li.id=?",
                                    (abs_e["source_id"],)).fetchone()
                finally:
                    con.close()
                chs = json.loads(r["chapters"] or "[]") if r else []
            out["chapters"] = [{"title": c.get("title"), "start": c.get("start"), "end": c.get("end")} for c in chs]
        except Exception as e:
            log.warning("abs chapters failed: %s", e)
    return out


# ── Actions (writes) ─────────────────────────────────────────────────────────
ACTIONS = {
    # name: (source, builder(body) -> (method, path, json))
    "search_episodes": ("sonarr", lambda b: ("POST", "/command", {"name": "EpisodeSearch", "episodeIds": [int(x) for x in b["episode_ids"]]})),
    "search_season": ("sonarr", lambda b: ("POST", "/command", {"name": "SeasonSearch", "seriesId": int(b["series_id"]), "seasonNumber": int(b["season"])})),
    "search_missing": ("sonarr", lambda b: ("POST", "/command", {"name": "SeriesSearch", "seriesId": int(b["series_id"])})),
    "monitor_episodes": ("sonarr", lambda b: ("PUT", "/episode/monitor", {"episodeIds": [int(x) for x in b["episode_ids"]], "monitored": bool(b["monitored"])})),
    "search_movie": ("radarr", lambda b: ("POST", "/command", {"name": "MoviesSearch", "movieIds": [int(b["movie_id"])]})),
    "monitor_movie": ("radarr", lambda b: ("PUT", "/movie/editor", {"movieIds": [int(b["movie_id"])], "monitored": bool(b["monitored"])})),
}


def run_action(cfg, state_path, name, body):
    if name == "monitor_season":
        return _monitor_season(cfg, state_path, body)
    if name == "search_harder":
        from . import harder
        if not cfg.source("prowlarr"):
            raise RuntimeError("Prowlarr is not configured")
        if body.get("series_id"):
            res = harder.harder_tv(cfg, body["series_id"], body.get("season"))
            invalidate("sonarr:")
        elif body.get("movie_id"):
            res = harder.harder_movie(cfg, body["movie_id"])
            invalidate("radarr:")
        else:
            raise ValueError("series_id or movie_id required")
        _audit(state_path, "search_harder", body, json.dumps(res)[:300])
        return {"ok": True, "result": res}
    if name not in ACTIONS:
        raise ValueError(f"unknown action {name}")
    source, build = ACTIONS[name]
    if not cfg.source(source):
        raise RuntimeError(f"{source} is not configured")
    method, path, payload = build(body)
    with arr.client(cfg.source(source)) as c:
        r = c.request(method, path, json=payload)
    ok = r.status_code < 400
    _audit(state_path, name, payload, f"{r.status_code}")
    invalidate(f"{source}:")
    if not ok:
        raise RuntimeError(f"{source} refused ({r.status_code}): {r.text[:200]}")
    return {"ok": True}


def _monitor_season(cfg, state_path, body):
    sid, season, mon = int(body["series_id"]), int(body["season"]), bool(body["monitored"])
    with arr.client(cfg.source("sonarr")) as c:
        series = c.get(f"/series/{sid}").json()
        for s in series.get("seasons") or []:
            if s["seasonNumber"] == season:
                s["monitored"] = mon
        r = c.put(f"/series/{sid}", json=series)
    _audit(state_path, "monitor_season", {"series_id": sid, "season": season, "monitored": mon}, f"{r.status_code}")
    invalidate("sonarr:")
    if r.status_code >= 400:
        raise RuntimeError(f"sonarr refused ({r.status_code}): {r.text[:200]}")
    return {"ok": True}


def _audit(state_path, action, payload, result):
    try:
        con = sqlite3.connect(state_path)
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS actions (ts REAL, action TEXT, payload TEXT, result TEXT)")
            con.execute("INSERT INTO actions VALUES (?,?,?,?)", (time.time(), action, json.dumps(payload), result))
        con.close()
    except Exception as e:
        log.warning("audit failed: %s", e)


# ── Activity: what is the server doing ───────────────────────────────────────
def _tail(path, n=8):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-n:]]
    except OSError:
        return []


def activity(cfg):
    out = {}

    def safe(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"[:200]}

    def arr_queue(source):
        with arr.client(cfg.source(source)) as c:
            q = c.get("/queue", params={"pageSize": 100, "includeEpisode": "true", "includeSeries": "true",
                                         "includeMovie": "true"}).json()
        rows = []
        for x in q.get("records", []):
            size, left = x.get("size") or 0, x.get("sizeleft") or 0
            ep = x.get("episode") or {}
            rows.append({"id": x.get("id"), "title": x.get("title"),
                         "item": (x.get("series") or x.get("movie") or {}).get("title"),
                         "episode": f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}" if ep else "",
                         "progress": round(100 * (1 - left / size)) if size else 0, "status": x.get("status"),
                         "tracked": x.get("trackedDownloadStatus"), "state": x.get("trackedDownloadState"),
                         "eta": x.get("timeleft"),
                         "messages": [m for s_ in x.get("statusMessages") or [] for m in s_.get("messages") or []][:2]})
        return {"total": q.get("totalRecords", len(rows)), "records": rows}

    if cfg.source("sonarr"):
        safe("sonarr", lambda: arr_queue("sonarr"))
    if cfg.source("radarr"):
        safe("radarr", lambda: arr_queue("radarr"))
    if cfg.source("seerr"):
        def seerr():
            s = cfg.source("seerr")
            with httpx.Client(base_url=s["url"].rstrip("/") + "/api/v1", timeout=30, headers={"X-Api-Key": s["api_key"]}) as c:
                d = c.get("/request", params={"take": 20, "sort": "added", "filter": "all"}).json()
            status = {1: "pending approval", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
            media_status = {1: "unknown", 2: "pending", 3: "processing", 4: "partially available", 5: "available"}
            return [{"type": r.get("type"), "tmdb": (r.get("media") or {}).get("tmdbId"),
                     "status": status.get(r.get("status"), r.get("status")),
                     "media": media_status.get((r.get("media") or {}).get("status"), ""),
                     "created": (r.get("createdAt") or "")[:10]} for r in d.get("results", [])]
        safe("requests", seerr)
    if cfg.source("romarr"):
        def romarr():
            s = cfg.source("romarr")
            with httpx.Client(base_url=s["url"].rstrip("/"), timeout=30, headers={"X-Api-Key": s["api_key"]}) as c:
                return {"queue": c.get("/api/v1/queue").json(), "wanted": c.get("/api/v1/wanted/missing").json()}
        safe("games", romarr)
    if cfg.source("shelfmark"):
        def shelf():
            s = cfg.source("shelfmark")
            with httpx.Client(base_url=s["url"].rstrip("/"), timeout=30, headers={"X-Api-Key": s["api_key"]}) as c:
                return c.get("/api/downloads/active").json()
        safe("books", shelf)
    if cfg.source("storyteller"):
        def st():
            con = ro_connect(cfg.source("storyteller")["db"])
            try:
                return [{"title": r["title"], "status": r["status"], "stage": r["current_stage"],
                         "progress": r["stage_progress"]}
                        for r in con.execute("""SELECT b.title, r.status, r.current_stage, r.stage_progress
                                                FROM readaloud r JOIN book b ON b.uuid=r.book_uuid
                                                WHERE r.status <> 'ALIGNED' ORDER BY r.updated_at DESC LIMIT 20""")]
            finally:
                con.close()
        safe("readalongs", st)
    logs = {}
    bb = cfg.source("bookbridge") or {}
    if bb.get("db"):
        logs["readalong_linker"] = _tail(os.path.join(os.path.dirname(bb["db"]), "readalong-link.log"))
    for name, path in (cfg.get("activity_logs") or {}).items():     # extra log files to show (config.yml)
        logs[name] = _tail(path)
    out["logs"] = logs
    return out
