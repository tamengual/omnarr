"""Plex Media Server: movies and shows, watched state, artwork, episodes, playback.

Everything goes through Omnarr's server with the token added server-side, so the token never
reaches the browser. The token is optional: a Plex server lets addresses in its "allowed
without auth" networks use the API without one.
"""
import uuid

import httpx

from .base import Unit

CLIENT_ID = "omnarr-" + uuid.uuid5(uuid.NAMESPACE_URL, "omnarr").hex[:12]   # stable per install
PAGE = 200


def headers(s):
    h = {"Accept": "application/json", "X-Plex-Product": "Omnarr", "X-Plex-Version": "1",
         "X-Plex-Client-Identifier": CLIENT_ID, "X-Plex-Platform": "Chrome", "X-Plex-Device": "Omnarr"}
    if s.get("api_key"):
        h["X-Plex-Token"] = s["api_key"]
    return h


def base(s):
    """The server address. People often paste Plex's web-app address (…:32400/web or
    …/web/index.html#!/…); the API lives one level up."""
    url = s["url"].split("#")[0].rstrip("/")
    for tail in ("/web/index.html", "/web"):
        if url.lower().endswith(tail):
            url = url[: -len(tail)]
    return url.rstrip("/")


def client(s, timeout=60):
    return httpx.Client(base_url=base(s), timeout=timeout, headers=headers(s))


def _paged(c, path, params=None):
    start = 0
    while True:
        r = c.get(path, params=params, headers={"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE)})
        r.raise_for_status()
        mc = r.json().get("MediaContainer") or {}
        items = mc.get("Metadata") or []
        yield from items
        total = mc.get("totalSize", mc.get("size", 0))
        start += len(items)
        if not items or start >= total:
            return


def guids(item):
    """{"tmdb": .., "imdb": .., "tvdb": ..} from Plex's Guid list (imdb://tt.., tmdb://..)."""
    out = {}
    for g in item.get("Guid") or []:
        scheme, _, val = (g.get("id") or "").partition("://")
        if scheme in ("tmdb", "imdb", "tvdb") and val:
            out[scheme] = val
    return out


def machine_id(c):
    return (c.get("/identity").json().get("MediaContainer") or {}).get("machineIdentifier", "")


def read(cfg):
    s = cfg.source("plex")
    if not s:
        return []
    link = cfg.link("plex")
    adult_libs = {x.lower() for x in (s.get("adult_libraries") or [])}
    units = []
    with client(s) as c:
        mid = machine_id(c)
        sections = (c.get("/library/sections").json().get("MediaContainer") or {}).get("Directory") or []
        for sec in sections:
            if sec.get("type") not in ("movie", "show"):
                continue
            is_movie = sec["type"] == "movie"
            for it in _paged(c, f"/library/sections/{sec['key']}/all",
                             {"type": 1 if is_movie else 2, "includeGuids": 1}):
                if is_movie:
                    dur = it.get("duration") or 0
                    pct = (it["viewOffset"] / dur) if it.get("viewOffset") and dur else (1.0 if it.get("viewCount") else None)
                    finished = bool(it.get("viewCount"))
                else:
                    leaves, seen = it.get("leafCount") or 0, it.get("viewedLeafCount") or 0
                    pct = (seen / leaves) if leaves and seen else None
                    finished = bool(leaves) and seen >= leaves
                rk = str(it["ratingKey"])
                units.append(Unit(
                    source="plex", source_id=rk, kind="movie" if is_movie else "show",
                    format="movie" if is_movie else "series", title=it.get("title") or "", year=it.get("year"),
                    description=(it.get("summary") or "").strip(), genres=[g.get("tag") for g in it.get("Genre") or [] if g.get("tag")],
                    url=f"{link}/web/index.html#!/server/{mid}/details?key=%2Flibrary%2Fmetadata%2F{rk}" if link and mid else "",
                    cover=f"plex:{rk}" if it.get("thumb") else "",
                    duration=(it["duration"] / 1000) if it.get("duration") else None,
                    added=_date(it.get("addedAt")), progress=pct, finished=finished,
                    library=sec.get("title") or "", rating=it.get("audienceRating") or it.get("rating"),
                    adult=(sec.get("title") or "").lower() in adult_libs, ids=guids(it),
                    extra={"thumb": it.get("thumb") or "", "episodes": it.get("leafCount") if not is_movie else None,
                           "last_listened": _iso(it.get("lastViewedAt"))},
                ))
    return units


def _date(ts):
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else ""


def _iso(ts):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) if ts else ""


def test(s):
    try:
        with client(s, 15) as c:
            r = c.get("/library/sections")
            if r.status_code in (401, 403):
                return False, "Plex refused the token (or this address isn't allowed without one)"
            if r.status_code == 404:
                return False, "That address answers, but it isn't a Plex server's API: use http://host:32400 (no /web)"
            r.raise_for_status()
            secs = [d for d in (r.json().get("MediaContainer") or {}).get("Directory") or [] if d.get("type") in ("movie", "show")]
            name = (c.get("/").json().get("MediaContainer") or {}).get("friendlyName") or "Plex"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"Connected to {name}: {len(secs)} movie/TV librar{'y' if len(secs) == 1 else 'ies'}"


def cover_bytes(cfg, rating_key, width=480):
    s = cfg.source("plex")
    with client(s, 30) as c:
        meta = (c.get(f"/library/metadata/{rating_key}").json().get("MediaContainer") or {}).get("Metadata") or [{}]
        thumb = meta[0].get("thumb")
        if not thumb:
            return None
        r = c.get("/photo/:/transcode", params={"url": thumb, "width": width, "height": int(width * 1.5),
                                                 "minSize": 1, "upscale": 1, "format": "jpg"})
    return r.content if r.status_code == 200 else None


def episodes(cfg, show_key):
    """[{rating_key, season, episode, title, duration (s), position (s), watched, summary}] for a show."""
    s = cfg.source("plex")
    with client(s) as c:
        items = list(_paged(c, f"/library/metadata/{show_key}/allLeaves"))
    return [{"rating_key": str(e["ratingKey"]), "season": e.get("parentIndex"), "episode": e.get("index"),
             "title": e.get("title") or "", "summary": (e.get("summary") or "").strip(),
             "duration": (e.get("duration") or 0) / 1000, "position": (e.get("viewOffset") or 0) / 1000,
             "watched": bool(e.get("viewCount"))} for e in items]


def metadata(cfg, rating_key):
    s = cfg.source("plex")
    with client(s) as c:
        meta = (c.get(f"/library/metadata/{rating_key}").json().get("MediaContainer") or {}).get("Metadata") or [None]
    return meta[0]
