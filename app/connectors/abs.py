"""Audiobookshelf: audiobooks + the user's listening progress.

API mode (default, needs an API key): the library listing doesn't include audio-file
inodes (ABS 2.37), so each item's details are fetched once and cached in the state DB,
re-fetched only when the item's updatedAt changes.
Database mode (legacy): reads absdatabase.sqlite through a read-only mount.
"""
import json
import os
import sqlite3
import time

import httpx

from .base import Unit, jlist, ro_connect, year_of


def api_mode(s):
    return bool(s and s.get("api_key") and s.get("url"))


def client(s, timeout=60):
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=timeout,
                        headers={"Authorization": f"Bearer {s['api_key']}"})


def _cache_con(cfg):
    con = sqlite3.connect(cfg.state_path, timeout=15)
    con.execute("CREATE TABLE IF NOT EXISTS abs_item_cache (item_id TEXT PRIMARY KEY, updated_at TEXT, detail TEXT)")
    return con


def item_detail(cfg, c, item_id, updated_at=None):
    """Expanded item JSON, from cache when updatedAt hasn't changed."""
    con = _cache_con(cfg)
    try:
        row = con.execute("SELECT updated_at, detail FROM abs_item_cache WHERE item_id=?", (item_id,)).fetchone()
        if row and (updated_at is None or str(updated_at) == row[0]):
            return json.loads(row[1])
        d = c.get(f"/api/items/{item_id}", params={"expanded": 1}).json()
        with con:
            con.execute("INSERT OR REPLACE INTO abs_item_cache VALUES (?,?,?)", (item_id, str(updated_at or d.get("updatedAt")), json.dumps(d)))
        return d
    finally:
        con.close()


def read(cfg):
    s = cfg.source("abs")
    if not s:
        return []
    return _read_api(cfg, s) if api_mode(s) else _read_db(cfg, s)


def _read_api(cfg, s):
    base = cfg.link("abs")
    units = []
    with client(s) as c:
        progress = {p["libraryItemId"]: p for p in (c.get("/api/me").json().get("mediaProgress") or []) if p.get("libraryItemId")}
        for lib in c.get("/api/libraries").json().get("libraries", []):
            if lib.get("mediaType") != "book":
                continue
            page = 0
            while True:
                d = c.get(f"/api/libraries/{lib['id']}/items", params={"limit": 200, "page": page}).json()
                rows = d.get("results") or []
                for it in rows:
                    if it.get("isMissing") or it.get("isInvalid") or not (it.get("media") or {}).get("numAudioFiles"):
                        continue
                    det = item_detail(cfg, c, it["id"], it.get("updatedAt"))
                    m = det.get("media") or {}
                    md = m.get("metadata") or {}
                    files = m.get("audioFiles") or []
                    ser = (md.get("series") or [{}])[0] if md.get("series") else {}
                    try:
                        seq = float(ser.get("sequence")) if ser.get("sequence") not in (None, "") else None
                    except ValueError:
                        seq = None
                    p = progress.get(it["id"])
                    pct = (min(1.0, (p.get("currentTime") or 0) / p["duration"]) if p and p.get("duration") else None)
                    added = det.get("addedAt")
                    units.append(Unit(
                        source="abs", source_id=it["id"], kind="book", format="audiobook",
                        title=md.get("title") or "", authors=[a.get("name") for a in md.get("authors") or [] if a.get("name")],
                        narrators=md.get("narrators") or [], series=ser.get("name") or "", series_index=seq,
                        year=year_of(md.get("publishedYear")), description=(md.get("description") or "").strip(),
                        genres=md.get("genres") or [], tags=m.get("tags") or [],
                        url=f"{base}/item/{it['id']}" if base else "",
                        cover=f"abs:{it['id']}" if m.get("coverPath") else "",
                        duration=m.get("duration"),
                        added=time.strftime("%Y-%m-%d", time.gmtime(added / 1000)) if added else "",
                        progress=pct, finished=bool(p and p.get("isFinished")),
                        library=lib.get("name") or "Audiobookshelf",
                        ids={k: v for k, v in (("isbn", md.get("isbn")), ("audible", md.get("asin"))) if v},
                        match={"inodes": [str(f.get("ino")) for f in files if f.get("ino") is not None]},
                        extra={"subtitle": md.get("subtitle") or "", "cover_path": m.get("coverPath") or "",
                               "last_listened": str(p.get("lastUpdate")) if p else ""},
                    ))
                page += 1
                if not rows or page * 200 >= (d.get("total") or 0):
                    break
    return units


def _read_db(cfg, s):
    con = ro_connect(s["db"])
    base = cfg.link("abs")                        # e.g. http://host:13378/audiobookshelf

    authors, series, progress = {}, {}, {}
    for r in con.execute("""SELECT ba.bookId, a.name FROM bookAuthors ba JOIN authors a ON a.id=ba.authorId
                            ORDER BY ba.createdAt"""):
        authors.setdefault(r["bookId"], []).append(r["name"])
    for r in con.execute("""SELECT bs.bookId, s.name, bs.sequence FROM bookSeries bs JOIN series s ON s.id=bs.seriesId"""):
        series.setdefault(r["bookId"], (r["name"], r["sequence"]))
    user = s.get("user")
    if user:
        for r in con.execute("""SELECT p.mediaItemId, p.currentTime, p.duration, p.isFinished, p.updatedAt
                                FROM mediaProgresses p JOIN users u ON u.id=p.userId WHERE u.username=?""", (user,)):
            progress[r["mediaItemId"]] = r

    units = []
    for r in con.execute("""SELECT li.id AS item_id, li.createdAt, li.libraryId, l.name AS library,
                                   b.id AS book_id, b.title, b.subtitle, b.publishedYear, b.description, b.isbn, b.asin,
                                   b.narrators, b.genres, b.tags, b.duration, b.coverPath, b.audioFiles
                            FROM libraryItems li JOIN books b ON b.id=li.mediaId
                            LEFT JOIN libraries l ON l.id=li.libraryId
                            WHERE li.mediaType='book' AND COALESCE(li.isMissing,0)=0 AND COALESCE(li.isInvalid,0)=0"""):
        files = jlist(r["audioFiles"])
        if not files:
            continue                               # ebook-only ABS item; Calibre is the ebook source
        sname, seq = series.get(r["book_id"], ("", None))
        try:
            seq = float(seq) if seq not in (None, "") else None
        except ValueError:
            seq = None
        p = progress.get(r["book_id"])
        pct = None
        if p is not None and (p["duration"] or 0) > 0:
            pct = min(1.0, (p["currentTime"] or 0) / p["duration"])
        u = Unit(
            source="abs", source_id=r["item_id"], kind="book", format="audiobook",
            title=r["title"] or "", authors=authors.get(r["book_id"], []),
            narrators=jlist(r["narrators"]), series=sname, series_index=seq,
            year=year_of(r["publishedYear"]), description=(r["description"] or "").strip(),
            genres=jlist(r["genres"]), tags=jlist(r["tags"]),
            url=f"{base}/item/{r['item_id']}" if base else "",
            cover=f"abs:{r['item_id']}" if r["coverPath"] else "",
            duration=r["duration"], added=str(r["createdAt"] or "")[:10],
            progress=pct, finished=bool(p and p["isFinished"]),
            library=r["library"] or "Audiobookshelf",
            ids={k: v for k, v in (("isbn", r["isbn"]), ("audible", r["asin"])) if v},
            match={"inodes": [str(f.get("ino")) for f in files if f.get("ino") is not None]},
            extra={"subtitle": r["subtitle"] or "", "cover_path": r["coverPath"] or "",
                   "last_listened": str(p["updatedAt"]) if p is not None else ""},
        )
        units.append(u)
    con.close()
    return units


def cover_bytes(cfg, item_id):
    """API mode: the cover image from ABS itself."""
    s = cfg.source("abs") or {}
    with client(s, timeout=30) as c:
        r = c.get(f"/api/items/{item_id}/cover")
    return r.content if r.status_code == 200 else None


def cover_file(cfg, item_id):
    """Database mode: ABS stores covers as container paths (/metadata/items/<id>/cover.jpg or
    inside the book folder). Map them through `path_map` to our mounts."""
    s = cfg.source("abs") or {}
    if not s.get("db"):
        return None
    con = ro_connect(s["db"])
    try:
        r = con.execute("SELECT b.coverPath FROM libraryItems li JOIN books b ON b.id=li.mediaId WHERE li.id=?",
                        (item_id,)).fetchone()
    finally:
        con.close()
    if not r or not r["coverPath"]:
        return None
    path = r["coverPath"]
    for src, dst in (s.get("path_map") or {}).items():
        if path.startswith(src.rstrip("/") + "/"):
            return os.path.join(dst, path[len(src.rstrip("/")) + 1:])
    return None
