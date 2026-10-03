"""Storyteller (storyteller.db, read-only). Read-alongs = EPUBs with synced narration.

Each Storyteller book was staged by the book-pipeline from one audiobook and one
Calibre ebook: the staged audio is a HARDLINK of the Audiobookshelf file (same
inode) and the staged epub keeps Calibre's filename. We read both straight off
the staged folder, so matching to ABS and Calibre is exact, never fuzzy.
"""
import os

from .base import Unit, ro_connect, year_of

AUDIO_EXT = (".m4b", ".m4a", ".mp3", ".mp4", ".flac", ".ogg", ".opus", ".aac")


def read(cfg):
    s = cfg.source("storyteller")
    if not s:
        return []
    con = ro_connect(s["db"])
    lib = s.get("library", "")
    base = cfg.link("storyteller")

    creators, series, asin = {}, {}, {}
    for r in con.execute("""SELECT bc.book_uuid, bc.role, c.name FROM book_to_creator bc
                            JOIN creator c ON c.uuid=bc.creator_uuid ORDER BY bc.created_at"""):
        creators.setdefault(r["book_uuid"], {}).setdefault(r["role"], []).append(r["name"])
    for r in con.execute("""SELECT bs.book_uuid, s.name, bs.position FROM book_to_series bs
                            JOIN series s ON s.uuid=bs.series_uuid"""):
        series.setdefault(r["book_uuid"], (r["name"], r["position"]))
    for r in con.execute("""SELECT i.book_uuid, i.value FROM identifier i
                            JOIN identifier_type t ON t.uuid=i.identifier_type_uuid WHERE t.kind='audible'"""):
        asin[r["book_uuid"]] = r["value"]

    units = []
    for r in con.execute("""SELECT b.uuid, b.title, b.publication_date, b.description, b.created_at, b.duration,
                                   r.status, r.updated_at AS aligned_at,
                                   (SELECT filepath FROM audiobook a WHERE a.book_uuid=b.uuid) AS audio_path
                            FROM book b JOIN readaloud r ON r.book_uuid=b.uuid"""):
        if (r["status"] or "").upper() != "ALIGNED":
            continue
        folder = ""
        inodes, epubs = [], []
        ap = r["audio_path"] or ""
        if ap.startswith("/library/"):
            folder = os.path.join(lib, ap[len("/library/"):].split("/")[0])
        if folder and os.path.isdir(folder):
            for f in os.listdir(folder):
                fl = f.lower()
                if fl.endswith(AUDIO_EXT):
                    inodes.append(str(os.stat(os.path.join(folder, f)).st_ino))
                elif fl.endswith(".epub") and not f.endswith("(readaloud).epub") and not fl.endswith("_epub2.epub"):
                    epubs.append(f)
        roles = creators.get(r["uuid"], {})
        sname, pos = series.get(r["uuid"], ("", None))
        u = Unit(
            source="storyteller", source_id=r["uuid"], kind="book", format="readalong",
            title=r["title"] or "", authors=roles.get("aut", []), narrators=roles.get("nrt", []),
            series=sname or "", series_index=pos,
            year=year_of(r["publication_date"]), description=(r["description"] or "").strip(),
            url=f"{base}/books/{r['uuid']}" if base else "",
            duration=r["duration"], added=str(r["aligned_at"] or r["created_at"] or "")[:10],
            library="Storyteller",
            ids={"audible": asin[r["uuid"]]} if r["uuid"] in asin else {},
            match={"inodes": inodes, "epub_names": epubs},
        )
        units.append(u)
    con.close()
    return units
