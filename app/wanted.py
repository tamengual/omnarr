"""Wanted books: keep looking until an acceptable copy arrives (Shelfmark has no such list).

A wanted row is one book in one format (ebook | audiobook). A background tick:
  - marks it done once that format is in the library index,
  - follows its current Shelfmark download (complete / error / cancelled),
  - when due, searches Shelfmark again and queues the best ACCEPTABLE release it has not
    tried before. Failures retry soon (another release); "nothing acceptable yet" waits
    RETRY_DAYS before the next search. Nothing is ever given up on automatically.
"""
import json
import logging
import re
import sqlite3
import threading
import time

from . import normalize, requests_, search

log = logging.getLogger("omnarr.wanted")
RETRY_DAYS = 3
QUICK_RETRY = 15 * 60          # after a failed download, try the next release soon
STUCK_DAYS = 2                 # "complete" in Shelfmark but not in the library after this -> search again

FORMATS = {"ebook": ["epub", "kepub", "azw3", "mobi"], "audiobook": ["m4b", "m4a", "mp3"],
           # comic archives only: the server routes these to Komga (CWA ignores them and a mover
           # puts them in Komga's folder); an epub/pdf would land in Calibre as a book
           "comic": ["cbz", "cbr", "cb7"]}
SIZE = {"ebook": (40_000, 150_000_000), "audiobook": (30_000_000, 4_000_000_000),
        "comic": (500_000, 2_000_000_000)}


def shelfmark_type(fmt):
    """Shelfmark only knows ebook/audiobook; comics are searched as ebooks."""
    return "audiobook" if fmt == "audiobook" else "ebook"
PACK = re.compile(r"\b(collection|complete|series|saga|trilogy|omnibus|box\s*set|books?\s*\d+\s*(-|–|to|thru)\s*\d+|\d+\s*books)\b", re.I)
JUNK = re.compile(r"\b(summary|study guide|sparknotes|cliffs?notes|analysis of|workbook)\b", re.I)

SCHEMA = """CREATE TABLE IF NOT EXISTS wanted_books (
  id INTEGER PRIMARY KEY, work_id TEXT, title TEXT, author TEXT, format TEXT,
  provider TEXT, book_id TEXT, status TEXT, attempts INTEGER DEFAULT 0, tried TEXT DEFAULT '[]',
  current TEXT, current_title TEXT, created REAL, last_search REAL, next_search REAL, done_at REAL, note TEXT,
  account_id INTEGER)"""


def _con(state_path):
    con = sqlite3.connect(state_path)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    if "account_id" not in {r[1] for r in con.execute("PRAGMA table_info(wanted_books)")}:
        con.execute("ALTER TABLE wanted_books ADD COLUMN account_id INTEGER")      # who asked (0.3+)
    return con


def list_all(state_path):
    con = _con(state_path)
    try:
        has_accounts = con.execute("SELECT 1 FROM sqlite_master WHERE name='accounts'").fetchone()
        q = ("SELECT w.*, a.username AS requested_by FROM wanted_books w LEFT JOIN accounts a ON a.id=w.account_id"
             if has_accounts else "SELECT *, NULL AS requested_by FROM wanted_books w")
        rows = [dict(r) for r in con.execute(q + " ORDER BY w.status='done', w.created DESC")]
    finally:
        con.close()
    for r in rows:
        r["tried"] = len(json.loads(r["tried"] or "[]"))
    return rows


def add(state_path, title, author, fmt, work_id=None, provider=None, book_id=None, current=None, current_title=None,
        account_id=None):
    if fmt not in FORMATS:
        raise ValueError("format must be ebook, audiobook or comic")
    con = _con(state_path)
    try:
        dup = con.execute("""SELECT id FROM wanted_books WHERE format=? AND status<>'done' AND lower(title)=lower(?)""",
                          (fmt, title)).fetchone()
        now = time.time()
        if dup:
            wid = dup["id"]
            if current:
                con.execute("UPDATE wanted_books SET status='downloading', current=?, current_title=?, last_search=? WHERE id=?",
                            (current, current_title, now, wid))
        else:
            cur = con.execute("""INSERT INTO wanted_books (work_id, title, author, format, provider, book_id, status,
                                 current, current_title, created, last_search, next_search, note, account_id)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (work_id, title, author, fmt, provider, book_id,
                               "downloading" if current else "searching", current, current_title,
                               now, now if current else None, now, "", account_id))
            wid = cur.lastrowid
        con.commit()
        return wid
    finally:
        con.close()


def remove(state_path, wid):
    con = _con(state_path)
    with con:
        con.execute("DELETE FROM wanted_books WHERE id=?", (wid,))
    con.close()


def search_now(state_path, wid):
    con = _con(state_path)
    with con:
        con.execute("UPDATE wanted_books SET next_search=?, status=CASE WHEN status='done' THEN status ELSE 'searching' END WHERE id=?",
                    (time.time(), wid))
    con.close()


# ── release choice ───────────────────────────────────────────────────────────
def acceptable(rel, want_title, fmt, tried):
    if rel.get("source_id") in tried:
        return False, "already tried"
    t = rel.get("title") or ""
    f = (rel.get("format") or "").lower()
    if f and f not in FORMATS[fmt]:
        return False, f"format {f}"
    if not f and re.search(r"\bpdf\b", t, re.I) and not any(x in t.lower() for x in FORMATS[fmt]):
        return False, "pdf"
    # Unstated format is allowed (many torrents don't say); _rank tries stated formats first, and
    # a copy that turns out wrong fails in Shelfmark and is marked tried, so the next one is used.
    lang = (rel.get("language") or "").lower()
    if lang and lang not in ("en", "eng", "english"):
        return False, f"language {lang}"
    size = rel.get("size_bytes") or 0
    lo, hi = SIZE[fmt]
    if size and not (lo <= size <= hi):
        return False, "size"
    if JUNK.search(t):
        return False, "summary/guide"
    if PACK.search(t) and not PACK.search(want_title):
        return False, "pack/collection"
    # "Mythos / Heroes" (two books in one listing): the first title is enough
    want = normalize.normalize_word(normalize.display_title(re.split(r"\s+/\s+", want_title)[0]))
    if not want or want not in normalize.normalize_word(t):
        return False, "title mismatch"
    return True, ""


def _rank(rel, fmt):
    f = (rel.get("format") or "").lower()
    stated = f in FORMATS[fmt] or any(x in (rel.get("title") or "").lower() for x in FORMATS[fmt])
    pref = FORMATS[fmt].index(f) if f in FORMATS[fmt] else len(FORMATS[fmt])
    return (0 if stated else 1, -(rel.get("seeders") or 0), pref)


def _resolve_candidate(cfg, row):
    if row["provider"] and row["book_id"]:
        return row["provider"], row["book_id"]
    best, score = None, 0.0
    first = re.split(r"\s+/\s+", row["title"])[0]
    # title + author first; then title alone (metadata search sometimes chokes on initials like "F. C.")
    for query in (" ".join(x for x in (first, row["author"]) if x), first):
        for c in requests_.book_candidates(cfg, query, shelfmark_type(row["format"])):
            s = normalize.same_book(first, row["author"] or "", c["title"] or "", (c["authors"] or [""])[0])
            if s > score:
                best, score = c, s
        if score >= normalize.THRESHOLD:
            return best["provider"], str(best["book_id"])
    return None, None


# ── the tick ─────────────────────────────────────────────────────────────────
def _in_library(index_path, row):
    con = search.connect(index_path)
    try:
        # a comic counts as arrived in Komga (comic) or Calibre (ebook)
        fmts = ("comic", "ebook") if row["format"] == "comic" else (row["format"],)
        if row["work_id"]:
            r = con.execute("SELECT formats FROM works WHERE id=?", (row["work_id"],)).fetchone()
            if r and set(fmts) & set(json.loads(r["formats"] or "[]")):
                return True
        sn = normalize.surname(row["author"] or "")
        like = " OR ".join("formats LIKE ?" for _ in fmts)
        for r in con.execute(f"SELECT title, authors, formats FROM works WHERE kind IN ('book','comic') AND ({like})",
                             [f'%"{f}"%' for f in fmts]):
            a = (json.loads(r["authors"] or "[]") or [""])[0]
            if (not sn or normalize.surname(a) == sn) and normalize.same_book(row["title"], row["author"] or "", r["title"], a) >= normalize.THRESHOLD:
                return True
    finally:
        con.close()
    return False


def _shelfmark_state(snapshot, current):
    for bucket, items in (snapshot.get("status") or {}).items():
        for key, it in (items or {}).items():
            if current in (key, it.get("id")):
                return bucket, it.get("status_message") or ""
    return None, ""


def _route(cfg, fmt):
    from . import extend
    route = extend.requester(cfg, fmt)
    if route[0] == "builtin" and not requests_.shelfmark_enabled(cfg):
        return None
    return route


def tick(cfg, state_path, index_path):
    from . import extend
    if not requests_.shelfmark_enabled(cfg) and not extend.any_requester(cfg, FORMATS):
        return
    con = _con(state_path)
    rows = [dict(r) for r in con.execute("SELECT * FROM wanted_books WHERE status<>'done'")]
    con.close()
    if not rows:
        return
    snapshot = None
    now = time.time()
    for row in rows:
        upd = {}
        try:
            if _in_library(index_path, row):
                upd = {"status": "done", "done_at": now, "note": "in your library"}
            elif row["status"] == "downloading" and (row["current"] or "").startswith("rmab:"):
                upd = _follow_readmeabook(cfg, row, now)
            elif row["status"] == "downloading" and (row["current"] or "").startswith("sent:"):
                pass                                   # handed to your own app/plug-in: wait for it to arrive
            elif row["status"] == "downloading" and row["current"]:
                if snapshot is None:
                    snapshot = requests_.shelfmark_activity(cfg)
                bucket, msg = _shelfmark_state(snapshot, row["current"])
                if bucket in ("error", "cancelled"):
                    tried = json.loads(row["tried"] or "[]") + [row["current"]]
                    upd = {"status": "searching", "tried": json.dumps(tried), "current": None,
                           "next_search": now + QUICK_RETRY, "note": f"download {bucket}: {msg}"[:200]}
                elif bucket == "complete" and row["format"] == "comic":
                    # comic archives go to Komga (file names rarely match the title well enough to
                    # spot them in the index), so a finished download is the success signal
                    from .connectors import komga
                    scanned = komga.scan_all(cfg, state_path)
                    if scanned:                        # the mover runs every few minutes: scan again later
                        timer = threading.Timer(600, komga.scan_all, args=(cfg, state_path))
                        timer.daemon = True            # never keeps Omnarr (or a test run) from exiting
                        timer.start()
                    upd = {"status": "done", "done_at": now,
                           "note": "downloaded; sent to Komga" + ("" if scanned else " (Komga not connected: scan it yourself)")}
                elif bucket == "complete" and now - (row["last_search"] or now) > STUCK_DAYS * 86400:
                    tried = json.loads(row["tried"] or "[]") + [row["current"]]
                    upd = {"status": "searching", "tried": json.dumps(tried), "current": None, "next_search": now,
                           "note": "downloaded but never reached the library; trying another copy"}
            elif row["status"] == "searching" and (row["next_search"] or 0) <= now:
                upd = _search_once(cfg, row, now)
        except Exception as e:
            log.warning("wanted %s (%s) failed: %s", row["id"], row["title"], e)
            upd = {"next_search": now + 3600, "note": f"error: {type(e).__name__}: {e}"[:200]}
        if upd.get("status") == "done":
            from . import notify
            notify.send(cfg, state_path, "request_ready", f"Ready: {row['title']}",
                        f"{row['title']} ({row['format']}) is now in the library.",
                        to=[row["account_id"]] if row.get("account_id") else ())
        if upd:
            con = _con(state_path)
            with con:
                con.execute(f"UPDATE wanted_books SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                            list(upd.values()) + [row["id"]])
            con.close()
            log.info("wanted '%s' (%s): %s", row["title"], row["format"], upd.get("note") or upd.get("status"))


def _follow_readmeabook(cfg, row, now):
    from .connectors import readmeabook
    state, progress, err = readmeabook.status(cfg.source("readmeabook"), row["current"][5:])
    if state in readmeabook.FAILED | readmeabook.CANCELLED:
        tried = json.loads(row["tried"] or "[]") + [row["current"]]
        why = f"ReadMeABook: {state}" + (f": {err}" if err else "")
        return {"status": "searching", "tried": json.dumps(tried), "current": None,
                "next_search": now + RETRY_DAYS * 86400, "note": f"{why}; trying again in {RETRY_DAYS} days"[:200]}
    if state == "awaiting_approval":
        return {"note": "waiting for approval in ReadMeABook"}
    if state in readmeabook.DONE:
        return {"note": "ReadMeABook: downloaded; waiting for it to appear in Audiobookshelf"}
    return {"note": f"ReadMeABook: {state.replace('_', ' ')}" + (f" ({int(progress)}%)" if progress else "")}


def _hand_off(cfg, row, now, route):
    """Requests for this format go somewhere other than Shelfmark (ReadMeABook, your URL, a plug-in)."""
    from . import extend
    if route[0] == "readmeabook":
        from .connectors import readmeabook
        s = cfg.source("readmeabook")
        book = readmeabook.best_match(s, re.split(r"\s+/\s+", row["title"])[0], row["author"] or "")
        if not book:
            return {"last_search": now, "next_search": now + RETRY_DAYS * 86400,
                    "note": f"not found on Audible via ReadMeABook yet; next try in {RETRY_DAYS} days"}
        rid, state, msg = readmeabook.request(s, book)
        if state in readmeabook.DONE and not rid:
            return {"last_search": now, "next_search": now + 86400, "note": "ReadMeABook says it's already in your library"}
        return {"status": "downloading", "current": f"rmab:{rid}", "current_title": book.get("title"), "last_search": now,
                "attempts": (row["attempts"] or 0) + 1, "note": msg[:200]}
    item = {"format": row["format"], "title": row["title"], "authors": [row["author"]] if row["author"] else [],
            "work_id": row["work_id"], "requested_by": row.get("account_id")}
    out = extend.send(cfg, route, item)
    where = "your request URL" if route[0] == "webhook" else extend.PLUGINS[route[1]]["spec"]["label"]
    return {"status": "downloading", "current": "sent:" + str(out.get("external_id") or ""), "last_search": now,
            "attempts": (row["attempts"] or 0) + 1, "note": f"sent to {where}: {out.get('message') or 'ok'}"[:200]}


def _search_once(cfg, row, now):
    route = _route(cfg, row["format"])
    if route is None:
        return {"last_search": now, "next_search": now + RETRY_DAYS * 86400, "note": "nothing is set up to request this format"}
    if route[0] != "builtin":
        return _hand_off(cfg, row, now, route)
    provider, book_id = _resolve_candidate(cfg, row)
    if not provider:
        return {"last_search": now, "next_search": now + RETRY_DAYS * 86400, "note": "book not found in metadata search yet"}
    tried = set(json.loads(row["tried"] or "[]"))
    releases = requests_.book_releases(cfg, provider, book_id, shelfmark_type(row["format"]))
    good, reasons = [], {}
    for r in releases:
        ok, why = acceptable(r, row["title"], row["format"], tried)
        if ok:
            good.append(r)
        else:
            reasons[why] = reasons.get(why, 0) + 1
    base = {"provider": provider, "book_id": book_id, "last_search": now, "attempts": (row["attempts"] or 0) + 1}
    if not good:
        why = ", ".join(f"{n} {k}" for k, n in sorted(reasons.items(), key=lambda x: -x[1])[:3]) or "no releases"
        return {**base, "next_search": now + RETRY_DAYS * 86400,
                "note": f"no acceptable copy yet ({len(releases)} found: {why}); next try in {RETRY_DAYS} days"}
    pick = sorted(good, key=lambda r: _rank(r, row["format"]))[0]
    payload = dict(pick)
    payload.setdefault("content_type", shelfmark_type(row["format"]))
    requests_.book_download(cfg, payload)
    return {**base, "status": "downloading", "current": pick.get("source_id"), "current_title": pick.get("title"),
            "note": f"downloading: {pick.get('title')} ({pick.get('format') or '?'}, {pick.get('size') or '?'})"[:200]}
