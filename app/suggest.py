"""Suggestions ("For you"): what to read, listen to or watch next, per person.

Everything here is explainable and stays on the server. It learns only from the person's own
history in Omnarr: what they finished and are in the middle of, plus (for admins) the owner's
own Calibre star ratings. Every suggestion carries the reason it was picked.

Sections, strongest first:
  up_next   the next book/issue in a series you're reading (owned, or not owned yet)
  library   things you own but haven't started that match your authors, worlds and genres
  screen    movies and shows like the ones you have (TMDB recommendations through Seerr)
  worlds    adaptations and companion works of what you loved (Wikidata, already used for
            the "related" lists on work pages)

Scoring only reads the index; the two outside lookups (related works, TMDB recommendations)
are passed in so they can be cached, budgeted and tested.
"""
import json
import math
import re
import time
from collections import Counter, defaultdict

from . import normalize
from .search import summary

KINDS = ("book", "comic", "movie", "show")
BUNDLE = re.compile(r"\b(box(ed)? set|bundle|omnibus|collection|books? \d+\s*[-–]\s*\d+|\d+-book)\b", re.I)
PER_SECTION = 18
VERB = {"book": "read", "comic": "read", "movie": "watched", "show": "watched"}


def _age_days(iso):
    if not iso:
        return None
    try:
        return max(0.0, (time.time() - time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400)
    except ValueError:
        return None


def _rating_factor(stars):
    """Calibre stars are 0-10 (half-stars). None = unrated."""
    if stars is None:
        return 1.0
    if stars >= 9:
        return 2.0
    if stars >= 8:
        return 1.6
    if stars >= 6:
        return 1.0
    if stars >= 4:
        return 0.5
    return -0.6                                   # disliked: steer away from its authors/genres


def load(con, account_id, use_owner_ratings):
    """Works (non-adult, visible) with this person's status, and their seeds with weights."""
    works = {}
    for r in con.execute(f"SELECT * FROM {con.works} WHERE adult=0 AND hidden=0 AND kind IN ({','.join('?' * len(KINDS))})", KINDS):
        works[r["id"]] = dict(r)
    stars = {}
    if use_owner_ratings:
        for wid, s in con.execute("""SELECT work_id, max(json_extract(extra, '$.my_rating')) FROM editions
                                     WHERE source='calibre' AND json_extract(extra, '$.my_rating') IS NOT NULL GROUP BY work_id"""):
            stars[wid] = s
    seeds = {}
    for wid, w in works.items():
        status = w.get("status") or "unread"
        if use_owner_ratings and w["kind"] in ("movie", "show") and status == "unread":
            # An owner's screen library is things they've seen and liked enough to keep a copy
            # of: treat every owned movie/show as watched and liked.
            w["status"] = status = "finished"
        if status not in ("finished", "in_progress"):
            continue
        base = 1.0 if status == "finished" else 0.8
        age = _age_days(w.get("last_activity"))
        recency = 0.6 if age is None else 0.5 + 0.5 * math.exp(-age / 365)
        seeds[wid] = base * recency * _rating_factor(stars.get(wid))
    return works, seeds


def profile(works, seeds):
    """Weighted authors, genres (lifted against how common they are in the library), universes."""
    authors, genres, universes = Counter(), Counter(), Counter()
    by_author = defaultdict(list)                  # author -> [(weight, work id)] for reasons
    lib_genres = Counter(g for w in works.values() for g in json.loads(w.get("genres") or "[]"))
    for wid, wt in seeds.items():
        w = works[wid]
        for pos, a in enumerate(json.loads(w.get("authors") or "[]")):
            key = a.strip().lower()
            authors[key] += wt
            by_author[key].append((pos == 0, wt, wid))     # their own books first when explaining
        for g in json.loads(w.get("genres") or "[]"):
            genres[g] += wt / math.sqrt(max(1, lib_genres[g]))
        if w.get("universe"):
            universes[w["universe"]] += wt
    return authors, genres, universes, by_author


def _card(w, reason, score):
    return {"work": summary(w), "reason": reason, "score": round(score, 3)}


def up_next(works, seeds):
    """The next unstarted entry in each series the person has read from, nearest first."""
    by_series = defaultdict(list)
    for wid, w in works.items():
        if w.get("series_key") and w.get("series_index") is not None:
            by_series[w["series_key"]].append(w)
    picks = []
    for key, members in by_series.items():
        done = [w for w in members if w["id"] in seeds]
        if not done:
            continue
        last = max(done, key=lambda w: w["series_index"])
        ahead = sorted((w for w in members if w["series_index"] > last["series_index"]
                        and (w.get("status") or "unread") == "unread"), key=lambda w: w["series_index"])
        if not ahead:
            continue
        nxt = ahead[0]
        weight = max(seeds[w["id"]] for w in done)
        picks.append(_card(nxt, f"Next in {last['series'] or 'the series'} after {last['title']}", 10 + weight))
    picks.sort(key=lambda c: -c["score"])
    return picks[:PER_SECTION]


def from_library(works, seeds, prof, skip=()):
    """Owned, unstarted works scored by shared authors, worlds and genres."""
    authors, genres, universes, by_author = prof
    top_genre = max(genres.values(), default=0) or 1
    started_series = {works[s].get("series_key") for s in seeds if works[s].get("series_key")}
    # Saturating, not relative: one finished book by an author already counts strongly, and an
    # author you've read fifteen of doesn't drown out everyone else.
    sat = lambda x: 1 - math.exp(-max(0.0, x) / 0.8) if x > 0 else max(-1.0, x)
    scored = []
    for wid, w in works.items():
        if wid in seeds or wid in skip or (w.get("status") or "unread") != "unread":
            continue
        if w.get("series_key") in started_series or BUNDLE.search(w.get("title") or ""):
            continue                               # "Up next"'s job / box sets of what you've read
        names = [a.strip().lower() for a in json.loads(w.get("authors") or "[]")]
        a_score = sat(max((authors.get(n, 0) for n in names), default=0))
        g_list = json.loads(w.get("genres") or "[]")
        g_score = min(1.0, sum(max(0, genres.get(g, 0)) for g in g_list) / top_genre)
        u_score = sat(universes.get(w.get("universe") or "", 0))
        r = w.get("rating")                        # public average: books ~1-5, screens ~1-10
        r_score = 0.0 if not r else min(1.0, max(0.0, ((r / 2 if r > 5 else r) - 3.5) / 1.2))
        score = 3.0 * a_score + 1.5 * g_score + 2.0 * u_score + 0.6 * r_score
        if score <= 0.6:
            continue
        parts = {"author": 3.0 * a_score, "genre": 1.5 * g_score, "universe": 2.0 * u_score}
        why = max(parts, key=parts.get)
        if why == "author":
            n = max(names, key=lambda n: authors.get(n, 0))
            seed = works[max(by_author[n])[2]]
            reason = f"Because you {VERB.get(seed['kind'], 'read')} {seed['title']}"
        elif why == "universe":
            reason = f"From the {w['universe']} world"
        else:
            g = max(g_list, key=lambda g: genres.get(g, 0))
            reason = f"Because you like {g}"
        scored.append((score, w, reason, names))
    scored.sort(key=lambda x: -x[0])
    out, per_author = [], Counter()
    for score, w, reason, names in scored:
        lead = names[0] if names else ""
        if lead and per_author[lead] >= 2:
            continue                               # variety: two per author at most
        per_author[lead] += 1
        out.append(_card(w, reason, score))
        if len(out) >= PER_SECTION:
            break
    return out


def top_seeds(works, seeds, kinds, n):
    return sorted((wid for wid in seeds if works[wid]["kind"] in kinds and seeds[wid] > 0),
                  key=lambda wid: -seeds[wid])[:n]


def screen_picks(works, seeds, recs_for, match_library, owned_screens, extra_seeds=()):
    """Movies/shows recommended (TMDB, via Seerr) for screens you have or watched, and for the
    screen adaptations of books you loved. recs_for(kind, tmdb) -> [{kind, tmdb, title, year,
    poster, status}]. owned_screens: [(work id, kind 'movie'|'tv', tmdb, weight)]."""
    tally = {}
    for wid, kind, tmdb, weight, label in list(owned_screens) + list(extra_seeds):
        for rec in recs_for(kind, tmdb) or []:
            if match_library(rec):
                continue
            k = (rec["kind"], str(rec["tmdb"]))
            t = tally.setdefault(k, {"item": rec, "score": 0.0, "because": []})
            t["score"] += weight
            t["because"].append((weight, label))
    out = []
    for t in sorted(tally.values(), key=lambda t: -t["score"])[:PER_SECTION]:
        label = max(t["because"])[1]
        out.append({"external": t["item"], "reason": f"Because of {label}", "score": round(t["score"], 3)})
    return out


def worlds(works, seeds, related_for, match_library, n_seeds=8, skip_tmdb=()):
    """Adaptations and companions of loved books/comics/screens that you don't have yet."""
    out, seen = [], set(skip_tmdb)
    for wid in top_seeds(works, seeds, ("book", "comic", "movie", "show"), n_seeds):
        seed = works[wid]
        for it in related_for(seed) or []:
            if it["kind"] not in ("movie", "tv", "book", "comic") or match_library(it):
                continue
            key = (it["kind"], str(it.get("tmdb") or it.get("wikidata") or it["label"]))
            if key in seen:
                continue
            seen.add(key)
            out.append({"external": {"kind": it["kind"], "tmdb": it.get("tmdb"), "title": it["label"], "year": it.get("year"),
                                     "authors": it.get("authors") or [], "url": it.get("url"), "wikidata": it.get("wikidata"),
                                     "status": it.get("status") or "unknown", "poster": it.get("poster") or ""},
                        "reason": f"From the world of {seed['title']}", "score": round(seeds[wid], 3)})
            if len(out) >= PER_SECTION:
                return out
    return out


def series_gaps(works, seeds, related_for, match_library, n_series=6):
    """Next books in series you finished where you don't own the next one (via Wikidata):
    the earliest related book published after your latest one in that series."""
    by_series = defaultdict(list)
    for wid in seeds:
        w = works[wid]
        if w["kind"] in ("book", "comic") and w.get("series_key"):
            by_series[w["series_key"]].append(w)
    owned_ahead = set()
    for w in works.values():
        if w.get("series_key") in by_series and w.get("series_index") is not None:
            last = max(by_series[w["series_key"]], key=lambda x: x.get("series_index") or 0)
            if w["series_index"] > (last.get("series_index") or 0):
                owned_ahead.add(w["series_key"])
    picks = []
    order = sorted(by_series, key=lambda k: -max(seeds[w["id"]] for w in by_series[k]))
    for key in [k for k in order if k not in owned_ahead][:n_series]:
        last = max(by_series[key], key=lambda x: (x.get("series_index") or 0, x.get("year") or 0))
        later = [it for it in related_for(last) or [] if it["kind"] == last["kind"] and it.get("year")
                 and last.get("year") and it["year"] >= last["year"] and not match_library(it)
                 and normalize.key(it["label"]) != normalize.key(last["title"])]
        if not later:
            continue
        nxt = min(later, key=lambda it: it["year"])
        picks.append({"external": {"kind": nxt["kind"], "title": nxt["label"], "year": nxt.get("year"), "authors": nxt.get("authors") or [],
                                   "url": nxt.get("url"), "wikidata": nxt.get("wikidata"), "status": "not_requested", "poster": ""},
                      "reason": f"Next in {last['series'] or 'the series'} after {last['title']}", "score": round(5 + seeds[last['id']], 3)})
    return picks
