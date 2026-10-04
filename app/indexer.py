"""Build the search index: read every connector, group Units into Works, write SQLite + FTS5.

Grouping (books only; other kinds never merge with each other):
  1. manual merges from the state DB (the user said so)
  2. BookBridge links (ABS <-> Calibre <-> Storyteller it already syncs)
  3. Storyteller's staged folder: hardlinked audio (inode = ABS file) and the
     epub's Calibre filename. Exact.
  4. manual-pairs.json from the book-pipeline (Audible ASIN <-> Calibre id)
  5. ISBN equality
  6. fuzzy: stage-pairs rule (author surname agrees AND title >= 0.90)
A Work never takes two Units from the same app (two Calibre books are two books),
and a unit the user split off is never auto-merged again.

Movies/TV link to books as *adaptations* (separate entries, cross-linked), from
the `adaptations:` list in config.
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import time

from . import normalize
from .connectors import abs as abs_c, arr, bookbridge, calibre, jellyfin, komga, plex, romm, stash, storyteller
from .connectors.base import iso_time

log = logging.getLogger("omnarr.indexer")

CONNECTORS = [("calibre", calibre.read), ("abs", abs_c.read), ("storyteller", storyteller.read),
              ("jellyfin", jellyfin.read), ("plex", plex.read), ("sonarr", arr.read_sonarr), ("radarr", arr.read_radarr),
              ("komga", komga.read), ("romm", romm.read), ("stash", stash.read)]
SOURCE_RANK = {"calibre": 0, "abs": 1, "storyteller": 2, "komga": 3, "jellyfin": 4, "plex": 4, "sonarr": 5, "radarr": 5,
               "romm": 6, "stash": 7}
# Formats that describe tracking, not something you can open: never shown as a format badge.
TRACKING_FORMATS = {"tracked"}

SCHEMA = """
CREATE TABLE works (
  id TEXT PRIMARY KEY, kind TEXT, title TEXT, sort_title TEXT, authors TEXT, narrators TEXT,
  series TEXT, series_index REAL, year INTEGER, description TEXT, genres TEXT, tags TEXT,
  cover TEXT, duration REAL, added TEXT, progress REAL, status TEXT, last_activity TEXT,
  hidden INTEGER, adult INTEGER, rating REAL, formats TEXT, sources TEXT, libraries TEXT,
  universe TEXT, universe_index REAL, series_key TEXT, info TEXT);
CREATE TABLE editions (
  work_id TEXT, unit_key TEXT, source TEXT, source_id TEXT, format TEXT, title TEXT, url TEXT,
  cover TEXT, duration REAL, progress REAL, finished INTEGER, hidden INTEGER, library TEXT,
  narrators TEXT, matched_by TEXT, extra TEXT);
CREATE TABLE work_links (a TEXT, b TEXT, rel TEXT);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE VIRTUAL TABLE works_fts USING fts5(
  id UNINDEXED, title, authors, narrators, series, description, genres,
  tokenize = 'unicode61 remove_diacritics 2');
CREATE INDEX ix_ed_work ON editions(work_id);
CREATE TABLE user_progress (account_id INTEGER, work_id TEXT, progress REAL, status TEXT, last_activity TEXT,
  PRIMARY KEY (account_id, work_id));
CREATE INDEX ix_works_kind ON works(kind);
CREATE INDEX ix_links_a ON work_links(a);
"""


# ── union-find with a "one unit per app" constraint ──────────────────────────
class Groups:
    def __init__(self, units):
        self.parent = {u.key: u.key for u in units}
        self.members = {u.key: {u.key} for u in units}
        self.sources = {u.key: {u.source} for u in units}
        self.how = {}                       # unit key -> how it was matched
        self.split = set()

    def find(self, k):
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a, b, how, force=False):
        if a not in self.parent or b not in self.parent:
            return False
        if not force and (a in self.split or b in self.split):
            return False
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if not force and (self.sources[ra] & self.sources[rb]):
            return False                    # would put two books from one app into one Work
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] |= self.members.pop(rb)
        self.sources[ra] |= self.sources.pop(rb)
        for k in (a, b):
            self.how.setdefault(k, how)
        return True

    def groups(self):
        return [sorted(m) for m in self.members.values()]


def _isbn(v):
    d = re.sub(r"[^0-9Xx]", "", v or "")
    return d.upper() if len(d) in (10, 13) else ""


def _state(path):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS overrides (a TEXT, b TEXT, action TEXT, created REAL,
                                            PRIMARY KEY (a, b, action));
      CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created REAL, expires REAL, adult_until REAL);
    """)
    return con


def group_books(units, links, manual_pairs, overrides):
    books = [u for u in units if u.kind == "book"]
    by_key = {u.key: u for u in books}
    g = Groups(books)
    for a, b, action in overrides:
        if action == "split":
            g.split.add(a)
    for a, b, action in overrides:
        if action == "merge":
            g.union(a, b, "manual", force=True)
    for a, b in links:
        g.union(a, b, "bookbridge")

    # Storyteller staged folder -> exact keys
    abs_by_inode, cal_by_epub = {}, {}
    for u in books:
        if u.source == "abs":
            for i in u.match.get("inodes", []):
                abs_by_inode[i] = u.key
        elif u.source == "calibre":
            for n in u.match.get("epub_names", []):
                cal_by_epub.setdefault(n, []).append(u.key)
    for u in books:
        if u.source != "storyteller":
            continue
        for i in u.match.get("inodes", []):
            if i in abs_by_inode:
                g.union(u.key, abs_by_inode[i], "same audio file")
                break
        for n in u.match.get("epub_names", []):
            hits = cal_by_epub.get(n, [])
            if len(hits) == 1:
                g.union(u.key, hits[0], "same epub file")

    # book-pipeline manual pairs (Audible ASIN <-> Calibre id)
    abs_by_asin = {u.ids.get("audible"): u.key for u in books if u.source == "abs" and u.ids.get("audible")}
    for p in manual_pairs:
        a = abs_by_asin.get(p.get("asin"))
        c = f"calibre:{p.get('calibre_id')}"
        if a and c in by_key:
            g.union(a, c, "manual pair")

    # ISBN
    by_isbn = {}
    for u in books:
        i = _isbn(u.ids.get("isbn"))
        if i:
            by_isbn.setdefault(i, []).append(u.key)
    for keys in by_isbn.values():
        for k in keys[1:]:
            g.union(keys[0], k, "isbn")

    # fuzzy: each ebook's best audiobook (stage-pairs rule), only between unmerged pairs
    ebooks = [u for u in books if u.source == "calibre" and not u.hidden]
    audios = [u for u in books if u.source == "abs"]
    av = {a.key: normalize.variants(a.title) for a in audios}
    asn = {a.key: normalize.surname(a.first_author) for a in audios}
    for e in ebooks:
        if "abs" in g.sources[g.find(e.key)]:
            continue
        ev, esn = normalize.variants(e.title), normalize.surname(e.first_author)
        best, score = None, 0.0
        for a in audios:
            if "calibre" in g.sources[g.find(a.key)]:
                continue
            if esn and asn[a.key] and esn != asn[a.key]:
                continue
            r = normalize.similarity(ev, av[a.key])
            if r > score:
                best, score = a, r
        if best and score >= normalize.THRESHOLD:
            g.union(e.key, best.key, f"title match {score:.2f}")
    return g, by_key


def _work_id(keys):
    anchor = sorted(keys, key=lambda k: (SOURCE_RANK.get(k.split(":")[0], 9), k))[0]
    return hashlib.sha1(anchor.encode()).hexdigest()[:12]


def _pick(units, attr, prefer=None):
    order = sorted(units, key=lambda u: (prefer or SOURCE_RANK).get(u.source, 9))
    for u in order:
        v = getattr(u, attr)
        if v not in (None, "", [], {}):
            return v
    return None


def build_work(members):
    series = _pick(members, "series") or ""
    raw = _pick(members, "title") or ""
    title = normalize.display_title(raw, series) if members[0].kind in ("book", "comic") else raw
    abs_first = {"abs": 0, "storyteller": 1, "calibre": 2}
    progress = max((u.progress for u in members if u.progress is not None), default=None)
    finished = any(u.finished for u in members)
    status = "finished" if finished else ("in_progress" if (progress or 0) > 0.005 else "unread")
    desc = max((u.description for u in members), key=len, default="")
    genres = _pick(members, "genres", abs_first) or []
    tags = sorted({t for u in members for t in u.tags})
    added = min((u.added for u in members if u.added), default="")
    last = max((iso_time(u.extra.get("last_listened")) for u in members), default="")
    return {
        "kind": members[0].kind, "title": title, "sort_title": normalize.sort_title(title),
        "authors": _pick(members, "authors") or [], "narrators": _pick(members, "narrators", abs_first) or [],
        "series": series, "series_index": _pick(members, "series_index"), "series_key": normalize.series_key(series),
        "year": _pick(members, "year", abs_first), "description": desc, "genres": genres, "tags": tags,
        "cover": _pick(members, "cover") or "", "duration": _pick(members, "duration", abs_first),
        "added": added, "progress": progress, "status": status, "last_activity": last,
        "hidden": all(u.hidden for u in members), "adult": any(u.adult for u in members),
        "rating": _pick(members, "rating"),
        "formats": sorted({u.format for u in members if not u.hidden and u.format not in TRACKING_FORMATS}),
        "sources": sorted({u.source for u in members}),
        "libraries": sorted({u.library for u in members if u.library}),
        "info": _screen_info(members),
    }


def _screen_info(members):
    """Shows/movies: what you have vs what Sonarr/Radarr track, for cards and filters.
    availability: in_library | partial | coming (tracked, nothing on disk yet) | ''."""
    if members[0].kind not in ("show", "movie"):
        return {}
    arr_u = next((u for u in members if u.source in ("sonarr", "radarr")), None)
    in_jf = any(u.source == "jellyfin" for u in members)
    info = dict(arr_u.extra.get("arr") or {}) if arr_u else {}
    if members[0].kind == "show":
        have, aired = info.get("have"), info.get("aired")
        if have is None:
            avail = "in_library" if in_jf else ""
        elif have == 0:
            avail = "coming"
        else:
            avail = "in_library" if have >= (aired or 0) else "partial"
    else:
        avail = "in_library" if (in_jf or info.get("has_file")) else ("coming" if arr_u else "")
    info.update(availability=avail, in_jellyfin=in_jf, tracked=bool(arr_u))
    return info


def group_screens(units):
    """Same show/movie in Jellyfin and Sonarr/Radarr -> one Work, matched on TVDB/TMDB/IMDb ids."""
    screens = [u for u in units if u.kind in ("show", "movie")]
    g = Groups(screens)
    for kind, idkeys in (("show", ("tvdb", "tmdb", "imdb")), ("movie", ("tmdb", "imdb"))):
        for k in idkeys:
            seen = {}
            for u in screens:
                v = u.ids.get(k)
                if u.kind != kind or not v:
                    continue
                if (kind, v) in seen:
                    g.union(seen[(kind, v)], u.key, f"same {k} id")
                else:
                    seen[(kind, v)] = u.key
    by_key = {u.key: u for u in screens}
    return [[by_key[k] for k in grp] for grp in g.groups()]


_STARTER = None


def _starter():
    """Built-in adaptations/universes (app/starter.yml), loaded once."""
    global _STARTER
    if _STARTER is None:
        import yaml
        try:
            with open(os.path.join(os.path.dirname(__file__), "starter.yml"), encoding="utf-8") as f:
                _STARTER = yaml.safe_load(f) or {}
        except OSError:
            _STARTER = {}
    return _STARTER


def _list(cfg, key):
    """config.yml entries first, then the built-in starter entries (unless `starter_lists: false`).
    A user universe with the same name as a starter one replaces it."""
    user = list(cfg.get(key) or [])
    if cfg.get("starter_lists", True) is False:
        return user
    starter = _starter().get(key) or []
    if key == "universes":
        names = {normalize.normalize_word(u.get("name") or "") for u in user}
        starter = [u for u in starter if normalize.normalize_word(u.get("name") or "") not in names]
    return user + starter


def _adaptation_links(cfg, works):
    """config `adaptations:` -> [(book_work_id, screen_work_id)].
    Entries: {book: "Wool", screen: "Silo"} or {series: "Foundation", screen: "Foundation"}."""
    out = []
    books = [(wid, w) for wid, w in works.items() if w["kind"] == "book"]
    screens = [(wid, w) for wid, w in works.items() if w["kind"] in ("movie", "show")]
    for entry in _list(cfg, "adaptations"):
        skey = normalize.key(entry.get("screen", ""))
        targets = [wid for wid, w in screens if normalize.key(w["title"]) == skey]
        if entry.get("year"):
            targets = [wid for wid in targets if works[wid]["year"] == entry["year"]] or targets
        if entry.get("series"):
            sk = normalize.key(entry["series"])
            sources = [wid for wid, w in books if w["series"] and normalize.key(w["series"]) == sk]
        else:
            bk = normalize.key(entry.get("book", ""))
            sources = [wid for wid, w in books if normalize.key(w["title"]) == bk]
        for b in sources:
            for t in targets:
                if (b, t) not in out:
                    out.append((b, t))
    return out


def _unify_series_names(works):
    """Apps spell one series differently ("The Silo Saga" / "Silo"); group by series_key and
    show every book in it under the most common spelling."""
    from collections import Counter
    names = {}
    for w in works.values():
        if w["series"]:
            names.setdefault((w["kind"], w["series_key"]), Counter())[w["series"]] += 1
    for w in works.values():
        if w["series"]:
            w["series"] = names[(w["kind"], w["series_key"])].most_common(1)[0][0]


def _apply_series_aliases(cfg, units):
    """config `series_aliases: {"Robots": "Robot", ...}` -- one name per series across apps."""
    aliases = {normalize.normalize_word(k): v for k, v in (cfg.get("series_aliases") or {}).items()}
    for u in units:
        if u.series and normalize.normalize_word(u.series) in aliases:
            u.series = aliases[normalize.normalize_word(u.series)]


def _assign_universes(cfg, works):
    """config `universes:` -> works[wid]["universe"/"universe_index"].

    A universe groups several series (and standalones) set in one world, in a reading
    order: {name, author?, order: [titles...], series: [names...]}. Titles in `order`
    take that position; other books from the listed series follow, by series order.
    """
    for wid, w in works.items():
        w["universe"], w["universe_index"] = "", None
    for uni in _list(cfg, "universes"):
        name = uni.get("name") or ""
        sn = normalize.surname(uni.get("author") or "")
        order = {normalize.key(t): i for i, t in enumerate(uni.get("order") or [])}
        series = {}                                  # series name -> position in the list
        for i, s in enumerate(uni.get("series") or []):
            series.setdefault(normalize.normalize_word(s), i)
        for wid, w in works.items():
            if w["kind"] != "book" or w["universe"]:
                continue
            if sn and not any(normalize.surname(a) == sn for a in w["authors"]):
                continue
            k = normalize.key(w["title"])
            if k in order:
                w["universe"], w["universe_index"] = name, float(order[k])
            elif w["series"] and normalize.normalize_word(w["series"]) in series:
                w["universe"] = name                 # series in listed order, then by number
                w["universe_index"] = 1000 + 1000 * series[normalize.normalize_word(w["series"])] + (w["series_index"] or 0)


def run(cfg):
    t0 = time.time()
    units, errors, counts = [], {}, {}
    from . import extend
    for name, fn in CONNECTORS + [("custom_library", extend.read_custom)] + extend.plugin_readers():
        if not cfg.source(name):
            continue
        try:
            got = fn(cfg)
            units.extend(got)
            counts[name] = len(got)
        except Exception as e:                   # one broken app must not blank the whole hub
            log.warning("connector %s failed: %s", name, e, exc_info=True)
            errors[name] = f"{type(e).__name__}: {e}"
    links = []
    if cfg.source("bookbridge"):
        try:
            links = bookbridge.read_links(cfg)
            counts["bookbridge_links"] = len(links)
        except Exception as e:
            errors["bookbridge"] = f"{type(e).__name__}: {e}"
    manual_pairs = []
    mp = cfg.get("sources.manual_pairs")
    if mp and os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                manual_pairs = json.load(f)
        except Exception as e:
            errors["manual_pairs"] = str(e)

    _apply_series_aliases(cfg, units)
    state = _state(cfg.get("index.state", "/data/state.db"))
    overrides = state.execute("SELECT a, b, action FROM overrides").fetchall()
    state.close()

    g, by_key = group_books(units, links, manual_pairs, overrides)
    groups = [[by_key[k] for k in grp] for grp in g.groups()]
    groups += group_screens(units)
    groups += [[u] for u in units if u.kind not in ("book", "show", "movie")]

    works, editions = {}, []
    for members in groups:
        wid = _work_id([u.key for u in members])
        works[wid] = build_work(members)
        for u in members:
            editions.append((wid, u, g.how.get(u.key, "") if u.kind == "book" else
                             ("same id" if len(members) > 1 and u.kind in ("show", "movie") else "")))
    _unify_series_names(works)
    wlinks = _adaptation_links(cfg, works)
    _assign_universes(cfg, works)

    path = cfg.get("index.path", "/data/index.db")
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    J = json.dumps
    for wid, w in works.items():
        con.execute("INSERT INTO works VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            wid, w["kind"], w["title"], w["sort_title"], J(w["authors"]), J(w["narrators"]),
            w["series"], w["series_index"], w["year"], w["description"], J(w["genres"]), J(w["tags"]),
            w["cover"], w["duration"], w["added"], w["progress"], w["status"], w["last_activity"],
            int(w["hidden"]), int(w["adult"]), w["rating"], J(w["formats"]), J(w["sources"]), J(w["libraries"]),
            w["universe"], w["universe_index"], w["series_key"], J(w["info"])))
        con.execute("INSERT INTO works_fts VALUES (?,?,?,?,?,?,?)", (
            wid, w["title"], " ".join(w["authors"]), " ".join(w["narrators"]),
            " ".join(x for x in (w["series"], w["universe"]) if x),
            w["description"][:4000], " ".join(w["genres"] + w["tags"])))
    for wid, u, how in editions:
        con.execute("INSERT INTO editions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            wid, u.key, u.source, u.source_id, u.format, u.title, u.url, u.cover, u.duration,
            u.progress, int(u.finished), int(u.hidden), u.library, J(u.narrators), how,
            J({**u.extra, "ids": u.ids, "authors": u.authors, "series": u.series,
               "series_index": u.series_index, "year": u.year})))
    try:                                         # each person's own progress (progress.py)
        from . import progress as progress_mod
        prows = progress_mod.build(cfg, cfg.get("index.state", "/data/state.db"), editions)
        con.executemany("INSERT OR REPLACE INTO user_progress VALUES (?,?,?,?,?)", prows)
        counts["progress_rows"] = len(prows)
    except Exception as e:
        log.warning("per-person progress failed: %s", e, exc_info=True)
        errors["progress"] = f"{type(e).__name__}: {e}"
    for a, b in wlinks:
        con.execute("INSERT INTO work_links VALUES (?,?,?)", (a, b, "adaptation"))
        con.execute("INSERT INTO work_links VALUES (?,?,?)", (b, a, "adaptation"))
    stats = {"counts": counts, "errors": errors, "works": len(works), "units": len(units),
             "links": len(wlinks), "seconds": round(time.time() - t0, 2),
             "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    con.execute("INSERT INTO meta VALUES ('stats', ?)", (J(stats),))
    con.commit()
    con.close()
    os.replace(tmp, path)                        # atomic swap: readers never see a half-built index
    log.info("index built: %s", stats)
    return stats
