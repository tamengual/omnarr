"""Requests ("Get it") from a library item.

- Related works (any kind): found on Wikidata from the work's external ids (shows/movies)
  or title + author (books): same media franchise or series, works based on it, and what
  it is based on. Cached in the state DB.
- Movies/TV are requested through Seerr (-> Radarr/Sonarr with the server's quality
  rules); their status comes from Seerr (available / requested / not requested).
- Missing book formats (ebook <-> audiobook) are requested through Shelfmark:
  search its metadata provider, list releases, queue the one the user picks.
- Games: no request backend on the server; shown as links only.
"""
import json
import logging
import re
import time
import urllib.parse

import httpx

log = logging.getLogger("omnarr.requests")

WIKIDATA = "https://query.wikidata.org/sparql"
UA = "Omnarr/0.2 (https://github.com/tamengual/omnarr; self-hosted media library)"
CACHE_DAYS = 30
SEERR_STATUS = {1: "unknown", 2: "requested", 3: "requested", 4: "partial", 5: "available", 6: "blocked"}


# ── Wikidata: everything related to a work ───────────────────────────────────
RELATED_CACHE_DAYS = 14
PER_KIND = 24                       # cap per kind so huge franchises (Star Wars…) stay readable


def _sparql_str(s):
    return json.dumps(s)            # JSON string escaping is valid SPARQL string escaping


def seed_from_ids(kind, ids):
    """SPARQL clause binding ?seed from a show/movie's external ids, or None."""
    props = {"show": (("tmdb", "P4983"), ("tvdb", "P4835"), ("imdb", "P345")),
             "movie": (("tmdb", "P4947"), ("imdb", "P345"))}.get(kind, ())
    parts = []
    for key, prop in props:
        v = str(ids.get(key) or "").strip()
        if re.fullmatch(r"\d{1,9}|tt\d{5,10}", v):
            parts.append(f"{{ ?seed wdt:{prop} {_sparql_str(v)} }}")
    return " UNION ".join(parts) or None


def seed_from_book(title, author_surname):
    """SPARQL clause binding ?seed to a book by English title + author surname.
    Wikidata labels often use a typographic apostrophe, so both spellings are tried."""
    variants = {title, title.replace("'", "\u2019"), title.replace("\u2019", "'")}
    values = " ".join(f"{_sparql_str(v)}@{lang}" for v in sorted(variants) for lang in ("en", "mul"))
    return (f"VALUES ?seedLabel {{ {values} }} ?seed rdfs:label ?seedLabel ; wdt:P50 ?a . ?a rdfs:label ?al . "
            f"FILTER(LANG(?al) IN (\"en\", \"mul\") && CONTAINS(LCASE(?al), {_sparql_str(author_surname.lower())}))")


NOT_MEDIA = ("character", "fictional", "season", "episode", "soundtrack", "album", "song",
             "franchise", "award", "toy", "board game", "card game", "tabletop", "role-playing game system",
             "theme park", "attraction", "musical work", "stage play")


def _classify(types, has):
    """movie|tv|game|book|comic, or None for things that aren't media (franchise items,
    characters, writing systems, soundtracks…) and for series-of-books entries."""
    if has.get("tmdbm"):
        return "movie"
    if has.get("tmdbt"):
        return "tv"
    if has.get("igdb"):
        return "game"
    # drop things that merely belong to the world: characters, seasons, episodes, music,
    # tabletop games, awards…
    ts = [x.lower() for x in types if not any(n in x.lower() for n in NOT_MEDIA)]
    if not ts:
        return None
    t = " | ".join(ts)
    tv_words = ("television series", "miniseries", "web series", "television program", "anime")
    if all("series" in x or "franchise" in x or "collection" in x for x in ts) and not any(k in t for k in tv_words):
        return None                 # a film series / novel series / comic series: its members are listed
    if "video game" in t:
        return "game"
    if "film" in t:
        return "movie"
    if any(k in t for k in tv_words):
        return "tv"
    if any(k in t for k in ("comic", "graphic novel", "manga")):
        return "comic"
    if any(k in t for k in ("novel", "literary work", "book", "novella", "short story")):
        return "book"
    return None


def related(state_con, seed_clause, cache_key):
    """[{label, kind, tmdb, igdb, year, wikidata, authors, url}] related to the seed work:
    same franchise / same series, works based on it, and what it is based on."""
    key = "wdr2:" + cache_key                     # v2: translations/editions filtered out
    row = state_con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    if row:
        cached = json.loads(row[0])
        if cached.get("at", 0) > time.time() - RELATED_CACHE_DAYS * 86400:
            return cached["items"]
    q = f"""
SELECT ?seed ?item ?itemLabel ?typeLabel ?formLabel ?genreLabel ?tmdbm ?tmdbt ?igdb ?d ?authorLabel ?article WHERE {{
  {seed_clause}
  {{ ?seed wdt:P8345|wdt:P179 ?fr . ?item wdt:P8345|wdt:P179 ?fr . }}
  UNION {{ ?item wdt:P144 ?seed }}
  UNION {{ ?seed wdt:P144 ?item }}
  UNION {{ ?seed wdt:P179 ?ser . ?item wdt:P144 ?ser }}
  FILTER(?item != ?seed)
  OPTIONAL {{ ?item wdt:P31 ?type }}
  OPTIONAL {{ ?item wdt:P7937 ?form }}
  OPTIONAL {{ ?item wdt:P136 ?genre }}
  OPTIONAL {{ ?item wdt:P4947 ?tmdbm }}
  OPTIONAL {{ ?item wdt:P4983 ?tmdbt }}
  OPTIONAL {{ ?item wdt:P5794 ?igdb }}
  OPTIONAL {{ ?item wdt:P577|wdt:P580 ?d }}
  OPTIONAL {{ ?item wdt:P50 ?author }}
  OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
}} LIMIT 1500"""
    try:
        r = httpx.get(WIKIDATA, params={"query": q, "format": "json"}, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        items = parse_related(r.json())
    except Exception as e:
        log.warning("wikidata related lookup failed for %s: %s", cache_key, e)
        with state_con:              # retry tomorrow, not on every page view
            state_con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                              (key, json.dumps({"at": time.time() - (RELATED_CACHE_DAYS - 1) * 86400, "items": []})))
        return []
    with state_con:
        state_con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                          (key, json.dumps({"at": time.time(), "items": items})))
    return items


def parse_related(data):
    seeds = {b["seed"]["value"] for b in data["results"]["bindings"] if "seed" in b}
    agg = {}
    for b in data["results"]["bindings"]:
        v = {k: x["value"] for k, x in b.items()}
        if v["item"] in seeds:
            continue
        wid = v["item"].rsplit("/", 1)[-1]
        a = agg.setdefault(wid, {"label": v.get("itemLabel", ""), "types": set(), "genres": set(), "authors": [], "years": set(),
                                 "tmdbm": None, "tmdbt": None, "igdb": None, "article": None})
        for k in ("typeLabel", "formLabel"):
            if v.get(k):
                a["types"].add(v[k])
        if v.get("genreLabel"):
            a["genres"].add(v["genreLabel"].lower())
        if "comic" in (v.get("genreLabel") or "").lower() or "graphic novel" in (v.get("genreLabel") or "").lower():
            a["types"].add(v["genreLabel"])
        for k in ("tmdbm", "tmdbt", "igdb", "article"):
            a[k] = a[k] or v.get(k)
        if v.get("authorLabel") and v["authorLabel"] not in a["authors"]:
            a["authors"].append(v["authorLabel"])
        if (v.get("d") or "")[:4].isdigit():
            a["years"].add(int(v["d"][:4]))
    items, per_kind = [], {}
    for wid, a in agg.items():
        kind = _classify(a["types"], a)
        if any("parody" in g or "fan fiction" in g for g in a["genres"]):
            continue
        if any("edition" in x.lower() or "translation" in x.lower() for x in a["types"]):
            continue                              # a translation/edition of the seed, not a new work
        if not kind or re.fullmatch(r"Q\d+", a["label"]):          # unlabelled items are noise
            continue
        items.append({"label": a["label"], "kind": kind, "tmdb": a["tmdbm"] or a["tmdbt"], "igdb": a["igdb"],
                      "year": min(a["years"]) if a["years"] else None, "wikidata": wid, "authors": a["authors"],
                      "url": a["article"] or f"https://www.wikidata.org/wiki/{wid}"})
    items.sort(key=lambda x: (x["year"] or 9999, x["label"]))
    out = []
    for it in items:
        per_kind[it["kind"]] = per_kind.get(it["kind"], 0) + 1
        if per_kind[it["kind"]] <= PER_KIND:
            out.append(it)
    return out


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


def seerr_recommendations(cfg, state_con, kind, tmdb_id, days=7):
    """TMDB's "recommended" titles for a movie/show, through Seerr; cached for `days`.
    -> [{kind: movie|tv, tmdb, title, year, poster, status}]"""
    path = "movie" if kind == "movie" else "tv"
    key = f"srec:{path}:{int(tmdb_id)}"
    row = state_con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    if row:
        cached = json.loads(row[0])
        if cached.get("at", 0) > time.time() - days * 86400:
            return cached["items"]
    c = _seerr(cfg)
    if not c:
        return []
    items = []
    try:
        with c:
            r = c.get(f"/{path}/{int(tmdb_id)}/recommendations", params={"page": 1})
        if r.status_code == 200:
            for x in r.json().get("results", [])[:20]:
                mt = x.get("mediaType") or path
                if mt not in ("movie", "tv"):
                    continue
                date = x.get("releaseDate") or x.get("firstAirDate") or ""
                mi = x.get("mediaInfo") or {}
                items.append({"kind": mt, "tmdb": x["id"], "title": x.get("title") or x.get("name") or "",
                              "year": int(date[:4]) if date[:4].isdigit() else None,
                              "poster": f"https://image.tmdb.org/t/p/w300{x['posterPath']}" if x.get("posterPath") else "",
                              "status": SEERR_STATUS.get(mi.get("status"), "not_requested") if mi else "not_requested"})
    except Exception as e:
        log.warning("seerr recommendations failed for %s: %s", key, e)
        return []
    with state_con:
        state_con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps({"at": time.time(), "items": items})))
    return items


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
