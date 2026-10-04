"""Komga: comic / manga series.

API mode (default): /api/v1 with an API key (X-API-Key).
Database mode (legacy): reads database.sqlite through a read-only mount.
"""
import httpx

from .base import Unit, ro_connect, year_of

AUTHOR_ROLES = ("writer", "author", "penciller", "artist", "")
ADULT_AGE = 18   # Komga age rating (ComicInfo "Adults Only 18+" sets 18): behind the private-section PIN


def api_mode(s):
    return bool(s and s.get("api_key") and s.get("url"))


def client(s, timeout=60):
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=timeout, headers={"X-API-Key": s["api_key"]})


def read(cfg):
    s = cfg.source("komga")
    if not s:
        return []
    return _read_api(cfg, s) if api_mode(s) else _read_db(cfg, s)


def _pages(c, path, method="get"):
    page = 0
    while True:
        if method == "post":
            d = c.post(path, params={"page": page, "size": 500}, json={}).json()
        else:
            d = c.get(path, params={"page": page, "size": 500}).json()
        yield from d.get("content") or []
        page += 1
        if d.get("last", True):
            return


def _read_api(cfg, s):
    """One Unit per book (a graphic novel or a volume/issue), grouped by its Komga series."""
    base = cfg.link("komga")
    units = []
    with client(s) as c:
        libs = {l["id"]: l.get("name") for l in c.get("/api/v1/libraries").json()}
        series = {r["id"]: r for r in _pages(c, "/api/v1/series")}
        for b in _pages(c, "/api/v1/books/list", "post"):
            if b.get("deleted"):
                continue
            m = b.get("metadata") or {}
            sr = series.get(b.get("seriesId")) or {}
            sm = sr.get("metadata") or {}
            authors = []
            for a in m.get("authors") or (sr.get("booksMetadata") or {}).get("authors") or []:
                if (a.get("role") or "").lower() in AUTHOR_ROLES and a.get("name") not in authors:
                    authors.append(a["name"])
            series_title = sm.get("title") or b.get("seriesTitle") or ""
            one_shot = (sr.get("booksCount") or 1) == 1 and not sm.get("title")
            units.append(Unit(
                source="komga", source_id=b["id"], kind="comic", format="comic",
                title=m.get("title") or b.get("name") or "", authors=authors,
                series="" if one_shot else series_title, series_index=m.get("numberSort"),
                year=year_of(m.get("releaseDate")),
                description=(m.get("summary") or sm.get("summary") or "").strip(),
                genres=sm.get("genres") or [], tags=m.get("tags") or [],
                adult=(sm.get("ageRating") or 0) >= ADULT_AGE,
                url=f"{base}/book/{b['id']}" if base else "",
                cover=f"komga:{b['id']}",
                added=str(b.get("created") or "")[:10], library=libs.get(b.get("libraryId")) or "Komga",
                ids={"isbn": m["isbn"]} if m.get("isbn") else {},
                extra={"pages": (b.get("media") or {}).get("pagesCount"), "publisher": sm.get("publisher") or "",
                       "series_id": b.get("seriesId")},
            ))
    return units


def scan_all(cfg, state_path=None):
    """Ask Komga to scan every library (it picks up new files right away instead of at its
    scheduled scan). Returns True when the request was sent. Audited."""
    s = cfg.source("komga") or {}
    if not api_mode(s):
        return False
    with client(s, timeout=30) as c:
        libs = c.get("/api/v1/libraries").json()
        for lib in libs:
            c.post(f"/api/v1/libraries/{lib['id']}/scan")
    if state_path:
        from .. import live
        live._audit(state_path, "komga.scan", {"libraries": [lib.get("name") for lib in libs]}, "ok")
    return True


def cover_bytes(cfg, book_id):
    s = cfg.source("komga") or {}
    if not api_mode(s):
        return None
    with client(s, timeout=30) as c:
        r = c.get(f"/api/v1/books/{book_id}/thumbnail")
        if r.status_code == 404:                      # older index entries hold series ids
            r = c.get(f"/api/v1/series/{book_id}/thumbnail")
    return r.content if r.status_code == 200 else None


def _read_db(cfg, s):
    con = ro_connect(s["db"])
    base = cfg.link("komga")
    authors, genres, agg = {}, {}, {}
    for r in con.execute("SELECT SERIES_ID, NAME, ROLE FROM BOOK_METADATA_AGGREGATION_AUTHOR"):
        if (r["ROLE"] or "").lower() in AUTHOR_ROLES:
            authors.setdefault(r["SERIES_ID"], [])
            if r["NAME"] not in authors[r["SERIES_ID"]]:
                authors[r["SERIES_ID"]].append(r["NAME"])
    for r in con.execute("SELECT SERIES_ID, GENRE FROM SERIES_METADATA_GENRE"):
        genres.setdefault(r["SERIES_ID"], []).append(r["GENRE"])
    for r in con.execute("SELECT SERIES_ID, RELEASE_DATE, SUMMARY FROM BOOK_METADATA_AGGREGATION"):
        agg[r["SERIES_ID"]] = r
    units = []
    for r in con.execute("""SELECT s.ID, s.NAME, s.BOOK_COUNT, s.CREATED_DATE, l.NAME AS LIB,
                                   m.TITLE, m.SUMMARY, m.PUBLISHER, m.AGE_RATING
                            FROM SERIES s LEFT JOIN SERIES_METADATA m ON m.SERIES_ID=s.ID
                            LEFT JOIN LIBRARY l ON l.ID=s.LIBRARY_ID
                            WHERE s.DELETED_DATE IS NULL"""):
        a = agg.get(r["ID"])
        units.append(Unit(
            source="komga", source_id=r["ID"], kind="comic", format="comic",
            title=r["TITLE"] or r["NAME"] or "", authors=authors.get(r["ID"], []),
            year=year_of(a["RELEASE_DATE"]) if a else None,
            description=(r["SUMMARY"] or (a["SUMMARY"] if a else "") or "").strip(),
            genres=genres.get(r["ID"], []),
            adult=(r["AGE_RATING"] or 0) >= ADULT_AGE,
            url=f"{base}/series/{r['ID']}" if base else "",
            cover="",                              # thumbnails need the API (add an API key)
            added=str(r["CREATED_DATE"] or "")[:10], library=r["LIB"] or "Komga",
            extra={"books": r["BOOK_COUNT"], "publisher": r["PUBLISHER"] or ""},
        ))
    con.close()
    return units
