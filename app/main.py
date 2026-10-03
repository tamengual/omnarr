"""Omnarr web app: one search across every media app on the server.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8765
"""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_mod, indexer, live, normalize, play, requests_, search, wanted
from .connectors import abs as abs_c, arr, calibre, jellyfin, komga, registry, romm, stash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("omnarr")

cfg = config_mod.load()
INDEX = cfg.get("index.path", "/data/index.db")
STATE = cfg.get("index.state", "/data/state.db")
STATIC = os.path.join(os.path.dirname(__file__), "static")
SESSION_DAYS = 90
COOKIE = "omnarr_session"

app = FastAPI(title="Omnarr", docs_url=None, redoc_url=None)
_index_lock = threading.Lock()


# ── state DB (password hash, sessions, manual overrides) ─────────────────────
def state():
    con = indexer._state(STATE)
    con.row_factory = sqlite3.Row
    return con


def _setting(con, k):
    r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _hash(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def _session(request):
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    con = state()
    try:
        r = con.execute("SELECT * FROM sessions WHERE token=? AND expires>?", (tok, time.time())).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _adult_ok(sess):
    return _adult_enabled() and bool(sess and (sess.get("adult_until") or 0) > time.time())


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/") and path != "/api/health":
        if not _session(request):
            return JSONResponse({"error": "login required"}, status_code=401)
    return await call_next(request)


# ── auth ─────────────────────────────────────────────────────────────────────
@app.get("/api/auth/status")
def auth_status(request: Request):
    con = state()
    try:
        has_pw = _setting(con, "password_hash") is not None
    finally:
        con.close()
    sess = _session(request)
    return {"setup_needed": not has_pw, "logged_in": bool(sess), "adult_unlocked": _adult_ok(sess),
            "adult_enabled": _adult_enabled(),
            "connections_needed": bool(sess) and not cfg.connections()}


def _adult_enabled():
    """The private section exists only when switched on (Settings) — off by default."""
    con = state()
    try:
        v = _setting(con, "adult_enabled")
    finally:
        con.close()
    return (v == "1") if v is not None else bool(cfg.get("adult.enabled", False))


def _new_session(response):
    tok = secrets.token_urlsafe(32)
    con = state()
    with con:
        con.execute("INSERT INTO sessions VALUES (?,?,?,0)", (tok, time.time(), time.time() + SESSION_DAYS * 86400))
        con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    con.close()
    response.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")


@app.post("/api/auth/setup")
async def auth_setup(request: Request, response: Response):
    body = await request.json()
    pw = body.get("password") or ""
    if len(pw) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    con = state()
    try:
        if _setting(con, "password_hash") is not None:
            raise HTTPException(409, "Already set up")
        salt = secrets.token_hex(16)
        with con:
            con.execute("INSERT INTO settings VALUES ('password_salt', ?)", (salt,))
            con.execute("INSERT INTO settings VALUES ('password_hash', ?)", (_hash(pw, salt),))
    finally:
        con.close()
    _new_session(response)
    return {"ok": True}


_fails = {}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _fails.get(ip, []) if t > time.time() - 300]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many attempts; wait 5 minutes")
    body = await request.json()
    con = state()
    try:
        salt, want = _setting(con, "password_salt"), _setting(con, "password_hash")
    finally:
        con.close()
    if not want or not hmac.compare_digest(_hash(body.get("password") or "", salt), want):
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "Wrong password")
    _new_session(response)
    return {"ok": True}


# ── adult section: separate PIN, unlock lasts adult.unlock_minutes (default 30) ──
def _set_adult(request, until):
    con = state()
    with con:
        con.execute("UPDATE sessions SET adult_until=? WHERE token=?", (until, request.cookies.get(COOKIE)))
    con.close()


@app.post("/api/adult/setup")
async def adult_setup(request: Request):
    if not _adult_enabled():
        raise HTTPException(404, "Adult section is disabled")
    pin = str((await request.json()).get("pin") or "")
    if not pin.isdigit() or not 4 <= len(pin) <= 12:
        raise HTTPException(400, "PIN must be 4-12 digits")
    con = state()
    try:
        if _setting(con, "adult_pin_hash") is not None:
            raise HTTPException(409, "PIN already set")
        salt = secrets.token_hex(16)
        with con:
            con.execute("INSERT INTO settings VALUES ('adult_pin_salt', ?)", (salt,))
            con.execute("INSERT INTO settings VALUES ('adult_pin_hash', ?)", (_hash(pin, salt),))
    finally:
        con.close()
    _set_adult(request, time.time() + 60 * int(cfg.get("adult.unlock_minutes", 30)))
    return {"ok": True}


@app.get("/api/adult/status")
def adult_status(request: Request):
    con = state()
    try:
        has_pin = _setting(con, "adult_pin_hash") is not None
    finally:
        con.close()
    sess = _session(request)
    return {"enabled": _adult_enabled(), "pin_set": has_pin,
            "unlocked": _adult_ok(sess), "until": (sess or {}).get("adult_until") or 0}


@app.post("/api/adult/unlock")
async def adult_unlock(request: Request):
    if not _adult_enabled():
        raise HTTPException(404, "Adult section is disabled")
    ip = "pin:" + (request.client.host if request.client else "?")
    recent = [t for t in _fails.get(ip, []) if t > time.time() - 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts; wait 10 minutes")
    pin = str((await request.json()).get("pin") or "")
    con = state()
    try:
        salt, want = _setting(con, "adult_pin_salt"), _setting(con, "adult_pin_hash")
    finally:
        con.close()
    if not want or not hmac.compare_digest(_hash(pin, salt), want):
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "Wrong PIN")
    _set_adult(request, time.time() + 60 * int(cfg.get("adult.unlock_minutes", 30)))
    return {"ok": True}


@app.post("/api/adult/lock")
def adult_lock(request: Request):
    _set_adult(request, 0)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    tok = request.cookies.get(COOKIE)
    if tok:
        con = state()
        with con:
            con.execute("DELETE FROM sessions WHERE token=?", (tok,))
        con.close()
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ── search / browse ──────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": os.path.exists(INDEX)}


# ── setup wizard: connections to the other apps ─────────────────────────────
@app.get("/api/setup/apps")
def setup_apps():
    return {"apps": registry.public_specs(cfg.connections(fresh=True)),
            "options": {"adult_enabled": _adult_enabled()}}


def _settings_from(app_key, body):
    if app_key not in registry.BY_KEY:
        raise HTTPException(404, "Unknown app")
    old = {k: v for k, v in (cfg.connections(fresh=True).get(app_key) or {}).items() if k != "enabled"}
    return registry.merge_secret(app_key, body.get("values") or {}, old)


def _run_test(app_key, settings):
    spec = registry.BY_KEY[app_key]
    missing = [f["label"] for f in spec["fields"] if f.get("required") and not settings.get(f["key"])
               and not (app_key in ("abs", "komga", "stash") and f["key"] == "api_key" and settings.get("db"))]
    if missing:
        return False, "Missing: " + ", ".join(missing)
    try:
        return spec["test"](settings)
    except Exception as e:                       # connection refused, DNS, bad JSON...
        return False, f"Could not connect: {e.__class__.__name__}: {str(e)[:200]}"


@app.post("/api/setup/apps/{app_key}/test")
async def setup_test(app_key: str, request: Request):
    """Test the values in the form (falls back to the stored secret when masked)."""
    settings = _settings_from(app_key, await request.json())
    ok, msg = await asyncio.to_thread(_run_test, app_key, settings)
    return {"ok": ok, "message": msg}


@app.post("/api/setup/apps/{app_key}")
async def setup_save(app_key: str, request: Request):
    body = await request.json()
    settings = _settings_from(app_key, body)
    ok, msg = await asyncio.to_thread(_run_test, app_key, settings)
    if not ok and not body.get("force"):
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    cfg.save_connection(app_key, settings, body.get("enabled", True))
    live._audit(STATE, "setup.save", {"app": app_key}, "ok" if ok else "saved; test failed")
    if not _index_lock.locked():
        threading.Thread(target=_reindex, daemon=True).start()
    return {"ok": True, "message": msg}


@app.delete("/api/setup/apps/{app_key}")
def setup_delete(app_key: str):
    cfg.delete_connection(app_key)
    live._audit(STATE, "setup.remove", {"app": app_key}, "ok")
    return {"ok": True}


@app.post("/api/setup/options")
async def setup_options(request: Request):
    body = await request.json()
    con = state()
    with con:
        if "adult_enabled" in body:
            con.execute("INSERT OR REPLACE INTO settings VALUES ('adult_enabled', ?)", ("1" if body["adult_enabled"] else "0",))
    con.close()
    return {"ok": True, "options": {"adult_enabled": _adult_enabled()}}


@app.get("/api/search")
def api_search(request: Request):
    if not os.path.exists(INDEX):
        raise HTTPException(503, "Index is still being built")
    return search.search(INDEX, dict(request.query_params), _adult_ok(_session(request)))


@app.get("/api/work/{wid}")
def api_work(wid: str, request: Request):
    w = search.work(INDEX, wid, _adult_ok(_session(request)))
    if not w:
        raise HTTPException(404, "Not found")
    return w


@app.get("/api/status")
def api_status():
    return {"index": search.stats(INDEX) if os.path.exists(INDEX) else None,
            "refresh_minutes": cfg.get("index.refresh_minutes", 15), "indexing": _index_lock.locked()}


@app.post("/api/reindex")
def api_reindex():
    if _index_lock.locked():
        return {"started": False, "reason": "already running"}
    threading.Thread(target=_reindex, daemon=True).start()
    return {"started": True}


@app.post("/api/override")
async def api_override(request: Request):
    """Manual fix-ups: {"a": unit_key, "b": unit_key, "action": "merge"} or {"a": unit_key, "action": "split"}."""
    body = await request.json()
    action = body.get("action")
    if action not in ("merge", "split"):
        raise HTTPException(400, "action must be merge or split")
    con = state()
    with con:
        con.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?)",
                    (body.get("a"), body.get("b") or "", action, time.time()))
    con.close()
    threading.Thread(target=_reindex, daemon=True).start()
    return {"ok": True}


# ── requests ("Get it") ──────────────────────────────────────────────────────
BOOK_FORMATS = ("ebook", "audiobook")


def _jellyfin_by_tmdb():
    """tmdb id -> work id for screen items already in the library."""
    con = search.connect(INDEX)
    try:
        return {r[0]: r[1] for r in con.execute(
            "SELECT json_extract(extra,'$.ids.tmdb'), work_id FROM editions WHERE source='jellyfin' "
            "AND json_extract(extra,'$.ids.tmdb') IS NOT NULL")}
    finally:
        con.close()


@app.get("/api/work/{wid}/requests")
def api_work_requests(wid: str, request: Request):
    """What can be requested from this item: missing book formats + screen/game adaptations."""
    w = search.work(INDEX, wid, _adult_ok(_session(request)))
    if not w:
        raise HTTPException(404, "Not found")
    out = {"missing_formats": [], "adaptations": [], "shelfmark_enabled": requests_.shelfmark_enabled(cfg),
           "seerr_enabled": bool(cfg.source("seerr")), "romarr_enabled": requests_.romarr_enabled(cfg)}
    if w["kind"] != "book":
        return out
    out["missing_formats"] = [f for f in BOOK_FORMATS if f not in w["formats"]]
    con = state()
    found, seen = [], set()
    try:
        surname = normalize.surname((w["authors"] or [""])[0])
        for t in normalize.lookup_titles(w["title"]):
            for a in requests_.adaptations(con, t, surname):
                if a["wikidata"] not in seen:
                    seen.add(a["wikidata"])
                    found.append(a)
            if found:
                break                    # the most specific title that Wikidata knows wins
    finally:
        con.close()
    owned = _jellyfin_by_tmdb()
    for a in found:
        item = dict(a, status="unknown", in_library=None, poster="", url="")
        if a["kind"] in ("movie", "tv") and a.get("tmdb"):
            if str(a["tmdb"]) in owned:
                item["status"], item["in_library"] = "available", owned[str(a["tmdb"])]
            elif cfg.source("seerr"):
                try:
                    d = requests_.seerr_details(cfg, a["kind"], a["tmdb"])
                    if d:
                        item.update(status=d["status"], poster=d["poster"], url=d["url"],
                                    label=d["title"] or a["label"], year=d["year"] or a["year"])
                except Exception as e:
                    log.debug("seerr details failed: %s", e)
        elif a["kind"] == "game":
            item.update(status="not_requested" if requests_.romarr_enabled(cfg) else "no_requester",
                        url=f"https://www.igdb.com/games/{a['igdb']}" if a.get("igdb") else "")
        out["adaptations"].append(item)
    return out


@app.post("/api/request/screen")
async def api_request_screen(request: Request):
    body = await request.json()
    if body.get("kind") not in ("movie", "tv") or not str(body.get("tmdb", "")).isdigit():
        raise HTTPException(400, "kind must be movie|tv and tmdb a number")
    try:
        return requests_.seerr_request(cfg, body["kind"], body["tmdb"])
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ── live detail, actions, activity ───────────────────────────────────────────
@app.get("/api/work/{wid}/live")
def api_work_live(wid: str, request: Request):
    """Fresh detail from the owning apps: episodes/seasons (TV), file + queue (movies),
    per-app reading positions + chapters (books)."""
    w = search.work(INDEX, wid, _adult_ok(_session(request)))
    if not w:
        raise HTTPException(404, "Not found")
    try:
        if w["kind"] == "show":
            return {"kind": "show", **live.show_detail(cfg, w["editions"])}
        if w["kind"] == "movie":
            return {"kind": "movie", **live.movie_detail(cfg, w["editions"])}
        if w["kind"] == "book":
            return {"kind": "book", **live.book_detail(cfg, w["editions"])}
    except Exception as e:
        log.warning("live detail %s failed: %s", wid, e, exc_info=True)
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return {"kind": w["kind"]}


@app.post("/api/action")
async def api_action(request: Request):
    """Search / monitor actions on Sonarr & Radarr. Body: {action, ...ids}. Audited in state.db."""
    body = await request.json()
    name = body.pop("action", "")
    try:
        return live.run_action(cfg, STATE, name, body)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ── in-app playback ──────────────────────────────────────────────────────────
@app.get("/api/play/video/{item_id}")
def api_play_video(item_id: str):
    """Jellyfin movie/episode id -> {url (direct or HLS through our proxy), resume, subtitles...}."""
    if not cfg.source("jellyfin"):
        raise HTTPException(404, "Jellyfin not configured")
    try:
        return play.video_info(cfg, item_id, live._jf_user(cfg))
    except Exception as e:
        raise HTTPException(502, f"Jellyfin: {type(e).__name__}: {e}")


@app.get("/api/play/audio/{item_id}")
def api_play_audio(item_id: str):
    """Audiobookshelf library item id -> {tracks (through our proxy), chapters, resume}."""
    info = play.audio_info(cfg, item_id) if cfg.source("abs") else None
    if not info:
        raise HTTPException(404, "Not found")
    return info


@app.post("/api/play/progress")
async def api_play_progress(request: Request):
    """{source: jellyfin|abs, item_id, position (s), duration (s), finished} -> saved in the owning app."""
    b = await request.json()
    try:
        pos, dur = float(b.get("position") or 0), float(b.get("duration") or 0)
        if b.get("source") == "jellyfin":
            ok = play.video_progress(cfg, live._jf_user(cfg), b["item_id"], pos, dur, bool(b.get("finished")))
        elif b.get("source") == "abs":
            ok = play.audio_progress(cfg, b["item_id"], pos, dur, bool(b.get("finished")))
        else:
            raise HTTPException(400, "source must be jellyfin or abs")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return {"ok": ok}


@app.post("/api/play/stop")
async def api_play_stop(request: Request):
    b = await request.json()
    if b.get("play_session_id") and cfg.source("jellyfin"):
        try:
            play.video_stop(cfg, b["play_session_id"])
        except Exception as e:
            log.debug("stop transcode failed: %s", e)
    return {"ok": True}


PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


async def _proxy(url, request, headers):
    from starlette.background import BackgroundTask
    from fastapi.responses import StreamingResponse
    import httpx as _httpx
    h = dict(headers)
    if request.headers.get("range"):
        h["Range"] = request.headers["range"]
    client = _httpx.AsyncClient(timeout=_httpx.Timeout(30, read=None))
    r = await client.send(client.build_request("GET", url, headers=h), stream=True)
    out = {k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS}
    ctype = r.headers.get("content-type", "")
    if "mpegurl" in ctype.lower() or url.split("?")[0].lower().endswith(".m3u8"):
        body = await r.aread()
        await r.aclose(); await client.aclose()
        return Response(play.rewrite_playlist(body.decode("utf-8", "replace")), status_code=r.status_code,
                        media_type=ctype or "application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    async def close():
        await r.aclose(); await client.aclose()
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out, background=BackgroundTask(close))


@app.get("/api/stream/jf/{path:path}")
async def api_stream_jf(path: str, request: Request):
    if not cfg.source("jellyfin"):
        raise HTTPException(404, "Jellyfin not configured")
    q = play._strip_key(str(request.url.query))
    url = f"{play._jf_base(cfg)}/{path}" + (f"?{q}" if q else "")
    return await _proxy(url, request, play._jf_headers(cfg))


@app.get("/api/stream/abs/{item_id}/{ino}")
async def api_stream_abs(item_id: str, ino: str, request: Request):
    if not cfg.source("abs"):
        raise HTTPException(404, "Audiobookshelf not configured")
    tok = play.abs_token(cfg)
    return await _proxy(f"{play.abs_base(cfg)}/api/items/{item_id}/file/{ino}", request, {"Authorization": f"Bearer {tok}"})


@app.get("/api/activity")
def api_activity():
    out = live.activity(cfg)
    out["wanted_books"] = [w for w in wanted.list_all(STATE) if w["status"] != "done"]
    return out


@app.get("/api/request/game/platforms")
def api_game_platforms():
    try:
        return {"enabled": requests_.romarr_enabled(cfg), "platforms": requests_.game_platforms(cfg)}
    except Exception as e:
        raise HTTPException(502, f"ROMarr: {e}")


@app.post("/api/request/game")
async def api_request_game(request: Request):
    body = await request.json()
    game, platform = (body.get("game") or "").strip(), (body.get("platform") or "").strip()
    if not game or not platform:
        raise HTTPException(400, "game and platform are required")
    try:
        return requests_.game_request(cfg, game, platform)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/api/request/search")
def api_request_search(q: str):
    """Search Seerr for movies/TV not in the library."""
    return {"results": requests_.seerr_search(cfg, q) if q.strip() else []}


@app.get("/api/request/book/candidates")
def api_book_candidates(work: str, format: str, request: Request):
    if format not in BOOK_FORMATS:
        raise HTTPException(400, "format must be ebook or audiobook")
    w = search.work(INDEX, work, _adult_ok(_session(request)))
    if not w:
        raise HTTPException(404, "Not found")
    query = " ".join(x for x in (w["title"], (w["authors"] or [""])[0]) if x)
    try:
        cands = requests_.book_candidates(cfg, query, format)
    except Exception as e:
        raise HTTPException(502, f"Shelfmark: {e}")
    # Metadata search ranks by popularity; put the book that IS this one first
    # (same author surname + title match), and drop study guides / summaries.
    author = (w["authors"] or [""])[0]
    junk = ("summary", "study guide", "sparknotes", "cliffsnotes", "analysis of")
    def score(c):
        s = normalize.same_book(w["title"], author, c["title"] or "", (c["authors"] or [""])[0])
        return s - (1 if any(j in (c["title"] or "").lower() for j in junk) else 0)
    cands.sort(key=score, reverse=True)
    return {"query": query, "candidates": cands[:8]}


@app.get("/api/request/book/releases")
def api_book_releases(provider: str, book_id: str, format: str):
    if format not in BOOK_FORMATS:
        raise HTTPException(400, "format must be ebook or audiobook")
    try:
        return {"releases": requests_.book_releases(cfg, provider, book_id, format)}
    except Exception as e:
        raise HTTPException(502, f"Shelfmark: {e}")


@app.post("/api/request/book/download")
async def api_book_download(request: Request):
    body = await request.json()
    rel = body.get("release")
    if not isinstance(rel, dict) or "source" not in rel or "source_id" not in rel:
        raise HTTPException(400, "release with source and source_id required")
    try:
        out = requests_.book_download(cfg, rel)
    except Exception as e:
        raise HTTPException(502, f"Shelfmark: {e}")
    # Track it: if this copy fails, the wanted-list job finds another one.
    w = search.work(INDEX, body.get("work") or "", True) if body.get("work") else None
    fmt = rel.get("content_type") or body.get("format")
    if fmt in wanted.FORMATS:
        wanted.add(STATE, (w or {}).get("title") or rel.get("title") or "", ((w or {}).get("authors") or [""])[0],
                   fmt, work_id=(w or {}).get("id"), provider=body.get("provider"), book_id=body.get("book_id"),
                   current=rel.get("source_id"), current_title=rel.get("title"))
    return out


# ── wanted books (keep looking until it arrives) ─────────────────────────────
@app.get("/api/wanted")
def api_wanted_list():
    return {"items": wanted.list_all(STATE), "retry_days": wanted.RETRY_DAYS}


@app.post("/api/wanted")
async def api_wanted_add(request: Request):
    """{work, format} -> keep looking for that format of this book, picking a copy automatically."""
    body = await request.json()
    fmt = body.get("format")
    w = search.work(INDEX, body.get("work") or "", _adult_ok(_session(request)))
    if not w or fmt not in wanted.FORMATS:
        raise HTTPException(400, "work and format (ebook|audiobook) required")
    wid = wanted.add(STATE, w["title"], (w["authors"] or [""])[0], fmt, work_id=w["id"],
                     provider=body.get("provider"), book_id=body.get("book_id"))
    threading.Thread(target=_wanted_tick, daemon=True).start()       # first search right away
    return {"ok": True, "id": wid}


@app.post("/api/wanted/{wid}/search")
def api_wanted_search(wid: int):
    wanted.search_now(STATE, wid)
    threading.Thread(target=_wanted_tick, daemon=True).start()
    return {"ok": True}


@app.delete("/api/wanted/{wid}")
def api_wanted_delete(wid: int):
    wanted.remove(STATE, wid)
    return {"ok": True}


# ── covers (served by us so the browser never needs app credentials) ────────
THUMBS = os.path.join(os.path.dirname(INDEX) or ".", "thumbs")
THUMB_WIDTH = 480
THUMB_MAX_AGE = 7 * 86400          # re-fetch a cached cover weekly (covers do change)


def _cover_original(source, sid, key):
    """Original cover bytes from the owning app, or None."""
    if source == "stash":
        if stash.api_mode(cfg.source("stash")):
            return stash.cover_bytes(cfg, sid)
        p = stash.cover_file(cfg, sid)
    elif source == "komga":
        return komga.cover_bytes(cfg, sid)
    elif source == "calibre":
        p = calibre.cover_file(cfg, sid)
    elif source == "abs":
        if abs_c.api_mode(cfg.source("abs")):
            return abs_c.cover_bytes(cfg, sid)
        p = abs_c.cover_file(cfg, sid)
    elif source == "jellyfin":
        return jellyfin.image(cfg, sid, width=THUMB_WIDTH)[0]
    elif source in ("sonarr", "radarr"):
        return arr.poster(cfg, source, sid)
    elif source == "romm":
        con = search.connect(INDEX)
        try:
            r = con.execute("SELECT json_extract(extra,'$.cover_path') FROM editions WHERE unit_key=?", (key,)).fetchone()
        finally:
            con.close()
        return romm.image(cfg, r[0])[0] if r and r[0] else None
    else:
        return None
    if p and os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def _thumbnail(data):
    """Shrink to THUMB_WIDTH as JPEG (RomM/ABS originals run 1-2 MB, 2400 px)."""
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(data))
    im = im.convert("RGB")
    if im.width > THUMB_WIDTH:
        im = im.resize((THUMB_WIDTH, round(im.height * THUMB_WIDTH / im.width)), Image.LANCZOS)
    out = BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue()


@app.get("/api/cover/{key}")
def api_cover(key: str, request: Request):
    source, _, sid = key.partition(":")
    adult = source == "stash"
    if adult and not _adult_ok(_session(request)):
        raise HTTPException(404, "No cover")          # gate BEFORE the cache, so cached adult covers stay gated
    headers = {"Cache-Control": "private, max-age=3600" if adult else "public, max-age=86400"}
    os.makedirs(THUMBS, exist_ok=True)
    path = os.path.join(THUMBS, hashlib.sha1(key.encode()).hexdigest() + ".jpg")
    try:
        if os.path.exists(path) and os.path.getmtime(path) > time.time() - THUMB_MAX_AGE:
            return FileResponse(path, media_type="image/jpeg", headers=headers)
        data = _cover_original(source, sid, key)
        if data:
            try:
                thumb = _thumbnail(data)
            except Exception as e:                    # odd format: serve the original as-is
                log.debug("thumbnail %s failed: %s", key, e)
                return Response(data, media_type="image/jpeg", headers=headers)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(thumb)
            os.replace(tmp, path)
            return Response(thumb, media_type="image/jpeg", headers=headers)
    except Exception as e:
        log.debug("cover %s failed: %s", key, e)
    raise HTTPException(404, "No cover")


# ── background indexing ──────────────────────────────────────────────────────
def _reindex():
    if not _index_lock.acquire(blocking=False):
        return
    try:
        indexer.run(cfg)
    except Exception as e:
        log.error("index build failed: %s", e, exc_info=True)
    finally:
        _index_lock.release()


def _scheduler():
    every = max(1, int(cfg.get("index.refresh_minutes", 15))) * 60
    while True:
        _reindex()
        time.sleep(every)


_wanted_lock = threading.Lock()


def _wanted_tick():
    if not _wanted_lock.acquire(blocking=False):
        return
    try:
        wanted.tick(cfg, STATE, INDEX)
    except Exception as e:
        log.error("wanted tick failed: %s", e, exc_info=True)
    finally:
        _wanted_lock.release()


def _wanted_loop():
    time.sleep(120)                     # let the first index build finish
    last_harder = 0.0
    while True:
        _wanted_tick()                  # cheap when nothing is due: a few DB reads
        if time.time() - last_harder > 24 * 3600:     # search harder: once a day, ≤8 titles, each ≤ weekly
            last_harder = time.time()
            try:
                from . import harder
                harder.auto(cfg, STATE, lambda a, p, r: live._audit(STATE, a, p, r))
            except Exception as e:
                log.error("search harder (auto) failed: %s", e, exc_info=True)
        time.sleep(30 * 60)


@app.on_event("startup")
def _startup():
    os.makedirs(os.path.dirname(INDEX) or ".", exist_ok=True)
    threading.Thread(target=_scheduler, name="indexer", daemon=True).start()
    threading.Thread(target=_wanted_loop, name="wanted", daemon=True).start()


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
