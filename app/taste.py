"""Taste: what a person's library is *about*, beyond genre, for suggestions.

Books get their subjects from Open Library ("Dystopias", "Plague", "Political fiction"); movies
and shows get their TMDB keywords through Seerr ("totalitarianism", "chosen one"). Both are cached
in the state DB, so each title is looked up once. A profile weights the terms that are unusually
common in what someone has read and watched: a term every book carries ("Fiction") counts for
nothing, a term a third of their shelf shares counts for a lot.

Taste notes are a person's own words, "more of" and "less of", one idea per line. A suggestion
whose subjects/keywords mention every main word of a note is moved up (more of) or dropped
(less of). Nothing here leaves the server except the title/author/TMDB lookups themselves.
"""
import json
import logging
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict

import httpx

from . import normalize

log = logging.getLogger("omnarr.taste")
from .requests_ import UA as _UA

OL = "https://openlibrary.org"
UA = {"User-Agent": _UA}
OUTAGE_MINUTES = 15         # after 3 failures in a row, stop asking Open Library for a while
CACHE_DAYS = 120
LIST_DAYS = 14                      # author / subject lists change slowly
MAX_TERMS = 80
OL_FIELDS = "key,title,author_name,first_publish_year,edition_count,subject,cover_i"
PAUSE = 1.0                 # seconds between Open Library requests (it asks for gentle use)

SCHEMA = """
CREATE TABLE IF NOT EXISTS taste_terms (k TEXT PRIMARY KEY, terms TEXT, at REAL);
CREATE TABLE IF NOT EXISTS taste_lists (k TEXT PRIMARY KEY, items TEXT, at REAL);
CREATE TABLE IF NOT EXISTS taste_prefs (account_id INTEGER PRIMARY KEY, library_is_read INTEGER NOT NULL DEFAULT 0,
  more TEXT NOT NULL DEFAULT '', less TEXT NOT NULL DEFAULT '');
"""

# Library/bookseller bookkeeping that Open Library mixes in with real subjects
NOISE = re.compile("^(" + "|".join((
    r"fiction", r"general", r"english", r"american", r"novels?", r"literature", r"large type books?", r"accessible book",
    r"protected daisy", r"in library", r"lending library", r"open[_ ]?syllabus.*", r"nyt.*", r"new york times.*",
    r"bestseller.*", r"award.*", r"juvenile.*", r"children'?s.*", r"young adult.*", r"adult", r"readers?",
    r"reading level.*", r"textual", r"translations?.*", r"english language.*", r"fiction in english",
    r"(american|english) fiction", r"popular work", r"history and criticism", r"criticism.*", r"long now manual.*",
    r"e-?books?", r"audiobooks?", r"audio.*", r"internet archive.*", r"overdrive.*", r"literary", r"imaginary.*",
    r"(\w+ )?authors?", r"\d.*", r"nouvelles?", r"romans?", r"roman fran.*", r"novela.*", r"ficci[oó]n.*", r"literatura.*",
    r"(\w+ )?literature", r"action (&|and) adventure", r"interpersonal relations", r"historical", r"history", r"biography",
    r"(\w+ )?language materials?", r"spanish language.*", r"comics.*graphic novels", r"thrillers? \(fiction\)", r"suspense",
    r"man-woman relationships", r"good and evil", r"fiction.*general", r"general.*fiction", r"united states",
    r"children", r"magie", r"etc\..*", r"collections?", r"anthologies", r"short stories")) + ")$", re.I)
# Subjects in other languages ("voyages dans le temps", "pour la jeunesse")
FOREIGN_WORDS = re.compile(r"\b(pour|dans|les|des|la|le|du|et|el|los|las|del|und|der|die|das|jeunesse|romans?)\b|[éèêàçñüöäß]", re.I)
# One idea, one name: "fantasy fiction" = "fantasy", "dystopias" = "dystopian" = "dystopia"
CANON = {"dystopias": "dystopia", "dystopian": "dystopia", "dystopian fiction": "dystopia", "utopias": "utopia",
         "fantasy fiction": "fantasy", "fantasy & magic": "fantasy", "magic": "magic", "science-fiction": "science fiction",
         "sci-fi": "science fiction", "adventure and adventurers": "adventure", "thrillers": "thriller",
         "political fiction": "political", "historical fiction": "historical", "horror fiction": "horror",
         "space opera fiction": "space opera", "life on other planets": "alien worlds", "human-alien encounters": "first contact",
         "imaginary wars and battles": "war", "artificial intelligence fiction": "artificial intelligence"}
# Searching Open Library needs its own wording back ("first contact" there means Columbus)
QUERY = {}
for _raw, _canon in CANON.items():
    QUERY.setdefault(_canon, _raw)
# Shelves, not themes: still count when scoring, but "Stories about…" never leads with them
GENERIC = {"science fiction", "fantasy", "adventure", "romance", "thriller", "mystery", "horror", "humor", "drama",
           "action", "comedy", "war", "politics and government", "social conditions"}


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ── taste notes and "my library is what I've read" ───────────────────────────
def prefs(state_path, account_id):
    con = _con(state_path)
    try:
        r = con.execute("SELECT * FROM taste_prefs WHERE account_id=?", (account_id,)).fetchone()
    finally:
        con.close()
    return {"library_is_read": bool(r and r["library_is_read"]), "more": r["more"] if r else "", "less": r["less"] if r else ""}


def set_prefs(state_path, account_id, library_is_read=None, more=None, less=None):
    cur = prefs(state_path, account_id)
    new = {"library_is_read": cur["library_is_read"] if library_is_read is None else bool(library_is_read),
           "more": cur["more"] if more is None else str(more)[:2000], "less": cur["less"] if less is None else str(less)[:2000]}
    con = _con(state_path)
    with con:
        con.execute("INSERT OR REPLACE INTO taste_prefs VALUES (?,?,?,?)",
                    (account_id, int(new["library_is_read"]), new["more"], new["less"]))
    con.close()
    return new


STOP = {"about", "with", "stories", "story", "books", "more", "less", "that", "where", "when", "from", "like", "very"}


def notes(text):
    """'dystopia, political corruption\\nchosen one' -> [['dystopia'], ['political', 'corruption'], ['chosen']]."""
    out = []
    for phrase in re.split(r"[\n,;]+", text or ""):
        words = [w for w in re.findall(r"[a-z]+", phrase.lower()) if len(w) >= 4 and w not in STOP]
        if words:
            out.append(words)
    return out


def _word_hit(w, words):
    """Whole words (and plurals); a long word also matches its longer forms ('totalitarian' -> 'totalitarianism'),
    but a short one never does, so 'romance' can't catch 'roman empire'."""
    if w in words or w + "s" in words or w.rstrip("s") in words:
        return True
    return len(w) >= 6 and any(t.startswith(w) for t in words)


def _hits(phrases, terms):
    words = {w for t in terms for w in re.findall(r"[a-z]+", t.lower())}
    return [p for p in phrases if all(_word_hit(w, words) for w in p)]


# ── cleaning ─────────────────────────────────────────────────────────────────
def clean(raw):
    """Open Library subjects -> short, lower-case themes ('Fiction, dystopian' -> 'dystopian')."""
    out = []
    for s in raw or []:
        for part in re.split(r"\s*(?:--|,|/)\s*", str(s)):
            t = re.sub(r"\s+", " ", part.strip().strip(".")).lower()
            t = re.sub(r"^fiction ", "", t)
            t = re.sub(r" (stories|tales)$", "", t)
            t = CANON.get(t, t)
            if 3 <= len(t) <= 40 and not NOISE.match(t) and not FOREIGN_WORDS.search(t) \
                    and not re.search(r"[=:()\[\]]", t) and t not in out:
                out.append(t)
    return out[:40]


# ── lookups (cached) ─────────────────────────────────────────────────────────
def _cached(con, table, key, days):
    col = "terms" if table == "taste_terms" else "items"
    r = con.execute(f"SELECT {col}, at FROM {table} WHERE k=?", (key,)).fetchone()
    if r and r["at"] > time.time() - days * 86400:
        return json.loads(r[col])
    return None


def _store(state_path, table, key, value):
    con = _con(state_path)
    with con:
        con.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)", (key, json.dumps(value), time.time()))
    con.close()


_ol_state = {"fails": 0, "down_until": 0.0}


def _ol(path, params):
    """One Open Library request. When it's down, fail fast instead of waiting out every timeout."""
    if time.time() < _ol_state["down_until"]:
        raise RuntimeError("Open Library isn't answering; trying again later")
    try:
        with httpx.Client(timeout=15, headers=UA) as c:
            r = c.get(OL + path, params=params)
            r.raise_for_status()
            out = r.json()
    except Exception:
        _ol_state["fails"] += 1
        if _ol_state["fails"] >= 3:
            _ol_state["down_until"] = time.time() + OUTAGE_MINUTES * 60
            _ol_state["fails"] = 0
        raise
    _ol_state["fails"] = 0
    return out


def book_key(title, authors):
    return "book:" + normalize.key(title) + "|" + normalize.surname((authors or [""])[0])


def screen_key(kind, tmdb):
    return f"tmdb:{'tv' if kind in ('tv', 'show') else 'movie'}:{tmdb}"


def short_title(title):
    """'Designated Targets: A Novel of the Axis of Time' -> 'Designated Targets'; 'Lightning Thief, The' -> 'The Lightning Thief'."""
    t = re.split(r"\s*(?::|\s-\s|\(|\[)", title or "")[0].strip()
    t = re.sub(r",?\s*(a novel|the novel)$", "", t, flags=re.I)
    m = re.match(r"^(.*), (the|a|an)$", t, re.I)
    return f"{m.group(2)} {m.group(1)}" if m else t


def fetch_book(title, authors):
    """Subjects for one book from Open Library (merged over the editions that are this book).
    Tries the short title with the author, then the title alone if that finds nothing."""
    short = short_title(title)
    want = normalize.key(short)
    sur = normalize.surname((authors or [""])[0])
    for params in ({"title": short, "author": (authors or [""])[0]}, {"title": short}):
        if not params.get("author", True):
            continue
        docs = _ol("/search.json", {**params, "fields": OL_FIELDS, "limit": 8}).get("docs") or []
        if sur:                                    # never take a same-titled book by someone else
            docs = [d for d in docs if sur in {normalize.surname(a) for a in d.get("author_name") or []}]
        same = [d for d in docs if normalize.key(short_title(d.get("title") or "")) == want] or docs[:1]
        got = clean([s for d in same[:2] for s in d.get("subject") or []])
        if got:
            return got
    return []


def fetch_screen(cfg, kind, tmdb):
    """TMDB keywords (plus genres) for a movie/show, through Seerr."""
    from . import requests_
    c = requests_._seerr(cfg)
    if not c:
        return []
    with c:
        r = c.get(f"/{'tv' if kind in ('tv', 'show') else 'movie'}/{int(tmdb)}")
    if r.status_code != 200:
        return []
    d = r.json()
    out = []
    for name in [k.get("name") for k in d.get("keywords") or []] + [g.get("name") for g in d.get("genres") or []]:
        t = re.sub(r"\s*\(.*?\)", "", (name or "").lower()).strip()
        if t and not SCREEN_NOISE.match(t) and t not in out:
            out.append(t)
    return out


# TMDB bookkeeping keywords
SCREEN_NOISE = re.compile(r"^(based on .*|duringcreditsstinger|aftercreditsstinger|post-credits scene|woman director|"
                          r"sequel|prequel|remake|reboot|live action( remake)?|anime|cartoon|3d|imax|tv movie|"
                          r"\d+s|\d{4}|miniseries|short film|anthology)$")


def terms(state_path, key):
    con = _con(state_path)
    try:
        return _cached(con, "taste_terms", key, CACHE_DAYS)
    finally:
        con.close()


def fill(cfg, state_path, wanted, budget=120):
    """Look up terms for [(key, kind, title, authors, tmdb)] not cached yet, most important first.
    Open Library asks for gentle use: one request a second, at most `budget` per run."""
    con = _con(state_path)
    try:
        todo = [w for w in wanted if _cached(con, "taste_terms", w[0], CACHE_DAYS) is None]
    finally:
        con.close()
    done = 0
    for key, kind, title, authors, tmdb in todo[:budget]:
        try:
            if kind in ("movie", "show", "tv"):
                got = fetch_screen(cfg, kind, tmdb) if tmdb else []
            else:
                got = fetch_book(title, authors)
                time.sleep(PAUSE)
        except Exception as e:
            log.info("taste lookup for %s failed: %s", title, e)
            if time.time() < _ol_state["down_until"]:
                break                                  # Open Library is down: the rest wait for the next refresh
            continue
        _store(state_path, "taste_terms", key, got)
        done += 1
    return done


def author_books(state_path, author):
    """Open Library's best-known works by an author, cached for LIST_DAYS."""
    return _list(state_path, "author:" + author.lower(), {"author": author, "sort": "editions"})


def subject_books(state_path, subject):
    q = QUERY.get(subject, subject)
    return _list(state_path, "subject:" + q.lower(), {"subject": q, "sort": "editions"})


def _list(state_path, key, params):
    con = _con(state_path)
    try:
        hit = _cached(con, "taste_lists", key, LIST_DAYS)
    finally:
        con.close()
    if hit is not None:
        return hit
    try:
        docs = _ol("/search.json", {**params, "fields": OL_FIELDS, "limit": 40, "language": "eng"}).get("docs") or []
        time.sleep(PAUSE)
    except Exception as e:
        log.info("open library list %s failed: %s", key, e)
        return []
    items = [{"title": d.get("title") or "", "authors": d.get("author_name") or [], "year": d.get("first_publish_year"),
              "editions": d.get("edition_count") or 0, "terms": clean(d.get("subject")),
              "url": OL + d["key"] if d.get("key") else "",
              "poster": f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else ""} for d in docs]
    _store(state_path, "taste_lists", key, items)
    return items


# ── the profile ──────────────────────────────────────────────────────────────
def profile(seed_terms, library_terms):
    """seed_terms: {work id: (weight, title, [terms])}; library_terms: [[terms]] for every work with
    terms. -> {"weights": {term: w}, "examples": {term: [titles]}} for the most telling terms."""
    n = max(1, len(library_terms))
    df = Counter(t for ts in library_terms for t in set(ts))
    score, examples, count = Counter(), defaultdict(list), Counter()
    for wid, (wt, title, ts) in seed_terms.items():
        for t in set(ts):
            idf = math.log(1 + n / df[t]) if df[t] else 0.0
            score[t] += wt * idf
            count[t] += 1
            examples[t].append((wt, short_title(title)))
    keep = {t: s for t, s in score.items() if count[t] >= 2 and df[t] < 0.6 * n}
    top = dict(sorted(keep.items(), key=lambda x: -x[1])[:MAX_TERMS])
    return {"weights": top, "examples": {t: [x[1] for x in sorted(examples[t], reverse=True)[:3]] for t in top}}


def scorer(prof, prefs_):
    """-> fn(terms) -> (score, why) or None when a 'less of' note matches. score ~0..2."""
    weights = prof.get("weights") or {}
    norm = sum(sorted(weights.values(), reverse=True)[:8]) or 1.0
    more, less = notes(prefs_.get("more")), notes(prefs_.get("less"))

    def fn(ts):
        ts = list(ts or [])
        if less and _hits(less, ts):
            return None
        hits = sorted(((weights[t], t) for t in set(ts) if t in weights), reverse=True)
        base = min(1.0, sum(w for w, _ in hits) / norm)
        liked = _hits(more, ts) if more else []
        why = ""
        if hits:
            t = hits[0][1]
            ex = prof["examples"].get(t) or []
            why = f"about {t}" + (f", like {' and '.join(ex[:2])}" if ex else "")
        elif liked:
            why = "matches what you said you want more of"
        return base + 0.6 * min(1, len(liked)), why
    return fn
