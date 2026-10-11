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

# Scene types you can type ("group", "dp", "animated"...): the words that pick them out, then the ways
# uploaders tag them (PornoLab lists them in the title: "[2015, Anal Sex, Bareback, Group Sex]").
# The first tag is what gets searched for.
SCENE_TYPES = {
    "solo": (["solo", "jerk off", "jack off", "jo"], ["solo", "masturbation", "jerk off", "jack off"]),
    "threesome": (["threesome", "3some", "three way", "threeway"], ["threesome", "3some", "three way", "threeway", "mmm"]),
    "group": (["group", "orgy", "foursome", "4some"], ["group sex", "orgy", "group", "foursome", "4some"]),
    "gangbang": (["gangbang", "gang bang", "gang-bang"], ["gang bang", "gangbang", "gang-bang"]),
    "dp": (["dp", "double penetration", "double anal", "dap"], ["double penetration", "dp", "double anal", "dap"]),
    "bareback": (["bareback", "raw", "bb", "condomless"], ["bareback", "raw", "condomless"]),
    "creampie": (["creampie", "breeding", "cum inside"], ["creampie", "breeding", "cum inside", "internal cumshot"]),
    "oral": (["oral", "blowjob", "bj", "sucking"], ["blowjob", "oral", "cocksucking", "sucking"]),
    "rimming": (["rimming", "rimjob", "rim"], ["rimming", "rimjob", "ass eating"]),
    "fisting": (["fisting", "fist"], ["fisting"]),
    "bdsm": (["bdsm", "bondage", "kink", "leather"], ["bdsm", "bondage", "fetish", "leather", "kink"]),
    "feet": (["feet", "foot"], ["foot fetish", "feet"]),
    "interracial": (["interracial", "bbc"], ["interracial", "bbc"]),
    "twink": (["twink", "twinks"], ["twink", "twinks", "young men"]),
    "bear": (["bear", "bears"], ["bear", "bears", "hairy"]),
    "daddy": (["daddy", "dad", "older"], ["daddy", "older/younger", "older younger", "age gap", "mature"]),
    "muscle": (["muscle", "muscular", "jock", "jocks"], ["muscle", "muscular", "muscles", "jock", "bodybuilder"]),
    "big dick": (["big dick", "big cock", "huge cock", "hung"], ["big dick", "big cock", "big dicks", "huge cock", "hung"]),
    "amateur": (["amateur", "homemade"], ["amateur", "homemade"]),
    "straight": (["straight guy", "gay for pay", "str8", "first time", "bait"], ["straight", "gay for pay", "str8", "first time", "bait"]),
    "military": (["military", "army", "soldier", "marine"], ["military", "army", "soldier", "uniform"]),
    "outdoor": (["outdoor", "outdoors", "public"], ["outdoor", "outdoors", "public"]),
    "massage": (["massage"], ["massage"]),
    "flip": (["flip", "flip fuck", "versatile"], ["flip fuck", "flip-flop", "flip flop", "versatile"]),
    "animated": (["animated", "animation", "cartoon", "hentai", "3d", "yaoi", "bara"],
                 ["animation", "animated", "cartoon", "3d", "cgi", "hentai", "yaoi", "bara"]),
    "vr": (["vr", "virtual reality"], ["vr", "virtual reality", "180", "360"]),
    "compilation": (["compilation", "best of"], ["compilation", "best of"]),
    "full movie": (["full movie", "feature", "dvd", "movie"], ["full length", "feature", "dvd", "movie"]),
}


def extract_types(q):
    """(scene types named in the words, the other words): "fratx group dp" -> (["group", "dp"], "fratx")."""
    words, found, i = norm(q).split(), [], 0
    rest = []
    phrases = sorted(((w.split(), t) for t, (says, _) in SCENE_TYPES.items() for w in says), key=lambda x: -len(x[0]))
    while i < len(words):
        for ph, t in phrases:
            if words[i:i + len(ph)] == ph:
                if t not in found:
                    found.append(t)
                i += len(ph)
                break
        else:
            rest.append(words[i])
            i += 1
    return found, " ".join(rest)


def type_hits(title, types):
    """The requested scene types a release is tagged with."""
    t = norm(title)
    return [ty for ty in types if any(re.search(rf"\b{re.escape(norm(tag))}\b", t) for tag in SCENE_TYPES[ty][1])]
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


def split_studio(q, studio):
    """(True, other words) when the words typed spell the studio, spaces or not
    ("fraternity x" / "FraternityX", "sean cody" / "SeanCody"); else (False, all words)."""
    words, target = norm(q).split(), compact(studio)
    for i in range(len(words)):
        joined = ""
        for j in range(i, len(words)):
            joined += words[j]
            if joined == target:
                return True, words[:i] + words[j + 1:]
            if len(joined) >= len(target):
                break
    return False, words


def first_names(performers):
    return [p.split()[0] for p in performers if p and p.split()]


# ── 1. understand the search (StashDB through Stash) ─────────────────────────
SCRAPE = """query($t:String!,$e:String!){ scrapeSingleScene(source:{stash_box_endpoint:$e}, input:{query:$t}) {
  title code date details image urls remote_site_id studio { name remote_site_id } performers { name gender } } }"""
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


MEN = {"MALE", "TRANSGENDER_MALE"}


def all_male(performers):
    """True unless StashDB lists a performer who isn't a man (unknown genders don't count against it)."""
    genders = [(p.get("gender") or "").upper() for p in performers or []]
    return not any(g and g not in MEN for g in genders)


def understand(cfg, q, limit=8, gay_only=False):
    """Scenes StashDB thinks the words mean: [{id, title, code, date, studio, performers, image, url}].
    gay_only: only scenes where every performer StashDB knows is a man. Scene types ("group", "dp")
    are left out of the lookup; they narrow the release search instead."""
    q = extract_types(q)[1]
    c = _stash(cfg)
    if not c or not q.strip():
        return []
    with c:
        endpoint = stashbox_endpoint(c)
        if not endpoint:
            return []
        r = c.post("/graphql", json={"query": SCRAPE, "variables": {"t": q.strip(), "e": endpoint}})
    out = []
    scenes = (r.json().get("data") or {}).get("scrapeSingleScene") or []
    if gay_only:
        scenes = [sc for sc in scenes if all_male(sc.get("performers"))]
    for sc in scenes[:limit]:
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
        nick = next((a for a in scene.get("studio_aliases") or [] if " " not in a and "." not in a
                     and len(compact(a)) >= 4 and compact(a) != compact(scene.get("studio"))), "")
        if forms and names:
            out.append(f"{forms[0]} {' '.join(names[:3])}")
            if nick:
                out.append(f"{nick} {' '.join(names[:3])}")          # FratX scenes are posted as FraternityX
        if scene.get("code") and forms:
            out.append(f"{forms[0]} {scene['code']}")
        if len(names) >= 2:
            out.append(" ".join(names[:3]))
        if forms and title:
            out.append(f"{forms[0]} {title}")
            if nick and not names:
                out.append(f"{nick} {title}")
        if forms and re.match(r"\d{4}-\d{2}-\d{2}", scene.get("date") or ""):
            y, m, d = scene["date"].split("-")
            out.append(f"{forms[0]} {y[2:]}.{m}.{d}")
        if not out and title:
            out.append(title)
    # the words as typed, with a studio name glued together the way uploaders write it
    typed = " ".join(q.split())
    if typed:
        named = named_studios(typed, known_studios)
        if named:
            studio, aliases, rest = named[0]
            glued = " ".join([studio_forms(studio)[0]] + rest)
            if norm(glued) != norm(typed):
                out.append(glued)
            for nick in [a for a in aliases if " " not in a and "." not in a and len(compact(a)) >= 4
                         and compact(a) != compact(studio)][:2]:
                out.append(" ".join([nick] + rest))      # FratX is also posted as FraternityX
            if len(rest) >= 2:
                out.append(" ".join(rest))
        out.append(typed)
    return list(dict.fromkeys(x.strip() for x in out if x.strip()))[:MAX_QUERIES]


# ── 3. search Prowlarr's adult indexers ──────────────────────────────────────
def _prowlarr(cfg, timeout=120):
    s = cfg.source("prowlarr")
    return httpx.Client(base_url=s["url"].rstrip("/") + "/api/v1", timeout=timeout, headers={"X-Api-Key": s["api_key"]})


GAY_CATEGORY = re.compile(r"\bgay\b|гей", re.I)


def _gay_categories(indexer):
    """The indexer's own gay sections (PornoLab has ten); empty when it has none."""
    found = set()
    def walk(cats):
        for c in cats or []:
            if GAY_CATEGORY.search(c.get("name") or ""):
                found.add(c["id"])
            walk(c.get("subCategories"))
    walk((indexer.get("capabilities") or {}).get("categories"))
    return found


def adult_indexers(cfg):
    """[(id, name, gay category ids)] of enabled indexers tagged xxx (or offering the XXX category when
    nothing is tagged)."""
    with _prowlarr(cfg, timeout=30) as c:
        tags = {t["id"]: t["label"].lower() for t in c.get("/tag").json()}
        idx = [i for i in c.get("/indexer").json() if i.get("enable")]
    tagged = [i for i in idx if any(tags.get(t) == "xxx" for t in i.get("tags") or [])]
    if not tagged:
        tagged = [i for i in idx if any(cat.get("id") == 6000 for cat in (i.get("capabilities") or {}).get("categories") or [])]
    return [(i["id"], i["name"], _gay_categories(i)) for i in tagged]


def _search(cfg, query, ids, categories=()):
    with _prowlarr(cfg) as c:
        params = [("query", query), ("type", "search"), ("limit", "100")] + [("indexerIds", str(i)) for i in ids] \
            + [("categories", str(x)) for x in sorted(categories)]
        r = c.get("/search?" + urllib.parse.urlencode(params))
        r.raise_for_status()
        return r.json()


# ── 4. rank by what you meant ────────────────────────────────────────────────
def _has_studio(t, tc, name, aliases=()):
    if not name:
        return False
    if any(compact(f) and compact(f) in tc for f in studio_forms(name)[:2]) or norm(name) in t:
        return True
    return any(len(compact(a)) >= 4 and compact(a) in tc for a in aliases)   # short nicknames match too much


def named_studios(q, studios):
    """[(name, aliases, other words)] for studios the words spell, by name or nickname, spaces or not.
    studios: {name: [aliases]} (a plain list/set of names works too)."""
    studios = studios if isinstance(studios, dict) else {s: [] for s in studios}
    out = []
    for name in sorted(studios, key=len, reverse=True):
        for spelling in [name, *studios[name]]:
            found, rest = split_studio(q, spelling)
            if found:
                out.append((name, studios[name], rest))
                break
    return out


def score(title, q, scene=None, studios=()):
    """(points, reasons, on_target) for one release title. on_target: it's from the studio you meant
    (or carries the scene's code); anything else can't be a match however many words it shares."""
    t, tc = norm(title), compact(title)
    pts, why, on_target = 0, [], False
    if scene:
        if _has_studio(t, tc, scene.get("studio"), scene.get("studio_aliases") or ()):
            pts += 4
            on_target = True
            why.append(scene["studio"])
        code = scene.get("code") or ""
        digits = re.sub(r"\D", "", code)
        if code and (compact(code) in tc or (len(digits) >= 3 and re.search(rf"(?<!\d){digits}(?!\d)", t))):
            pts += 5
            why.append(f"scene {scene['code']}")
            # a long or lettered code is distinctive on its own; "485" only counts with the studio
            on_target = on_target or len(digits) >= 5 or (re.search(r"[a-z]", compact(code)) and len(compact(code)) >= 4)
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
        named = named_studios(q, studios)
        rest = " ".join(named[0][2]) if named else norm(q)     # the studio's own words don't count again
        for name, aliases, _ in named:
            if _has_studio(t, tc, name, aliases):
                pts += 6
                on_target = True
                why.append(name)
        words = [w for w in _words(rest) if len(w) > 1]
        hits = [w for w in words if re.search(rf"\b{re.escape(w)}\b", t) or (len(w) > 3 and w in tc)]
        pts += 2 * len(hits)
        if hits:
            why.append(", ".join(hits[:4]))
        if named and not on_target:
            pts = min(pts, 1)                    # you named a studio; another studio's release isn't it
        on_target = on_target or not named
    return pts, why, on_target


def studio_hints(cfg, q):
    """{studio: [aliases]} for studios the words name, by name or nickname ("fratx" -> FraternityX)."""
    c = _stashdb(cfg)
    if not c:
        return {s["studio"]: [] for s in understand(cfg, q, limit=5) if s["studio"]}
    words = norm(q).split()
    pairs = [words[i:i + 2] for i in range(min(len(words) - 1, 3))]
    terms = list(dict.fromkeys([q.strip()] + [w for w in words if len(w) >= 3][:3]
                               + ["".join(p) for p in pairs]))            # "fraternity x" -> fraternityx
    found = {}
    with c:
        for term in terms:
            try:
                for s in _gql(c, "query($t:String!){ searchStudio(term:$t, limit:5){ name aliases } }", {"t": term}).get("searchStudio") or []:
                    found.setdefault(s["name"], s.get("aliases") or [])
            except Exception as e:
                log.info("stashdb studio search %r failed: %s", term, e)
    return {n: a for n, a in found.items() if named_studios(q, {n: a})}


def find(cfg, q, scene=None, gay_only=False):
    """{"queries": [...], "indexers": [...], "results": [...], "errors": [...], "hidden": n}."""
    ids = adult_indexers(cfg)
    if not ids:
        return {"queries": [], "indexers": [], "results": [], "errors": ["No adult indexers in Prowlarr (tag them xxx)"]}
    if scene and scene.get("studio") and "studio_aliases" not in scene:
        try:                                     # its nicknames too: FratX is mostly posted as FraternityX
            scene = {**scene, "studio_aliases": studio_hints(cfg, scene["studio"]).get(scene["studio"], [])}
        except Exception as e:
            log.info("stashdb studio lookup failed: %s", e)
    types, q = extract_types(q)                  # "fratx group dp" -> group + dp, and "fratx"
    studios = {scene["studio"]: scene.get("studio_aliases") or []} if scene and scene.get("studio") else {}
    if not scene and q:
        try:
            studios.update(studio_hints(cfg, q))
        except Exception as e:
            log.info("stashdb lookup failed: %s", e)
    queries = phrasings(q, scene, studios) if (q or scene) else []
    if types:                                    # also search with the type the way it's tagged
        tag = SCENE_TYPES[types[0]][1][0]
        with_type = [f"{b} {tag}" for b in queries[:2]] or [tag] + ([f"gay {tag}"] if gay_only else [])
        queries = list(dict.fromkeys(with_type + queries))[:MAX_QUERIES + 1]
    # Gay only: sites with gay sections are searched in just those sections; the rest are kept
    # only for releases that say "gay" or come from the studio you named / the scene you picked.
    sectioned = {i: cats for i, _, cats in ids if cats} if gay_only else {}
    plain = [i for i, _, _ in ids if i not in sectioned]
    gay_cats = set().union(*sectioned.values()) if sectioned else set()
    jobs = [(query, list(sectioned), gay_cats) for query in queries if sectioned] + \
           [(query, plain, set()) for query in queries if plain]
    seen, results, errors, dropped = {}, [], [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), 8) or 1) as pool:
        futures = {pool.submit(_search, cfg, query, idx, cats): (query, bool(cats)) for query, idx, cats in jobs}
        for f in concurrent.futures.as_completed(futures):
            query, in_sections = futures[f]
            try:
                found = f.result()
            except Exception as e:
                errors.append(f"{query}: {type(e).__name__}")
                continue
            for rel in found:
                key = rel.get("infoHash") or rel.get("guid")
                if key in seen:
                    seen[key]["found_by"].add(query)
                    continue
                pts, why, on_target = score(rel.get("title", ""), q, scene, studios)
                if types:                        # every type you asked for has to be in its tags
                    hits = type_hits(rel.get("title", ""), types)
                    pts += 3 * len(hits)
                    why += hits
                    on_target = on_target and len(hits) == len(types)
                if gay_only:
                    cats = {c.get("id") for c in rel.get("categories") or []}
                    if in_sections:
                        ok = bool(cats & gay_cats)
                    else:
                        adult = not cats or any(6000 <= (c or 0) < 7000 for c in cats)   # not a kids' film called "Gay Purr-ee"
                        ok = adult and (bool(GAY_CATEGORY.search(rel.get("title", ""))) or (on_target and bool(studios)))
                    if not ok:
                        dropped += 1
                        continue
                item = {"guid": rel.get("guid"), "indexer_id": rel.get("indexerId"), "indexer": rel.get("indexer"),
                        "title": rel.get("title", ""), "size": rel.get("size") or 0, "seeders": rel.get("seeders"),
                        "published": (rel.get("publishDate") or "")[:10], "info_url": rel.get("infoUrl") or "",
                        "protocol": rel.get("protocol"), "score": pts, "why": why, "on_target": on_target,
                        "found_by": {query}}
                seen[key] = item
                results.append(item)
    best = max((r["score"] for r in results if r["on_target"]), default=0)
    floor = max(4, best * 0.5) if scene else max(2, best * 0.5)
    for r in results:
        r["match"] = bool(r.pop("on_target")) and r["score"] >= floor and r["score"] > 0
        r["found_by"] = sorted(r["found_by"])
    results.sort(key=lambda r: (-r["match"], -r["score"], -(r["seeders"] or 0)))
    return {"queries": queries, "indexers": [n for _, n, _ in ids], "results": results[:80], "errors": errors,
            "hidden": dropped, "gay_only": gay_only, "types": types}


# ── your choices (Gay only), kept for the searches Follow runs on its own too ──
def prefs(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS adult_prefs (k TEXT PRIMARY KEY, v TEXT)")
        got = dict(con.execute("SELECT k, v FROM adult_prefs").fetchall())
    finally:
        con.close()
    return {"gay_only": got.get("gay_only") == "1"}


def set_prefs(state_path, gay_only):
    con = sqlite3.connect(state_path, timeout=15)
    with con:
        con.execute("CREATE TABLE IF NOT EXISTS adult_prefs (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("INSERT OR REPLACE INTO adult_prefs VALUES ('gay_only', ?)", ("1" if gay_only else "0",))
    con.close()
    return prefs(state_path)


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
  scenes { id title code date studio { name aliases } performers { performer { name } } } } }"""


def _new_scenes(c, kind, fid, since):
    d = _gql(c, SCENES_BY % ("studios" if kind == "studio" else "performers"), {"id": fid})
    out = []
    for s in (d.get("queryScenes") or {}).get("scenes") or []:
        if (s.get("date") or "") < since:
            continue
        out.append({"id": s["id"], "title": s.get("title") or "", "code": s.get("code") or "", "date": s.get("date") or "",
                    "studio": (s.get("studio") or {}).get("name") or "", "studio_aliases": (s.get("studio") or {}).get("aliases") or [],
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
            pick = best_release(find(cfg, "", scene, gay_only=prefs(state_path)["gay_only"]))
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