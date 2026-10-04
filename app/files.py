"""Downloading the original file of something you can see ("Save to device").

Each source hands back either a local file (read-only mounts: Calibre, Storyteller) or an
upstream URL + headers that Omnarr streams through, so app credentials never reach the
browser. Shows/series are not downloadable as a whole (too big); single files only.
"""
import os
import re

from .connectors.base import ro_connect

EBOOK_ORDER = ("EPUB", "KEPUB", "AZW3", "MOBI", "PDF", "CBZ", "CBR", "FB2", "TXT")
DOWNLOADABLE = {"calibre", "storyteller", "abs", "komga", "jellyfin", "romm"}


class NotDownloadable(Exception):
    pass


def safe_name(name, fallback="download"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "").strip(" .")
    return name[:180] or fallback


def calibre(cfg, book_id, fmt=None):
    s = cfg.source("calibre") or {}
    con = ro_connect(s["db"])
    try:
        b = con.execute("SELECT path, title FROM books WHERE id=?", (int(book_id),)).fetchone()
        rows = {r["format"].upper(): r["name"] for r in con.execute("SELECT format, name FROM data WHERE book=?", (int(book_id),))}
    finally:
        con.close()
    if not b or not rows:
        raise NotDownloadable("No file for this book")
    want = (fmt or "").upper()
    pick = want if want in rows else next((f for f in EBOOK_ORDER if f in rows), next(iter(rows)))
    path = os.path.join(s.get("library", ""), b["path"], f"{rows[pick]}.{pick.lower()}")
    if not os.path.isfile(path):
        raise NotDownloadable("The file isn't reachable from Omnarr (check the Calibre library mount)")
    return {"path": path, "filename": safe_name(b["title"]) + "." + pick.lower(), "formats": sorted(rows)}


def storyteller(cfg, book_uuid):
    """The read-along EPUB (text + synced narration)."""
    s = cfg.source("storyteller") or {}
    con = ro_connect(s["db"])
    try:
        r = con.execute("""SELECT b.title, (SELECT filepath FROM audiobook a WHERE a.book_uuid=b.uuid) AS audio_path
                           FROM book b WHERE b.uuid=?""", (book_uuid,)).fetchone()
    finally:
        con.close()
    ap = (r["audio_path"] or "") if r else ""
    if not ap.startswith("/library/"):
        raise NotDownloadable("Read-along file not found")
    folder = os.path.join(s.get("library", ""), ap[len("/library/"):].split("/")[0])
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.endswith("(readaloud).epub"):
                return {"path": os.path.join(folder, f), "filename": safe_name(r["title"]) + " (read-along).epub"}
    raise NotDownloadable("Read-along file not found (check the Storyteller library mount)")


def abs_item(cfg, item_id, token):
    """One audio file -> that file; several -> ABS's own zip of the item."""
    from .connectors import abs as abs_c
    s = cfg.source("abs") or {}
    base = (s.get("url") or "").rstrip("/")
    with abs_c.client(s) as c:
        d = c.get(f"/api/items/{item_id}", params={"expanded": 1}).json()
    m = d.get("media") or {}
    title = safe_name((m.get("metadata") or {}).get("title") or "audiobook")
    files = [f for f in m.get("audioFiles") or [] if not f.get("exclude")]
    if len(files) == 1:
        ext = ((files[0].get("metadata") or {}).get("ext") or ".m4b").lstrip(".")
        return {"url": f"{base}/api/items/{item_id}/file/{files[0]['ino']}/download",
                "headers": {"Authorization": f"Bearer {token}"}, "filename": f"{title}.{ext}"}
    return {"url": f"{base}/api/items/{item_id}/download", "headers": {"Authorization": f"Bearer {token}"},
            "filename": f"{title}.zip"}


def komga_book(cfg, book_id):
    s = cfg.source("komga") or {}
    if not s.get("api_key"):
        raise NotDownloadable("Komga needs an API key for downloads")
    return {"url": f"{s['url'].rstrip('/')}/api/v1/books/{book_id}/file", "headers": {"X-API-Key": s["api_key"]},
            "filename": None}                     # Komga sends the real file name


def jellyfin_item(cfg, item_id, kind):
    if kind != "movie":
        raise NotDownloadable("Whole shows can't be downloaded; open an episode in Jellyfin instead")
    s = cfg.source("jellyfin") or {}
    return {"url": f"{s['url'].rstrip('/')}/Items/{item_id}/Download",
            "headers": {"Authorization": f'MediaBrowser Token="{s["api_key"]}"'}, "filename": None}


def romm_rom(cfg, rom_id, fs_name):
    s = cfg.source("romm") or {}
    if not fs_name:
        raise NotDownloadable("RomM didn't report a file name for this game")
    from urllib.parse import quote
    return {"url": f"{s['url'].rstrip('/')}/api/roms/{int(rom_id)}/content/{quote(fs_name)}",
            "headers": {"Authorization": f"Bearer {s['api_key']}"}, "filename": fs_name}
