"""Query the built index: full-text search + filters + facets + sorting."""
import json
import re
import sqlite3

SORTS = {
    "title": "w.sort_title {d}",
    "added": "w.added {d}, w.sort_title",
    "year": "w.year {d}, w.sort_title",
    "author": "json_extract(w.authors,'$[0]') {d}, w.series, w.series_index, w.sort_title",
    "series": "w.series {d}, w.series_index, w.sort_title",
    "universe": "w.universe {d}, w.universe_index, w.sort_title",
    "duration": "w.duration {d}",
    "rating": "w.rating {d}",
    "progress": "w.progress {d}",
    "recent": "w.last_activity {d}, w.added {d}",
}
LIST_FACETS = ("formats", "genres", "libraries", "sources")
PARAM = {"formats": "format", "genres": "genre", "libraries": "library", "sources": "source"}


WORK_COLS = ("id, kind, title, sort_title, authors, narrators, series, series_index, year, description, genres, "
             "tags, cover, duration, added, hidden, adult, rating, formats, sources, libraries, universe, "
             "universe_index, series_key, info")


class _Con(sqlite3.Connection):
    works = "works"                               # table (or per-person view) holding the works


def connect(path, account_id=None):
    """Read-only index connection. With account_id, `works` progress/status/last_activity are
    that person's own (a temp view over user_progress); without, the owner-level values."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, factory=_Con)
    con.row_factory = sqlite3.Row
    if account_id is not None and con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='user_progress'").fetchone():
        cols = ", ".join(f"w0.{c.strip()}" for c in WORK_COLS.split(","))
        con.execute(f"""CREATE TEMP VIEW pw AS SELECT {cols}, up.progress AS progress,
                        COALESCE(up.status, 'unread') AS status, up.last_activity AS last_activity
                        FROM works w0 LEFT JOIN user_progress up ON up.work_id = w0.id AND up.account_id = {int(account_id)}""")
        con.works = "pw"
    return con


def _fts_query(q):
    """User text -> safe FTS5 query: every word must match, as a prefix."""
    words = re.findall(r"[\w']+", q, flags=re.UNICODE)
    return " ".join(f'"{w.replace(chr(34), "")}"*' for w in words)


def _csv(v):
    return [x for x in (v or "").split(",") if x]


def _where(p, adult_ok):
    """Build WHERE clauses from request params. Returns (sql list, args list, uses_fts)."""
    where, args = [], []
    q = (p.get("q") or "").strip()
    fts = bool(q and _fts_query(q))
    if fts:
        where.append("w.id IN (SELECT id FROM works_fts WHERE works_fts MATCH ?)")
        args.append(_fts_query(q))
    if p.get("hidden") != "1":
        where.append("w.hidden = 0")
    # Adult items: never without an unlocked session (server-side gate). Even when
    # unlocked they stay out of normal browsing and only appear in the adult section
    # (adult=only, or kind=scene) or when explicitly mixed in (adult=include).
    adult_mode = p.get("adult") or ""
    if not adult_ok:
        # locked: the private section is simply empty (not the normal library)
        where.append("0" if adult_mode == "only" else "w.adult = 0")
    elif adult_mode == "only" or "scene" in _csv(p.get("kind")):
        where.append("w.adult = 1")
    elif adult_mode != "include":
        where.append("w.adult = 0")
    for col, key in (("kind", "kind"), ("status", "status")):
        vals = _csv(p.get(key))
        if vals:
            where.append(f"w.{col} IN ({','.join('?' * len(vals))})")
            args += vals
    for col in LIST_FACETS:                        # any-of within a facet
        vals = _csv(p.get(PARAM[col]))
        if vals:
            where.append(f"EXISTS (SELECT 1 FROM json_each(w.{col}) j WHERE j.value IN ({','.join('?' * len(vals))}))")
            args += vals
    for f in _csv(p.get("has")):                   # all-of: e.g. has=ebook,audiobook
        where.append("EXISTS (SELECT 1 FROM json_each(w.formats) j WHERE j.value = ?)")
        args.append(f)
    for col, key in (("authors", "author"), ("narrators", "narrator")):
        v = (p.get(key) or "").strip()
        if v:
            where.append(f"EXISTS (SELECT 1 FROM json_each(w.{col}) j WHERE j.value LIKE ?)")
            args.append(f"%{v}%")
    if p.get("series"):
        where.append("w.series LIKE ?")
        args.append(f"%{p['series']}%")
    if p.get("in_series") == "1":
        where.append("w.series <> ''")
    if p.get("universe"):
        where.append("w.universe = ?")
        args.append(p["universe"])
    vals = _csv(p.get("availability"))              # in_library | partial | coming
    if vals:
        where.append(f"json_extract(w.info,'$.availability') IN ({','.join('?' * len(vals))})")
        args += vals
    for col, key, op in (("year", "year_min", ">="), ("year", "year_max", "<="),
                         ("duration", "dur_min", ">="), ("duration", "dur_max", "<="),
                         ("rating", "rating_min", ">=")):
        v = p.get(key)
        if v not in (None, ""):
            try:
                num = float(v) * (3600 if col == "duration" else 1)    # duration params are hours
            except ValueError:
                continue
            where.append(f"w.{col} {op} ?")
            args.append(num)
    if p.get("added_days"):
        where.append("w.added >= date('now', ?)")
        args.append(f"-{int(p['added_days'])} days")
    if p.get("linked") == "1":
        where.append("EXISTS (SELECT 1 FROM work_links l WHERE l.a = w.id)")
    return where, args, fts


def summary(r):
    return {
        "id": r["id"], "kind": r["kind"], "title": r["title"],
        "authors": json.loads(r["authors"] or "[]"), "series": r["series"], "series_index": r["series_index"],
        "year": r["year"], "cover": r["cover"], "duration": r["duration"], "progress": r["progress"],
        "status": r["status"], "formats": json.loads(r["formats"] or "[]"), "rating": r["rating"],
        "genres": json.loads(r["genres"] or "[]"), "adult": bool(r["adult"]), "hidden": bool(r["hidden"]),
        "universe": r["universe"] or "",
        "info": json.loads(r["info"] or "{}"),
    }


def search(path, p, adult_ok=False, account_id=None):
    con = connect(path, account_id)
    W = con.works
    try:
        where, args, fts = _where(p, adult_ok)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        sort = p.get("sort") or ("relevance" if fts else "title")
        desc = p.get("order", "desc" if sort in ("added", "rating", "progress", "recent") else "asc") == "desc"
        if sort == "relevance" and fts:
            order = "(SELECT bm25(works_fts, 0, 10, 5, 2, 4, 1, 2) FROM works_fts WHERE works_fts.id=w.id AND works_fts MATCH ?)"
            order_args = [_fts_query(p["q"])]
        else:
            order = SORTS.get(sort, SORTS["title"]).format(d="DESC" if desc else "ASC")
            order_args = []
        limit = max(1, min(int(p.get("limit") or 60), 500))
        offset = max(0, int(p.get("offset") or 0))
        total = con.execute(f"SELECT count(*) FROM {W} w {wsql}", args).fetchone()[0]
        rows = con.execute(f"SELECT w.* FROM {W} w {wsql} ORDER BY {order} NULLS LAST LIMIT ? OFFSET ?",
                           args + order_args + [limit, offset]).fetchall()
        facets = {}
        for col in ("kind", "status"):
            facets[col] = {r[0]: r[1] for r in con.execute(
                f"SELECT w.{col}, count(*) FROM {W} w {wsql} GROUP BY 1 ORDER BY 2 DESC", args)}
        facets["availability"] = {r[0]: r[1] for r in con.execute(
            f"SELECT json_extract(w.info,'$.availability'), count(*) FROM {W} w {wsql} "
            f"{'AND' if where else 'WHERE'} json_extract(w.info,'$.availability') <> '' GROUP BY 1", args)}
        facets["universes"] = {r[0]: r[1] for r in con.execute(
            f"SELECT w.universe, count(*) FROM {W} w {wsql} {'AND' if where else 'WHERE'} w.universe <> '' "
            "GROUP BY 1 ORDER BY 2 DESC", args)}
        for col in LIST_FACETS:
            facets[col] = {r[0]: r[1] for r in con.execute(
                f"SELECT j.value, count(*) FROM {W} w, json_each(w.{col}) j {wsql} GROUP BY 1 ORDER BY 2 DESC LIMIT 60",
                args)}
        facets["decade"] = {str(r[0]): r[1] for r in con.execute(
            f"SELECT (w.year/10)*10, count(*) FROM {W} w {wsql} {'AND' if where else 'WHERE'} w.year IS NOT NULL "
            "GROUP BY 1 ORDER BY 1 DESC", args)}
        return {"total": total, "offset": offset, "items": [summary(r) for r in rows], "facets": facets}
    finally:
        con.close()


def work(path, wid, adult_ok=False, account_id=None):
    con = connect(path, account_id)
    W = con.works
    try:
        r = con.execute(f"SELECT * FROM {W} WHERE id=?", (wid,)).fetchone()
        if not r or (r["adult"] and not adult_ok):
            return None
        out = summary(r)
        out.update({
            "description": r["description"], "narrators": json.loads(r["narrators"] or "[]"),
            "tags": json.loads(r["tags"] or "[]"), "libraries": json.loads(r["libraries"] or "[]"),
            "added": r["added"], "last_activity": r["last_activity"],
        })
        out["editions"] = [{
            "key": e["unit_key"], "source": e["source"], "source_id": e["source_id"], "format": e["format"], "title": e["title"],
            "url": e["url"], "duration": e["duration"], "progress": e["progress"],
            "finished": bool(e["finished"]), "hidden": bool(e["hidden"]), "library": e["library"],
            "matched_by": e["matched_by"], "extra": json.loads(e["extra"] or "{}"),
        } for e in con.execute("SELECT * FROM editions WHERE work_id=? ORDER BY format", (wid,))]
        out["related"] = [summary(x) for x in con.execute(
            f"SELECT w.* FROM work_links l JOIN {W} w ON w.id=l.b WHERE l.a=? AND (w.adult=0 OR ?)",
            (wid, int(adult_ok)))]
        out["series_works"] = []
        if r["series"]:
            out["series_works"] = [summary(x) for x in con.execute(
                f"SELECT * FROM {W} WHERE series_key=? AND kind=? AND hidden=0 ORDER BY series_index NULLS LAST, sort_title",
                (r["series_key"], r["kind"]))]
        out["universe_works"] = []
        if r["universe"]:
            out["universe_works"] = [summary(x) for x in con.execute(
                f"SELECT * FROM {W} WHERE universe=? AND hidden=0 ORDER BY universe_index NULLS LAST, sort_title",
                (r["universe"],))]
        return out
    finally:
        con.close()


def stats(path):
    con = connect(path)
    try:
        r = con.execute("SELECT v FROM meta WHERE k='stats'").fetchone()
        return json.loads(r[0]) if r else {}
    finally:
        con.close()
