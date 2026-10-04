"""ReadMeABook: audiobook requests (Audible search, download, import into Audiobookshelf/Plex).

Omnarr hands audiobook requests to it instead of Shelfmark when chosen in Connections. It uses
one token (Profile -> API Tokens in ReadMeABook, an admin's token so Omnarr's own approvals
are the only gate). Only the endpoints ReadMeABook allows for tokens are used:
GET /api/auth/me, GET /api/audiobooks/search, POST /api/requests, GET /api/requests/:id.
"""
import logging

import httpx

from .. import normalize

log = logging.getLogger("omnarr.readmeabook")
# ReadMeABook request states -> Omnarr's wanted-list states
ACTIVE = {"pending", "searching", "downloading", "processing"}
WAITING = {"awaiting_search", "awaiting_import", "awaiting_approval", "awaiting_release"}
DONE = {"available", "downloaded"}
FAILED = {"failed", "warn"}
CANCELLED = {"cancelled", "denied"}


def configured(s):
    return bool(s and s.get("url") and s.get("api_key"))


def handles_audiobooks(cfg):
    s = cfg.source("readmeabook")
    return configured(s) and str(s.get("audiobooks", "yes")).strip().lower() not in ("false", "no", "0", "off")


def _client(s, timeout=30):
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=timeout,
                        headers={"Authorization": f"Bearer {s['api_key']}", "Accept": "application/json"})


def test(s):
    try:
        with _client(s, 15) as c:
            r = c.get("/api/auth/me")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code in (401, 403):
        return False, "API token rejected"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    d = r.json() or {}
    user = (d.get("user") or d).get("username") or (d.get("user") or d).get("plexUsername") or "token owner"
    return True, f"Connected as {user}"


def search(s, title, author=""):
    """Audible results (via ReadMeABook) for a title + author, best match first."""
    found = []
    with _client(s) as c:
        for q in ([f"{title} {author}".strip(), title] if author else [title]):
            r = c.get("/api/audiobooks/search", params={"q": q, "page": 1})
            if r.status_code != 200:
                continue
            found = r.json().get("results") or []
            if found:
                break
    first = (author or "").split(",")[0]
    scored = []
    for b in found:
        lang = (b.get("language") or "english").lower()
        if lang and not lang.startswith("en"):
            continue
        score = normalize.same_book(title, first, b.get("title") or "", (b.get("author") or "").split(",")[0])
        scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    return [dict(b, match=round(sc, 3)) for sc, b in scored]


def best_match(s, title, author=""):
    hits = search(s, title, author)
    return hits[0] if hits and hits[0]["match"] >= normalize.THRESHOLD else None


def request(s, book):
    """Create a request for an Audible result. -> (request id or None, status, message)."""
    body = {"audiobook": {k: book.get(k) for k in ("asin", "title", "author", "narrator", "description", "coverArtUrl")
                          if book.get(k)}}
    with _client(s) as c:
        r = c.post("/api/requests", json=body)
    if r.status_code == 201:
        req = (r.json() or {}).get("request") or {}
        return str(req.get("id") or ""), req.get("status") or "pending", "sent to ReadMeABook"
    try:
        d = r.json() or {}
    except ValueError:
        d = {}
    err = d.get("error") or ""
    if r.status_code == 409:
        # already in their library, already being fetched, or already requested: fine for us
        rid = str((d.get("request") or {}).get("id") or d.get("requestId") or "")
        return rid or None, "available" if err == "AlreadyAvailable" else "pending", f"ReadMeABook: {err or 'already requested'}"
    raise RuntimeError(f"ReadMeABook refused the request ({r.status_code}): {d.get('message') or err or r.text[:200]}")


def status(s, request_id):
    """(state, progress 0-100, error message) for a request."""
    with _client(s) as c:
        r = c.get(f"/api/requests/{request_id}")
    if r.status_code == 404:
        return "cancelled", 0, "request no longer exists in ReadMeABook"
    r.raise_for_status()
    req = (r.json() or {}).get("request") or r.json() or {}
    return req.get("status") or "pending", req.get("progress") or 0, req.get("errorMessage") or ""
