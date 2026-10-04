"""Calibre library (metadata.db, read-only). Ebooks; opened in Calibre-Web."""
import os
import re

from .base import Unit, ro_connect, year_of

EBOOK_FORMATS = ("EPUB", "KEPUB", "AZW3", "MOBI", "PDF", "AZW", "KFX")


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


ADULT_TAGS = ("NSFW", "XXX", "18+")     # behind the private-section PIN


def read(cfg):
    s = cfg.source("calibre")
    if not s:
        return []
    con = ro_connect(s["db"])
    hide = {t.lower() for t in (s.get("hide_tags") or [])}
    adult_tags = s.get("adult_tags")              # unset: the usual NSFW tags; [] turns this off
    adult = {t.lower() for t in (ADULT_TAGS if adult_tags is None else adult_tags)}
    read_col = s.get("date_read_column")          # e.g. 1 -> custom_column_1 (Goodreads "Date Read")
    base = cfg.link("cwa")                        # Calibre-Web(-Automated) book page: /book/<id>
    tmpl = "{base}/book/{id}"

    authors, tags, ids, fmts, series, comments, done = {}, {}, {}, {}, {}, {}, set()
    for r in con.execute("""SELECT l.book, a.name FROM books_authors_link l JOIN authors a ON a.id=l.author
                            ORDER BY l.book, l.id"""):
        authors.setdefault(r["book"], []).append(r["name"].replace("|", ","))
    for r in con.execute("SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag"):
        tags.setdefault(r["book"], []).append(r["name"])
    for r in con.execute("SELECT book, type, val FROM identifiers"):
        ids.setdefault(r["book"], {})[r["type"]] = r["val"]
    for r in con.execute("SELECT book, format, name, uncompressed_size FROM data"):
        fmts.setdefault(r["book"], []).append((r["format"], r["name"], r["uncompressed_size"]))
    for r in con.execute("SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id=l.series"):
        series[r["book"]] = r["name"]
    for r in con.execute("SELECT book, text FROM comments"):
        comments[r["book"]] = _strip_html(r["text"])
    if read_col:
        try:
            done = {r[0] for r in con.execute(f"SELECT book FROM custom_column_{int(read_col)}")}
        except Exception:
            done = set()
    stars = {}                                    # the owner's own rating (Calibre: 0-10, i.e. half-stars)
    try:
        for r in con.execute("SELECT l.book, r.rating FROM books_ratings_link l JOIN ratings r ON r.id=l.rating"):
            if r["rating"]:
                stars[r["book"]] = r["rating"]
    except Exception:
        pass

    units = []
    for b in con.execute("SELECT id, title, timestamp, pubdate, series_index, path, has_cover FROM books"):
        bid = b["id"]
        formats = fmts.get(bid, [])
        if not any(f[0] in EBOOK_FORMATS for f in formats):
            continue
        btags = tags.get(bid, [])
        u = Unit(
            source="calibre", source_id=str(bid), kind="book", format="ebook",
            title=b["title"], authors=authors.get(bid, []),
            series=series.get(bid, ""), series_index=b["series_index"] if bid in series else None,
            year=year_of(b["pubdate"]), description=comments.get(bid, ""),
            tags=[t for t in btags if t.lower() not in hide],
            url=tmpl.format(base=base, id=bid) if base else "",
            cover=f"calibre:{bid}" if b["has_cover"] else "",
            added=(b["timestamp"] or "")[:10],
            finished=bid in done,
            hidden=any(t.lower() in hide for t in btags),
            adult=any(t.lower() in adult for t in btags),
            library="Calibre",
            ids={k: v for k, v in ids.get(bid, {}).items() if k in ("isbn", "asin", "mobi-asin", "google", "goodreads")},
            match={"epub_names": [f"{n}.epub" for f, n, _ in formats if f == "EPUB"]},
            extra={"formats": sorted({f[0] for f in formats}), "path": b["path"],
                   **({"my_rating": stars[bid]} if bid in stars else {})},
        )
        units.append(u)
    con.close()
    return units


def cover_file(cfg, book_id):
    """Path of a Calibre book's cover.jpg (looked up fresh; folders move on edits)."""
    s = cfg.source("calibre") or {}
    con = ro_connect(s["db"])
    try:
        r = con.execute("SELECT path FROM books WHERE id=?", (int(book_id),)).fetchone()
    finally:
        con.close()
    return os.path.join(s.get("library", ""), r["path"], "cover.jpg") if r else None
