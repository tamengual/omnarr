"""Sonarr / Radarr (HTTP API v3). Index side: every tracked series/movie, so things that
are requested but not yet in Jellyfin still show up ("on its way"). Live side lives in
app/live.py (episodes, queue, actions)."""
import httpx

from .base import Unit


def client(s):
    return httpx.Client(base_url=s["url"].rstrip("/") + "/api/v3", timeout=60,
                        headers={"X-Api-Key": s["api_key"], "Accept": "application/json"})


def _poster(images):
    for i in images or []:
        if i.get("coverType") == "poster":
            return True
    return False


def _sonarr_status(s):
    st = s.get("statistics") or {}
    have, aired = st.get("episodeFileCount") or 0, st.get("episodeCount") or 0
    return {"have": have, "aired": aired, "total": st.get("totalEpisodeCount") or 0,
            "size": st.get("sizeOnDisk") or 0, "seasons": st.get("seasonCount") or 0,
            "monitored": bool(s.get("monitored")), "airing": s.get("status") == "continuing"}


def read_sonarr(cfg):
    s = cfg.source("sonarr")
    if not s:
        return []
    base = cfg.link("sonarr")
    with client(s) as c:
        series = c.get("/series").json()
    units = []
    for x in series:
        ids = {k: str(x[f]) for k, f in (("tvdb", "tvdbId"), ("tmdb", "tmdbId"), ("imdb", "imdbId")) if x.get(f)}
        info = _sonarr_status(x)
        units.append(Unit(
            source="sonarr", source_id=str(x["id"]), kind="show", format="tracked",
            title=x.get("title") or "", year=x.get("year"), description=(x.get("overview") or "").strip(),
            genres=x.get("genres") or [], url=f"{base}/series/{x.get('titleSlug')}" if base else "",
            cover=f"sonarr:{x['id']}" if _poster(x.get("images")) else "",
            added=(x.get("added") or "")[:10], rating=((x.get("ratings") or {}).get("value") or None),
            library="Sonarr", ids=ids, extra={"arr": info, "network": x.get("network") or ""},
        ))
    return units


def read_radarr(cfg):
    s = cfg.source("radarr")
    if not s:
        return []
    base = cfg.link("radarr")
    with client(s) as c:
        movies = c.get("/movie").json()
    units = []
    for x in movies:
        ids = {k: str(x[f]) for k, f in (("tmdb", "tmdbId"), ("imdb", "imdbId")) if x.get(f)}
        mf = x.get("movieFile") or {}
        info = {"has_file": bool(x.get("hasFile")), "monitored": bool(x.get("monitored")),
                "available": bool(x.get("isAvailable")), "size": mf.get("size") or 0,
                "quality": ((mf.get("quality") or {}).get("quality") or {}).get("name") or ""}
        units.append(Unit(
            source="radarr", source_id=str(x["id"]), kind="movie", format="tracked",
            title=x.get("title") or "", year=x.get("year"), description=(x.get("overview") or "").strip(),
            genres=x.get("genres") or [], url=f"{base}/movie/{x.get('tmdbId')}" if base else "",
            cover=f"radarr:{x['id']}" if _poster(x.get("images")) else "",
            added=(x.get("added") or "")[:10], duration=(x.get("runtime") or 0) * 60 or None,
            rating=(((x.get("ratings") or {}).get("tmdb") or {}).get("value") or None),
            library="Radarr", ids=ids, extra={"arr": info},
        ))
    return units


def poster(cfg, source, sid):
    s = cfg.source(source)
    with client(s) as c:
        r = c.get(f"/mediacover/{int(sid)}/poster.jpg")
        if r.status_code == 200:
            return r.content
    return None
