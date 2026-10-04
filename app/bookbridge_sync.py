"""Reading-position sync with BookBridge over its KOSync API.

Omnarr's ebook reader acts as one more KOReader-style device for the server's owner: it reads
the position BookBridge has for a book (from the Kobo, Storyteller, Audiobookshelf...) and
reports where you stopped, and BookBridge passes that on to everything else.

- A book is identified by BookBridge's own document id (KOReader's partial MD5 of the EPUB),
  read from its database for the Calibre book it is linked to.
- A position is a fraction (0..1) plus a KOReader XPath such as
  "/body/DocFragment[3]/body/div/p[12].0". The reader converts to and from EPUB CFIs.
- Only admins use it: the KOSync login is the owner's, the same rule as Jellyfin/ABS.
"""
import hashlib
import logging
import threading
from datetime import datetime, timezone

import httpx

from .connectors.base import ro_connect

log = logging.getLogger("omnarr.bookbridge")
DEVICE, DEVICE_ID = "Omnarr", "omnarr-web-reader"


def configured(s):
    return bool(s and s.get("sync_url") and s.get("kosync_user") and s.get("kosync_key"))


def _headers(s):
    key = s["kosync_key"]
    return {"x-auth-user": s["kosync_user"], "x-auth-key": hashlib.md5(key.encode()).hexdigest(),
            "Accept": "application/vnd.koreader.v1+json"}


def check(s):
    try:
        r = httpx.get(s["sync_url"].rstrip("/") + "/users/auth", headers=_headers(s), timeout=10)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code == 200:
        return True, "ok"
    return False, "KOSync login rejected" if r.status_code in (401, 403) else f"HTTP {r.status_code}"


def document_for_calibre(s, calibre_id):
    """BookBridge's document id for a Calibre book, or None if it isn't linked."""
    try:
        con = ro_connect(s["db"])
        try:
            r = con.execute("""SELECT kosync_doc_id FROM books WHERE ebook_source='CWA' AND ebook_source_id=?
                               AND kosync_doc_id IS NOT NULL AND kosync_doc_id != '' LIMIT 1""", (str(calibre_id),)).fetchone()
        finally:
            con.close()
        return r[0] if r else None
    except Exception as e:
        log.warning("bookbridge lookup failed: %s", e)
        return None


def get_position(s, doc):
    """{fraction, xpath, updated (epoch s), device} or None."""
    try:
        r = httpx.get(f"{s['sync_url'].rstrip('/')}/syncs/progress/{doc}", headers=_headers(s), timeout=8)
    except Exception as e:
        log.warning("bookbridge get failed: %s", e)
        return None
    if r.status_code != 200:
        return None
    d = r.json() or {}
    if d.get("percentage") is None:
        return None
    ts = d.get("timestamp")
    try:
        ts = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        try:
            ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = None
    return {"fraction": float(d["percentage"]), "xpath": d.get("progress") or "", "updated": ts,
            "device": d.get("device") or ""}


def put_position(s, doc, fraction, xpath):
    """Report a position (in the background; never slows the reader down)."""
    body = {"document": doc, "percentage": round(float(fraction), 6), "progress": xpath or "",
            "device": DEVICE, "device_id": DEVICE_ID}

    def run():
        try:
            r = httpx.put(s["sync_url"].rstrip("/") + "/syncs/progress", json=body, headers=_headers(s), timeout=10)
            if r.status_code >= 400:
                log.warning("bookbridge put -> HTTP %s", r.status_code)
        except Exception as e:
            log.warning("bookbridge put failed: %s", e)
    threading.Thread(target=run, daemon=True, name="bookbridge-put").start()
