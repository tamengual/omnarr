"""Find new scenes for the private section (behind the PIN).

Adult releases, especially for gay studios, are named however the uploader likes, so a search
typed the way a person thinks ("Sean Cody Dustin Ryder") usually returns junk, while the right
spelling ("SeanCody Dustin") finds the scene at once. So a search here:

  1. Understands it: asks StashDB (through your Stash and its saved StashDB key) which scenes,
     studio and performers the words mean.
  2. Rewrites it the ways uploaders name things: the studio as one word or a domain
     (SeanCody, SeanCody.com), performers' first names, the studio's scene code, scene-style dates.
  3. Searches every adult indexer in Prowlarr (tag "xxx") with several of those phrasings at once.
  4. Ranks what comes back by how well each release matches what you meant, and says why.

Downloading hands the release to Prowlarr, which sends it to its download client (qBittorrent).
If the Stash connection has a downloads folder, Omnarr then has Stash scan it and identify new
files against StashDB, so the messy file names don't matter.
"""
import concurrent.futures
import json
import logging
import re
import sqlite3
import time
import urllib.parse

import httpx

log = logging.getLogger("omnarr.adultfind")
STOP = {"the", "and", "a", "an", "of", "with", "in", "on", "to", "for", "his", "her", "my", "&", "vs", "x"}
MAX_QUERIES = 5


# ── helpers ──────────────────────────────────────────────────────────────────
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def compact(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _words(s):
    return [w for w in norm(s).split() if w not in STOP]


def studio_forms(name):
    """How uploaders write a studio: 'Sean Cody' -> SeanCody, SeanCody.com, sean cody, (initials SC)."""
    name = re.sub(r"\.(com|net|tv)$", "", (name or "").strip(), flags=re.I)
    if not name:
        return []
    one = re.sub(r"[^A-Za-z0-9]+", "", name)
    forms = [one, f"{one}.com", name]
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    if len(words) >= 3:
        forms.append("".join(w[0] for w in words).upper())
    return list(dict.fromkeys(f for f in forms if f))


def first_names(performers):
    return [p.split()[0] for p in performers if p and p.split()]


# ── 1. understand the search (StashDB through Stash) ─────────────────────────
SCRAPE = """query($t:String!,$e:String!){ scrapeSingleScene(source:{stash_box_endpoint:$e}, input:{query:$t}) {
  title code date details image urls remote_site_id studio { name remote_site_id } performers { name } } }"""
BOXES = "query{ configuration { general { stashBoxes { endpoint name } } } }"


def _stash(cfg, timeout=60):
    s = cfg.source("stash") or {}
    if not (s.get("url") and s.get("api_key")):
        return None
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=timeout, headers={"ApiKey": s["api_key"]})


def stashbox_endpoint(c):
    r = c.post("/graphql", json={"query": BOXES})
    boxes = ((r.json().get("data") or {}).get("configuration") or {}).get("general", {}).get("stashBoxes") or []
    return next((b["endpoint"] for b in boxes if "stashdb" in b["endpoint"]), boxes[0]["endpoint"] if boxes else None)


def understand(cfg, q, limit=8):
    """Scenes StashDB thinks the words mean: [{id, title, code, date, studio, performers, image, url}]."""
    c = _stash(cfg)
    if not c or not q.strip():
        return []
    with c:
        endpoint = stashbox_endpoint(c)
        if not endpoint:
            return []
        r = c.post("/graphql", json={"query": SCRAPE, "variables": {"t": q.strip(), "e": endpoint}})
    out = []
    for sc in ((r.json().get("data") or {}).get("scrapeSingleScene") or [])[:limit]:
        image = sc.get("image") or ""
        out.append({"id": sc.get("remote_site_id") or "", "title": sc.get("title") or "", "code": sc.get("code") or "",
                    "date": sc.get("date") or "", "studio": (sc.get("studio") or {}).get("name") or "",
                    "studio_id": (sc.get("studio") or {}).get("remote_site_id") or "",
                    "performers": [p["name"] for p in sc.get("performers") or []],
                    "image": image if image.startswith(("http", "data:image")) and len(image) < 600_000 else "",
                    "url": (sc.get("urls") or [""])[0]})
    return out


# ── 2. rewrite it the ways uploaders name things ─────────────────────────────
def phrasings(q, scene=None, known_studios=()):
    """Up to MAX_QUERIES search strings, best first."""
    out = []
    if scene:
        forms = studio_forms(scene.get("studio"))
        names = first_names(scene.get("performers") or [])
        title = " ".join(w for w in norm(re.sub(r":.*$", "", scene.get("title") or "")).split() if w not in STOP)
        if forms and names:
            out.append(f"{forms[0]} {' '.join(names[:3])}")
        if scene.get("code") and forms:
            out.append(f"{forms[0]} {scene['code']}")
        if len(names) >= 2:
            out.append(" ".join(names[:3]))
        if forms and title:
            out.append(f"{forms[0]} {title}")
        if forms and re.match(r"\d{4}-\d{2}-\d{2}", scene.get("date") or ""):
            y, m, d = scene["date"].split("-")
            out.append(f"{forms[0]} {y[2:]}.{m}.{d}")
        if not out and title:
            out.append(title)
    # the words as typed, with a studio name glued together the way uploaders write it
    typed = " ".join(q.split())
    if typed:
        glued = typed
        for studio in sorted(known_studios, key=len, reverse=True):
            if " " in studio and norm(studio) in norm(typed):
                glued = re.sub(re.escape(studio), studio_forms(studio)[0], typed, flags=re.I)
                rest = norm(re.sub(re.escape(studio), " ", typed, flags=re.I))
                if glued != typed:
                    out.append(glued)
                if rest and len(rest.split()) >= 2:
                    out.append(rest)
                break
        out.append(typed)
    return list(dict.fromkeys(x.strip() for x in out if x.strip()))[:MAX_QUERIES]


# ── 3. search Prowlarr's adult indexers ──────────────────────────────────────
def _prowlarr(cfg, timeout=120):
    s = cfg.source("prowlarr")
    return httpx.Client(base_url=s["url"].rstrip("/") + "/api/v1", timeout=timeout, headers={"X-Api-Key": s["api_key"]})


def adult_indexers(cfg):
    """[(id, name)] of enabled indexers tagged xxx (or offering the XXX category when nothing is tagged)."""
    with _prowlarr(cfg, timeout=30) as c:
        tags = {t["id"]: t["label"].lower() for t in c.get("/tag").json()}
        idx = [i for i in c.get("/indexer").json() if i.get("enable")]
    tagged = [i for i in idx if any(tags.get(t) == "xxx" for t in i.get("tags") or [])]
    if not tagged:
        tagged = [i for i in idx if any(cat.get("id") == 6000 for cat in (i.get("capabilities") or {}).get("categories") or [])]
    return [(i["id"], i["name"]) for i in tagged]


def _search(cfg, query, ids):
    with _prowlarr(cfg) as c:
        params = [("query", query), ("type", "search"), ("limit", "100")] + [("indexerIds", str(i)) for i in ids]
        r = c.get("/search?" + urllib.parse.urlencode(params))
        r.raise_for_status()
        return r.json()


# ── 4. rank by what you meant ────────────────────────────────────────────────
def _has_studio(t, tc, name):
    return bool(name) and (any(compact(f) and compact(f) in tc for f in studio_forms(name)[:2]) or norm(name) in t)


def score(title, q, scene=None, studios=()):
    """(points, reasons, on_target) for one release title. on_target: it's from the studio you meant
    (or carries the scene's code); anything else can't be a match however many words it shares."""
    t, tc = norm(title), compact(title)
    pts, why, on_target = 0, [], False
    if scene:
        if _has_studio(t, tc, scene.get("studio")):
            pts += 4
            on_target = True
            why.append(scene["studio"])
        code = scene.get("code") or ""
        digits = re.sub(r"\D", "", code)
        if code and (compact(code) in tc or (len(digits) >= 3 and re.search(rf"(?<!\d){digits}(?!\d)", t))):
            pts += 5
            on_target = True
            why.append(f"scene {scene['code']}")
        if not scene.get("studio"):
            on_target = True
        for p in scene.get("performers") or []:
            if norm(p) and norm(p) in t:
                pts += 3
                why.append(p)
            elif p.split() and len(p.split()[0]) > 2 and re.search(rf"\b{re.escape(norm(p.split()[0]))}\b", t):
                pts += 2
                why.append(p.split()[0])
        tw = [w for w in _words(re.sub(r":", " ", scene.get("title") or "")) if len(w) > 2]
        hits = [w for w in tw if re.search(rf"\b{re.escape(w)}\b", t)]
        pts += min(len(hits), 4)
        d = scene.get("date") or ""
        if re.match(r"\d{4}-\d{2}-\d{2}", d):
            y, m, dd = d.split("-")
            if f"{y[2:]} {m} {dd}" in t or f"{y} {m} {dd}" in t:
                pts += 3
                why.append(d)
            elif re.search(rf"\b{y}\b", t):
                pts += 1
    else:
        named = [s for s in studios if s and norm(s) and norm(s) in norm(q)]
        rest = norm(q)
        for s in named:                          # the words of a studio's name don't count on their own
            rest = rest.replace(norm(s), " ")
            if _has_studio(t, tc, s):
                pts += 6
                on_target = True
                why.append(s)
        words = [w for w in _words(rest) if len(w) > 1]
        hits = [w for w in words if re.search(rf"\b{re.escape(w)}\b", t) or (len(w) > 3 and w in tc)]
        pts += 2 * len(hits)
        if hits:
            why.append(", ".join(hits[:4]))
        if named and not on_target:
            pts = min(pts, 1)                    # you named a studio; another studio's release isn't it
        on_target = on_target or not named
    return pts, why, on_target


def find(cfg, q, scene=None):
    """{"queries": [...], "indexers": [...], "results": [...], "errors": [...]}."""
    ids = adult_indexers(cfg)
    if not ids:
        return {"queries": [], "indexers": [], "results": [], "errors": ["No adult indexers in Prowlarr (tag them xxx)"]}
    studios = {scene["studio"]} if scene and scene.get("studio") else set()
    if not scene:
        try:
            studios |= {s["studio"] for s in understand(cfg, q, limit=5) if s["studio"]}
        except Exception as e:
            log.info("stashdb lookup failed: %s", e)
    queries = phrasings(q, scene, studios)
    seen, results, errors = {}, [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries) or 1) as pool:
        futures = {pool.submit(_search, cfg, query, [i for i, _ in ids]): query for query in queries}
        for f in concurrent.futures.as_completed(futures):
            try:
                found = f.result()
            except Exception as e:
                errors.append(f"{futures[f]}: {type(e).__name__}")
                continue
            for rel in found:
                key = rel.get("infoHash") or rel.get("guid")
                if key in seen:
                    seen[key]["found_by"].add(futures[f])
                    continue
                pts, why, on_target = score(rel.get("title", ""), q, scene, studios)
                item = {"guid": rel.get("guid"), "indexer_id": rel.get("indexerId"), "indexer": rel.get("indexer"),
                        "title": rel.get("title", ""), "size": rel.get("size") or 0, "seeders": rel.get("seeders"),
                        "published": (rel.get("publishDate") or "")[:10], "info_url": rel.get("infoUrl") or "",
                        "protocol": rel.get("protocol"), "score": pts, "why": why, "on_target": on_target,
                        "found_by": {futures[f]}}
                seen[key] = item
                results.append(item)
    best = max((r["score"] for r in results if r["on_target"]), default=0)
    floor = max(4, best * 0.5) if scene else max(2, best * 0.5)
    for r in results:
        r["match"] = bool(r.pop("on_target")) and r["score"] >= floor and r["score"] > 0
        r["found_by"] = sorted(r["found_by"])
    results.sort(key=lambda r: (-r["match"], -r["score"], -(r["seeders"] or 0)))
    return {"queries": queries, "indexers": [n for _, n in ids], "results": results[:80], "errors": errors}


# ── download, then let Stash identify it ─────────────────────────────────────
GRABS = "CREATE TABLE IF NOT EXISTS adult_grabs (guid TEXT PRIMARY KEY, title TEXT, account_id INTEGER, at REAL)"


def grab(cfg, state_path, account_id, guid, indexer_id, title=""):
    with _prowlarr(cfg, timeout=60) as c:
        if not c.get("/downloadclient").json():
            raise RuntimeError("Prowlarr has no download client yet. Add qBittorrent in Prowlarr → Settings → "
                               "Download Clients (category: stash), then try again")
        r = c.post("/search", json={"guid": guid, "indexerId": int(indexer_id)})
    if r.status_code >= 400:
        try:
            msg = r.json()[0].get("errorMessage") if isinstance(r.json(), list) else r.json().get("message")
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"Prowlarr couldn't send it to the download client: {msg or r.status_code}")
    con = sqlite3.connect(state_path, timeout=15)
    with con:
        con.execute(GRABS)
        con.execute("INSERT OR REPLACE INTO adult_grabs VALUES (?,?,?,?)", (guid, title[:300], account_id, time.time()))
    con.close()
    return True


def pending(state_path, days=2):
    con = sqlite3.connect(state_path, timeout=15)
    try:
        con.execute(GRABS)
        return con.execute("SELECT COUNT(*) FROM adult_grabs WHERE at > ?", (time.time() - days * 86400,)).fetchone()[0]
    finally:
        con.close()


def scan_and_identify(cfg):
    """Have Stash pick up new downloads and match them on StashDB (by video fingerprint)."""
    s = cfg.source("stash") or {}
    path = (s.get("downloads_path") or "").strip()
    c = _stash(cfg)
    if not path or not c:
        return False
    with c:
        endpoint = stashbox_endpoint(c)
        c.post("/graphql", json={"query": "mutation($p:[String!]){ metadataScan(input:{paths:$p}) }", "variables": {"p": [path]}})
        if endpoint:
            c.post("/graphql", json={"query": """mutation($p:[String!],$e:String!){ metadataIdentify(input:{
                       sources:[{source:{stash_box_endpoint:$e}}], paths:$p}) }""", "variables": {"p": [path], "e": endpoint}})
    return True


def recent(state_path, limit=20):
    con = sqlite3.connect(state_path, timeout=15)
    try:
        con.execute(GRABS)
        return [{"title": t, "at": at} for t, at in con.execute("SELECT title, at FROM adult_grabs ORDER BY at DESC LIMIT ?", (limit,))]
    finally:
        con.close()


# ── follow studios and performers: new scenes download on their own ─────────
FOLLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS adult_follows (kind TEXT, id TEXT, name TEXT, added REAL, PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS adult_wanted (scene_id TEXT PRIMARY KEY, scene TEXT, follow TEXT, status TEXT,
  tries INTEGER DEFAULT 0, last_try REAL DEFAULT 0, added REAL, release TEXT);
"""
LOOKBACK_DAYS = 14        # following something also picks up its last two weeks of scenes
GIVE_UP_DAYS = 45         # stop looking for a scene this long after it came out
MIN_SCORE = 9             # auto-download only clear matches: studio + performers, or the scene code
MAX_GRABS_PER_RUN = 6
MAX_SEARCHES_PER_RUN = 12


def _fcon(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(FOLLOW_SCHEMA)
    return con


def _stashdb(cfg):
    """A client for StashDB itself, using the key saved in Stash's settings (never stored here)."""
    c = _stash(cfg)
    if not c:
        return None
    with c:
        r = c.post("/graphql", json={"query": "query{ configuration { general { stashBoxes { endpoint api_key } } } }"})
    boxes = ((r.json().get("data") or {}).get("configuration") or {}).get("general", {}).get("stashBoxes") or []
    box = next((b for b in boxes if "stashdb" in b["endpoint"]), boxes[0] if boxes else None)
    if not box or not box.get("api_key"):
        return None
    return httpx.Client(base_url=box["endpoint"], timeout=60, headers={"ApiKey": box["api_key"]})


def _gql(c, query, variables=None):
    r = c.post("", json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"][0].get("message", "StashDB error"))
    return body["data"]


def lookup(cfg, term):
    """Studios and performers on StashDB matching a name, to follow."""
    c = _stashdb(cfg)
    if not c or not term.strip():
        return {"studios": [], "performers": []}
    with c:
        d = _gql(c, """query($t:String!){ searchStudio(term:$t, limit:6){ id name aliases parent { name } }
                         searchPerformer(term:$t, limit:8){ id name disambiguation aliases gender scene_count } }""",
                 {"t": term.strip()})
    return {"studios": [{"id": s["id"], "name": s["name"], "note": (s.get("parent") or {}).get("name") or ""}
                        for s in d.get("searchStudio") or []],
            "performers": [{"id": p["id"], "name": p["name"],
                            "note": " · ".join(x for x in (p.get("disambiguation") or "", f"{p['scene_count']} scenes"
                                                           if p.get("scene_count") else "") if x)}
                           for p in d.get("searchPerformer") or []]}


def follows(state_path):
    con = _fcon(state_path)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM adult_follows ORDER BY name COLLATE NOCASE")]
    finally:
        con.close()


def follow(state_path, kind, fid, name):
    if kind not in ("studio", "performer") or not fid:
        raise ValueError("kind must be studio or performer")
    con = _fcon(state_path)
    with con:
        con.execute("INSERT OR IGNORE INTO adult_follows VALUES (?,?,?,?)", (kind, fid, name[:200], time.time()))
    con.close()


def unfollow(state_path, kind, fid):
    con = _fcon(state_path)
    with con:
        con.execute("DELETE FROM adult_follows WHERE kind=? AND id=?", (kind, fid))
        con.execute("DELETE FROM adult_wanted WHERE follow=? AND status='waiting'", (f"{kind}:{fid}",))
    con.close()


def wanted_list(state_path, limit=60):
    con = _fcon(state_path)
    try:
        out = []
        for r in con.execute("SELECT * FROM adult_wanted ORDER BY added DESC LIMIT ?", (limit,)):
            sc = json.loads(r["scene"])
            out.append({"scene_id": r["scene_id"], "title": sc.get("title"), "studio": sc.get("studio"),
                        "performers": sc.get("performers"), "date": sc.get("date"), "status": r["status"],
                        "tries": r["tries"], "release": r["release"] or ""})
        return out
    finally:
        con.close()


SCENES_BY = """query($id:ID!){ queryScenes(input:{%s:{value:[$id], modifier:INCLUDES}, sort:DATE, direction:DESC, per_page:25}) {
  scenes { id title code date studio { name } performers { performer { name } } } } }"""


def _new_scenes(c, kind, fid, since):
    d = _gql(c, SCENES_BY % ("studios" if kind == "studio" else "performers"), {"id": fid})
    out = []
    for s in (d.get("queryScenes") or {}).get("scenes") or []:
        if (s.get("date") or "") < since:
            continue
        out.append({"id": s["id"], "title": s.get("title") or "", "code": s.get("code") or "", "date": s.get("date") or "",
                    "studio": (s.get("studio") or {}).get("name") or "",
                    "performers": [p["performer"]["name"] for p in s.get("performers") or []]})
    return out


def _owned(cfg, scene_id, endpoint):
    c = _stash(cfg, timeout=30)
    with c:
        r = c.post("/graphql", json={"query": """query($id:String!,$e:String!){ findScenes(scene_filter:{
                     stash_id_endpoint:{stash_id:$id, endpoint:$e, modifier:EQUALS}}){ count } }""",
                                     "variables": {"id": scene_id, "e": endpoint}})
    return ((r.json().get("data") or {}).get("findScenes") or {}).get("count", 0) > 0


def best_release(found):
    """The release to download automatically, or None when nothing is a clear match."""
    good = [r for r in found["results"] if r["match"] and r["score"] >= MIN_SCORE and (r["seeders"] or 0) > 0]
    return max(good, key=lambda r: (r["score"], r["seeders"] or 0)) if good else None


def check_follows(cfg, state_path, audit=lambda *a: None):
    """Add new scenes from followed studios/performers, then try to download the ones that are due."""
    fl = follows(state_path)
    if not fl or not cfg.source("prowlarr"):
        return {"new": 0, "grabbed": 0}
    c = _stashdb(cfg)
    if not c:
        return {"new": 0, "grabbed": 0, "error": "Stash has no StashDB key"}
    endpoint = str(c.base_url).rstrip("/")
    now, new = time.time(), 0
    con = _fcon(state_path)
    with c:
        for f in fl:
            since = time.strftime("%Y-%m-%d", time.gmtime(f["added"] - LOOKBACK_DAYS * 86400))
            try:
                scenes = _new_scenes(c, f["kind"], f["id"], since)
            except Exception as e:
                log.warning("stashdb scenes for %s failed: %s", f["name"], e)
                continue
            with con:
                for s in scenes:
                    cur = con.execute("INSERT OR IGNORE INTO adult_wanted (scene_id, scene, follow, status, added) VALUES (?,?,?,?,?)",
                                      (s["id"], json.dumps(s), f"{f['kind']}:{f['id']}", "waiting", now))
                    new += cur.rowcount
    grabbed = searched = 0
    due = [r for r in con.execute("SELECT * FROM adult_wanted WHERE status='waiting' ORDER BY added")]
    for r in due:
        if searched >= MAX_SEARCHES_PER_RUN or grabbed >= MAX_GRABS_PER_RUN:
            break
        scene = json.loads(r["scene"])
        tries, wait = r["tries"], (6 * 3600 if r["tries"] < 4 else 24 * 3600)
        if now - r["last_try"] < wait:
            continue
        released = time.mktime(time.strptime(scene["date"], "%Y-%m-%d")) if scene.get("date") else r["added"]
        if now - released > GIVE_UP_DAYS * 86400 and tries > 0:
            with con:
                con.execute("UPDATE adult_wanted SET status='not_found' WHERE scene_id=?", (r["scene_id"],))
            continue
        try:
            if _owned(cfg, r["scene_id"], endpoint):
                with con:
                    con.execute("UPDATE adult_wanted SET status='have' WHERE scene_id=?", (r["scene_id"],))
                continue
            searched += 1
            pick = best_release(find(cfg, "", scene))
            if pick:
                grab(cfg, state_path, None, pick["guid"], pick["indexer_id"], pick["title"])
                grabbed += 1
                audit("adult_follow_grab", {"scene": scene.get("title"), "studio": scene.get("studio")}, pick["title"][:200])
            with con:
                con.execute("UPDATE adult_wanted SET status=?, tries=tries+1, last_try=?, release=? WHERE scene_id=?",
                            ("downloading" if pick else "waiting", now, pick["title"] if pick else None, r["scene_id"]))
        except Exception as e:
            log.warning("follow search for %s failed: %s", scene.get("title"), e)
            with con:
                con.execute("UPDATE adult_wanted SET tries=tries+1, last_try=? WHERE scene_id=?", (now, r["scene_id"]))
    con.close()
    return {"new": new, "grabbed": grabbed, "searched": searched}