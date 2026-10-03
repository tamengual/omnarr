"""Requests ("Get it") from a library item.

- Screen/game adaptations of a book: found on Wikidata ("based on", P144, of the
  book or of its book series), with TMDB / IGDB ids. Cached in the state DB.
- Movies/TV are requested through Seerr (-> Radarr/Sonarr with the server's quality
  rules); their status comes from Seerr (available / requested / not requested).
- Missing book formats (ebook <-> audiobook) are requested through Shelfmark:
  search its metadata provider, list releases, queue the one the user picks.
- Games: no request backend on the server; shown as links only.
"""
import json
import logging
import time
import urllib.parse

import httpx

log = logging.getLogger("omnarr.requests")

WIKIDATA = "https://query.wikidata.org/sparql"
UA = "Omnarr/0.1 (self-hosted; personal media library)"
CACHE_DAYS = 30
SEERR_STATUS = {1: "unknown", 2: "requested", 3: "requested", 4: "partial", 5: "available", 6: "blocked"}


# ── Wikidata: what was made from this book? ──────────────────────────────────
def _sparql_str(s):
    return json.dumps(s)            # JSON string escaping is valid SPARQL string escaping


def adaptations(state_con, title, author_surname):
    """[{label, kind: movie|tv|game, tmdb, igdb, year, wikidata}] for a book title + author."""
    key = f"wd:{title.lower()}|{author_surname.lower()}"
    row = state_con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    if row:
        cached = json.loads(row[0])
        if cached.get("at", 0) > time.time() - CACHE_DAYS * 86400:
            return cached["items"]
    q = f"""
SELECT DISTINCT ?work ?workLabel ?tmdbm ?tmdbt ?igdb ?year WHERE {{
  ?book rdfs:label {_sparql_str(title)}@en ; wdt:P50 ?a .
  ?a rdfs:label ?al . FILTER(LANG(?al) = "en" && CONTAINS(LCASE(?al), {_sparql_str(author_surname.lower())}))
  {{ ?work wdt:P144 ?book }} UNION {{ ?book wdt:P179 ?ser . ?work wdt:P144 ?ser }}
  OPTIONAL {{ ?work wdt:P4947 ?tmdbm }}
  OPTIONAL {{ ?work wdt:P4983 ?tmdbt }}
  OPTIONAL {{ ?work wdt:P5794 ?igdb }}
  OPTIONAL {{ ?work wdt:P577 ?d }} BIND(YEAR(?d) AS ?year)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} LIMIT 40"""
    items = []
    try:
        r = httpx.get(WIKIDATA, params={"query": q, "format": "json"}, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        seen = {}
        for b in r.json()["results"]["bindings"]:
            v = {k: x["value"] for k, x in b.items()}
            if not (v.get("tmdbm") or v.get("tmdbt") or v.get("igdb")):
                continue
            kind = "movie" if v.get("tmdbm") else ("tv" if v.get("tmdbt") else "game")
            wid = v["work"].rsplit("/", 1)[-1]
            it = seen.setdefault(wid, {"label": v.get("workLabel", ""), "kind": kind,
                                       "tmdb": v.get("tmdbm") or v.get("tmdbt"), "igdb": v.get("igdb"),
                                       "year": None, "wikidata": wid})
            y = v.get("year")
            if y and y.isdigit() and (it["year"] is None or int(y) < it["year"]):
                it["year"] = int(y)
        items = sorted(seen.values(), key=lambda x: (x["year"] or 9999))
    except Exception as e:
        log.warning("wikidata lookup failed for %r: %s", title, e)
        return []                    # don't cache failures
    with state_con:
        state_con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                          (key, json.dumps({"at": time.time(), "items": items})))
    return items


# ── Seerr ────────────────────────────────────────────────────────────────────
def _seerr(cfg):
    s = cfg.source("seerr")
    if not s:
        return None
    return httpx.Client(base_url=s["url"].rstrip("/") + "/api/v1", timeout=20,
                        headers={"X-Api-Key": s["api_key"], "Accept": "application/json"})


def seerr_details(cfg, kind, tmdb_id):
    """{title, year, poster, status, url} for a TMDB movie/tv from Seerr."""
    c = _seerr(cfg)
    if not c:
        return None
    with c:
        r = c.get(f"/{'movie' if kind == 'movie' else 'tv'}/{int(tmdb_id)}")
        if r.status_code != 200:
            return None
        d = r.json()
    mi = d.get("mediaInfo") or {}
    date = d.get("releaseDate") or d.get("firstAirDate") or ""
    base = cfg.link("seerr")
    return {
        "title": d.get("title") or d.get("name") or "", "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster": f"https://image.tmdb.org/t/p/w300{d['posterPath']}" if d.get("posterPath") else "",
        "status": SEERR_STATUS.get(mi.get("status"), "not_requested") if mi else "not_requested",
        "url": f"{base}/{'movie' if kind == 'movie' else 'tv'}/{int(tmdb_id)}" if base else "",
    }


def seerr_request(cfg, kind, tmdb_id):
    c = _seerr(cfg)
    if not c:
        raise RuntimeError("Seerr is not configured")
    body = {"mediaType": "movie" if kind == "movie" else "tv", "mediaId": int(tmdb_id)}
    if kind != "movie":
        body["seasons"] = "all"
    with c:
        r = c.post("/request", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Seerr refused the request ({r.status_code}): {r.text[:200]}")
    return {"ok": True}


def seerr_search(cfg, query):
    """Free search (for things Wikidata doesn't know). Returns movie/tv results with status."""
    c = _seerr(cfg)
    if not c:
        return []
    with c:
        # Seerr rejects '+' for spaces; encode explicitly.
        r = c.get("/search?query=" + urllib.parse.quote(query, safe="") + "&page=1")
        if r.status_code != 200:
            return []
        out = []
        for x in r.json().get("results", []):
            if x.get("mediaType") not in ("movie", "tv"):
                continue
            date = x.get("releaseDate") or x.get("firstAirDate") or ""
            mi = x.get("mediaInfo") or {}
            out.append({"kind": x["mediaType"], "tmdb": x["id"], "title": x.get("title") or x.get("name"),
                        "year": int(date[:4]) if date[:4].isdigit() else None,
                        "poster": f"https://image.tmdb.org/t/p/w300{x['posterPath']}" if x.get("posterPath") else "",
                        "status": SEERR_STATUS.get(mi.get("status"), "not_requested") if mi else "not_requested"})
        return out[:12]


# ── ROMarr (games) ───────────────────────────────────────────────────────────
_platform_cache = {"at": 0, "items": []}


def _romarr(cfg):
    s = cfg.source("romarr")
    if not s or not s.get("api_key"):
        return None
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=60,
                        headers={"X-Api-Key": s["api_key"], "Accept": "application/json"})


def romarr_enabled(cfg):
    s = cfg.source("romarr")
    return bool(s and s.get("api_key"))


def game_platforms(cfg):
    """[{slug, name}] ROMarr can fetch for (cached 1 day)."""
    if _platform_cache["items"] and _platform_cache["at"] > time.time() - 86400:
        return _platform_cache["items"]
    c = _romarr(cfg)
    if not c:
        return []
    with c:
        r = c.get("/api/platforms")
        r.raise_for_status()
        items = [{"slug": p["slug"], "name": p.get("name") or p["slug"]} for p in r.json() if p.get("slug")]
    items.sort(key=lambda p: p["name"].lower())
    _platform_cache.update(at=time.time(), items=items)
    return items


def game_request(cfg, game, platform):
    c = _romarr(cfg)
    if not c:
        raise RuntimeError("ROMarr is not configured")
    with c:
        r = c.post("/api/request", json={"game": game, "platform": platform})
    if r.status_code >= 400:
        raise RuntimeError(f"ROMarr refused ({r.status_code}): {r.text[:200]}")
    return r.json() if r.content else {"ok": True}


# ── Shelfmark (missing ebook / audiobook) ────────────────────────────────────
def _shelfmark(cfg):
    s = cfg.source("shelfmark")
    if not s or not s.get("api_key"):
        return None
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=60,
                        headers={"X-Api-Key": s["api_key"], "Accept": "application/json"})


def shelfmark_enabled(cfg):
    s = cfg.source("shelfmark")
    return bool(s and s.get("api_key"))


def book_candidates(cfg, query, content_type):
    """Shelfmark metadata search -> [{provider, book_id, title, authors, year, cover}]."""
    c = _shelfmark(cfg)
    if not c:
        raise RuntimeError("Shelfmark API key not configured")
    with c:
        r = c.get("/api/metadata/search", params={"query": query, "content_type": content_type, "limit": 25})
        r.raise_for_status()
        data = r.json()
    books = data.get("books") if isinstance(data, dict) else data
    out = []
    for b in books or []:
        out.append({"provider": b.get("provider"), "book_id": b.get("provider_id") or b.get("id"),
                    "title": b.get("title"), "authors": b.get("authors") or [],
                    "year": b.get("publish_year") or b.get("year"), "cover": b.get("cover_url") or ""})
    return out


def book_releases(cfg, provider, book_id, content_type):
    c = _shelfmark(cfg)
    if not c:
        raise RuntimeError("Shelfmark API key not configured")
    with c:
        r = c.get("/api/releases", params={"provider": provider, "book_id": book_id, "content_type": content_type},
                  timeout=120)
        r.raise_for_status()
        data = r.json()
    rel = data.get("releases") if isinstance(data, dict) else data
    return rel or []


def shelfmark_activity(cfg):
    """Shelfmark download states: {"status": {"queued"|"downloading"|"complete"|"error"|"cancelled"|...: {task_id: {...}}}}."""
    c = _shelfmark(cfg)
    if not c:
        return {}
    with c:
        r = c.get("/api/activity/snapshot")
        r.raise_for_status()
        return r.json()


def book_download(cfg, release):
    c = _shelfmark(cfg)
    if not c:
        raise RuntimeError("Shelfmark API key not configured")
    with c:
        r = c.post("/api/releases/download", json=release)
    if r.status_code >= 400:
        raise RuntimeError(f"Shelfmark refused ({r.status_code}): {r.text[:200]}")
    return r.json() if r.content else {"ok": True}
