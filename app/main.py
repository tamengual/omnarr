"""Omnarr web app: one search across every media app on the server.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8765
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import accounts, bookbridge_sync, config as config_mod, identity, indexer, live, normalize, notify, play, playstate, requests_, search, wanted
from .connectors import abs as abs_c, arr, calibre, jellyfin, komga, registry, romm, stash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("omnarr")

cfg = config_mod.load()
INDEX = cfg.get("index.path", "/data/index.db")
STATE = cfg.get("index.state", "/data/state.db")
STATIC = os.path.join(os.path.dirname(__file__), "static")
SESSION_DAYS = 90
COOKIE = "omnarr_session"
# Served under a path prefix? Reverse proxies: set OMNARR_BASE_PATH (e.g. /omnarr) and strip it
# before forwarding. Home Assistant add-on: OMNARR_HA_INGRESS=1 — the prefix comes from the
# X-Ingress-Path header, and HA's own login is trusted, but ONLY for requests from HA's ingress proxy.
BASE_PATH = os.environ.get("OMNARR_BASE_PATH", "").rstrip("/")
HA_INGRESS = os.environ.get("OMNARR_HA_INGRESS") == "1"
NO_PW_HA = "No password is set for direct sign-in yet. Open Omnarr from Home Assistant and set one in Settings → Password."
INGRESS_PROXY = os.environ.get("OMNARR_INGRESS_PROXY", "172.30.32.2")

@asynccontextmanager
async def _lifespan(_app):
    _startup()                      # defined at the bottom: starts the indexer + wanted loops
    yield


app = FastAPI(title="Omnarr", docs_url=None, redoc_url=None, lifespan=_lifespan)
_index_lock = threading.Lock()


# ── state DB, accounts and sessions ──────────────────────────────────────────
_accounts_ready = False


def state():
    global _accounts_ready
    con = indexer._state(STATE)
    con.row_factory = sqlite3.Row
    if not _accounts_ready:
        cfg.connections(fresh=True)           # makes sure the connections table exists
        accounts.ensure(con)                  # one-time import of the old single password
        _accounts_ready = True
    return con


_hash = accounts.hash_secret


def _setting(con, k):
    r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _ingress_path(request):
    """HA ingress prefix when the request really came through HA's ingress proxy, else None."""
    if HA_INGRESS and request.client and request.client.host == INGRESS_PROXY:
        return request.headers.get("x-ingress-path") or None
    return None


def _base_path(request):
    return (_ingress_path(request) or BASE_PATH).rstrip("/")


def _session(request):
    """The current session joined with its account ({..., "account": {...}}), or None."""
    con = state()
    try:
        if _ingress_path(request):
            # HA has already authenticated this person: one account per HA user.
            uid = request.headers.get("x-remote-user-id") or "user"
            name = request.headers.get("x-remote-user-display-name") or request.headers.get("x-remote-user-name") or "ha-user"
            acct = accounts.for_ha_user(con, uid, name)
            tok = "ha:" + uid
            r = con.execute("SELECT * FROM sessions WHERE token=?", (tok,)).fetchone()
            if not r or r["account_id"] != acct["id"]:
                with con:
                    con.execute("INSERT OR REPLACE INTO sessions (token, created, expires, adult_until, account_id) "
                                "VALUES (?,?,?,?,?)", (tok, time.time(), 4102444800, (r["adult_until"] if r else 0) or 0, acct["id"]))
                r = con.execute("SELECT * FROM sessions WHERE token=?", (tok,)).fetchone()
        else:
            tok = request.cookies.get(COOKIE)
            if not tok or tok.startswith("ha:"):
                return None
            r = con.execute("SELECT * FROM sessions WHERE token=? AND expires>?", (tok, time.time())).fetchone()
            acct = accounts.get(con, r["account_id"]) if r else None
            if not acct:
                return None
        return {**dict(r), "account": dict(acct)}
    finally:
        con.close()


def _account(request):
    sess = _session(request)
    return sess["account"] if sess else None


def _adult_ok(sess):
    return (_adult_enabled() and bool(sess) and bool(sess["account"].get("adult_allowed"))
            and (sess.get("adult_until") or 0) > time.time())


ADMIN_PATHS = ("/api/setup/", "/api/reindex", "/api/override", "/api/accounts")
# Anything that makes the server fetch or change something needs the "can request" switch.
REQUEST_PATHS = ("/api/request/", "/api/action", "/api/wanted")
DOWNLOAD_PATHS = ("/api/download/",)
UPLOAD_PATHS = ("/api/upload",)
QUEUEABLE = ("/api/request/screen", "/api/request/game", "/api/request/book/download")


def _queueable(path, method):
    return method == "POST" and (path in QUEUEABLE or path == "/api/wanted")


@app.middleware("http")
async def revalidate_ui(request: Request, call_next):
    """The UI (html/js/css) must be re-checked on every load, or browsers keep running an old
    version after an upgrade. Unchanged files still come back as a cheap 304."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/") and path != "/api/health":
        sess = _session(request)
        if not sess:
            return JSONResponse({"error": "login required"}, status_code=401)
        if path.startswith(ADMIN_PATHS) and sess["account"]["role"] != "admin":
            return JSONResponse({"error": "Only an admin can do that", "detail": "Only an admin can do that"}, status_code=403)
        if (path.startswith(REQUEST_PATHS) and request.method != "GET"
                and not accounts.allowed(sess["account"], "can_request")
                and not (accounts.allowed(sess["account"], "can_ask") and _queueable(path, request.method))):
            msg = "Your account can browse and play, but not request downloads"
            return JSONResponse({"error": msg, "detail": msg}, status_code=403)
        if path.startswith(DOWNLOAD_PATHS) and not accounts.allowed(sess["account"], "can_download"):
            msg = "Your account can't save files to your device"
            return JSONResponse({"error": msg, "detail": msg}, status_code=403)
        if path.startswith(UPLOAD_PATHS) and request.method != "GET" and not accounts.allowed(sess["account"], "can_upload"):
            msg = "Your account can't upload files"
            return JSONResponse({"error": msg, "detail": msg}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def act_as(request: Request, call_next):
    """Jellyfin/ABS calls in this request use the signed-in person's own identity."""
    identity.set_for(_account(request) if request.url.path.startswith("/api/") else None)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Basic hardening for installs reachable from the internet (e.g. Tailscale Funnel).
    Registered last, so it wraps everything, including the login check's 401/403 replies."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


# ── auth ─────────────────────────────────────────────────────────────────────
@app.get("/api/auth/status")
def auth_status(request: Request):
    con = state()
    try:
        n_accounts = accounts.count(con)
        any_password = con.execute("SELECT 1 FROM accounts WHERE hash IS NOT NULL").fetchone() is not None
    finally:
        con.close()
    sess = _session(request)
    acct = sess["account"] if sess else None
    is_admin = bool(acct and acct["role"] == "admin")
    # In the HA add-on, a direct visitor must never get to create the first account.
    return {"setup_needed": n_accounts == 0 and not HA_INGRESS,
            "has_password": bool(acct and acct["hash"]) if acct else any_password,
            "logged_in": bool(sess), "user": accounts.public(acct) if acct else None,
            "role": acct["role"] if acct else None,
            "adult_unlocked": _adult_ok(sess), "adult_enabled": _adult_enabled(),
            "adult_allowed": bool(acct and acct["adult_allowed"]),
            "permissions": {k: accounts.allowed(acct, k) for k in accounts.PERMISSIONS} if acct else {},
            "connections_needed": is_admin and not cfg.connections(),
            "ha_ingress": bool(_ingress_path(request))}


def _adult_enabled():
    """The private section exists only when switched on (Settings) — off by default."""
    con = state()
    try:
        v = _setting(con, "adult_enabled")
    finally:
        con.close()
    return (v == "1") if v is not None else bool(cfg.get("adult.enabled", False))


def _new_session(request, response, account_id):
    tok = secrets.token_urlsafe(32)
    con = state()
    with con:
        con.execute("INSERT INTO sessions (token, created, expires, adult_until, account_id) VALUES (?,?,?,0,?)",
                    (tok, time.time(), time.time() + SESSION_DAYS * 86400, account_id))
        con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    con.close()
    response.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        path=_base_path(request) + "/", secure=request.url.scheme == "https")


@app.post("/api/auth/setup")
async def auth_setup(request: Request, response: Response):
    """First run: create the admin account."""
    if HA_INGRESS:
        raise HTTPException(403, NO_PW_HA)
    body = await request.json()
    con = state()
    try:
        if accounts.count(con):
            raise HTTPException(409, "Already set up")
        try:
            aid = accounts.create(con, body.get("username") or "admin", body.get("password") or "", "admin", adult_allowed=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        con.close()
    _new_session(request, response, aid)
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
        username = (body.get("username") or "").strip()
        if username:
            row = accounts.by_username(con, username)
        else:                                  # older clients: the only account with a password
            rows = con.execute("SELECT * FROM accounts WHERE hash IS NOT NULL").fetchall()
            row = rows[0] if len(rows) == 1 else None
        any_password = con.execute("SELECT 1 FROM accounts WHERE hash IS NOT NULL").fetchone() is not None
    finally:
        con.close()
    if not any_password and HA_INGRESS:
        raise HTTPException(403, NO_PW_HA)
    if not accounts.verify_password(row, body.get("password")):
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "Wrong username or password")
    _new_session(request, response, row["id"])
    return {"ok": True}


@app.post("/api/auth/password")
async def auth_password(request: Request):
    """Set or change your own password. The current password is required too, except when
    coming through Home Assistant (already authenticated by HA) or setting it the first time."""
    sess = _session(request)
    if not sess:
        raise HTTPException(403, "Sign in first")
    acct = sess["account"]
    body = await request.json()
    con = state()
    try:
        row = accounts.get(con, acct["id"])
        if row["hash"] and not _ingress_path(request) and not accounts.verify_password(row, body.get("current")):
            raise HTTPException(403, "Current password is wrong")
        try:
            accounts.set_password(con, acct["id"], body.get("new") or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        with con:                              # sign out this person's other browsers
            con.execute("DELETE FROM sessions WHERE account_id=? AND token NOT LIKE 'ha:%' AND token <> ?",
                        (acct["id"], sess["token"]))
    finally:
        con.close()
    return {"ok": True}


# ── accounts (admin) and your own app identities ─────────────────────────────
@app.get("/api/accounts")
def api_accounts_list():
    con = state()
    try:
        return {"accounts": accounts.list_all(con)}
    finally:
        con.close()


@app.post("/api/accounts")
async def api_accounts_create(request: Request):
    body = await request.json()
    con = state()
    try:
        aid = accounts.create(con, body.get("username"), body.get("password") or "", body.get("role") or "member",
                              adult_allowed=bool(body.get("adult_allowed")),
                              can_request=bool(body.get("can_request", True)),
                              can_download=bool(body.get("can_download", False)),
                              can_upload=bool(body.get("can_upload", False)),
                              can_ask=bool(body.get("can_ask", False)))
        acct = accounts.public(accounts.get(con, aid))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()
    live._audit(STATE, "account.create", {"username": acct["username"], "role": acct["role"], "by": _account(request)["username"]}, "ok")
    return {"ok": True, "account": acct}


@app.patch("/api/accounts/{account_id}")
async def api_accounts_update(account_id: int, request: Request):
    body = await request.json()
    me = _account(request)
    con = state()
    try:
        row = accounts.get(con, account_id)
        if not row:
            raise HTTPException(404, "No such account")
        demoting = body.get("role") == "member" and row["role"] == "admin"
        if demoting and accounts.count(con, "admin") <= 1:
            raise HTTPException(400, "Omnarr needs at least one admin")
        try:
            accounts.update(con, account_id, **{k: body[k] for k in ("role", "username", *accounts.PERMISSIONS) if k in body})
            if body.get("password"):
                accounts.set_password(con, account_id, body["password"])
                with con:
                    con.execute("DELETE FROM sessions WHERE account_id=? AND token NOT LIKE 'ha:%'", (account_id,))
            if body.get("clear_pin"):
                with con:
                    con.execute("UPDATE accounts SET pin_salt=NULL, pin_hash=NULL WHERE id=?", (account_id,))
        except ValueError as e:
            raise HTTPException(400, str(e))
        acct = accounts.public(accounts.get(con, account_id))
    finally:
        con.close()
    changed = sorted(k for k in ("role", "username", "password", "clear_pin", *accounts.PERMISSIONS) if k in body)
    live._audit(STATE, "account.update", {"username": acct["username"], "changed": changed, "by": me["username"]}, "ok")
    return {"ok": True, "account": acct}


@app.delete("/api/accounts/{account_id}")
def api_accounts_delete(account_id: int, request: Request):
    me = _account(request)
    if account_id == me["id"]:
        raise HTTPException(400, "You can't delete your own account")
    con = state()
    try:
        row = accounts.get(con, account_id)
        if not row:
            raise HTTPException(404, "No such account")
        if row["role"] == "admin" and accounts.count(con, "admin") <= 1:
            raise HTTPException(400, "Omnarr needs at least one admin")
        accounts.delete(con, account_id)
    finally:
        con.close()
    live._audit(STATE, "account.delete", {"username": row["username"], "by": me["username"]}, "ok")
    return {"ok": True}


# ── invitations: an admin makes a sign-up link (copy it, or have Omnarr email it) ──
@app.get("/api/accounts/invites")
def api_invites_list():
    con = state()
    try:
        return {"invites": accounts.list_invites(con), "email_enabled": bool(cfg.source("email"))}
    finally:
        con.close()


@app.post("/api/accounts/invites")
async def api_invites_create(request: Request):
    """{role, can_request, can_download, can_upload, adult_allowed, days, email?, note?, link_base}
    -> {url}. link_base is the address the inviter is using (the invitee must be able to reach it)."""
    body = await request.json()
    me = _account(request)
    con = state()
    try:
        public = (_setting(con, "public_url") or "").strip()
    finally:
        con.close()
    link_base = public or (body.get("link_base") or "").split("#")[0]   # a set public address wins
    if not link_base.startswith(("http://", "https://")):
        raise HTTPException(400, "link_base (the Omnarr address to put in the link) is required")
    con = state()
    try:
        token = accounts.create_invite(con, me["id"], body, body.get("days") or 7, body.get("email"), body.get("note"))
    finally:
        con.close()
    url = f"{link_base}#invite={token}"
    emailed, message = False, ""
    to = (body.get("email") or "").strip()
    if to:
        if not cfg.source("email"):
            message = "Email isn't set up (Settings → Connections → Email), so copy the link instead."
        else:
            from . import mailer
            try:
                await asyncio.to_thread(mailer.send, cfg.source("email"), to, f"{me['username']} invited you to Omnarr",
                                        f"{me['username']} has invited you to their media library on Omnarr.\n\n"
                                        f"Choose a username and password here (the link works once and expires in "
                                        f"{int(body.get('days') or 7)} days):\n\n{url}\n")
                emailed, message = True, f"Invitation emailed to {to}."
            except Exception as e:
                message = f"Couldn't send the email ({type(e).__name__}: {str(e)[:150]}). Copy the link instead."
    live._audit(STATE, "invite.create", {"by": me["username"], "email": to or None,
                                          "role": body.get("role") or "member"}, "emailed" if emailed else "link")
    return {"ok": True, "url": url, "emailed": emailed, "message": message}


@app.delete("/api/accounts/invites/{invite_id}")
def api_invites_revoke(invite_id: int):
    con = state()
    try:
        accounts.revoke_invite(con, invite_id)
    finally:
        con.close()
    return {"ok": True}


@app.get("/api/auth/invite/{token}")
def api_invite_check(token: str, request: Request):
    _invite_rate(request)
    con = state()
    try:
        row = accounts.find_invite(con, token)
        inviter = accounts.get(con, row["created_by"]) if row else None
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "This invitation link has expired or was already used")
    return {"ok": True, "invited_by": inviter["username"] if inviter else None, "expires": row["expires"],
            "email": row["email"]}


@app.post("/api/auth/invite/{token}")
async def api_invite_accept(token: str, request: Request, response: Response):
    _invite_rate(request)
    body = await request.json()
    con = state()
    try:
        aid = accounts.accept_invite(con, token, body.get("username"), body.get("password") or "")
        acct = accounts.get(con, aid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()
    live._audit(STATE, "invite.accept", {"username": acct["username"]}, "ok")
    notify.send(cfg, STATE, "account_joined", "New Omnarr account", f"{acct['username']} joined through an invitation.")
    _new_session(request, response, aid)
    return {"ok": True}


def _invite_rate(request):
    ip = "invite:" + (request.client.host if request.client else "?")
    recent = [t for t in _fails.get(ip, []) if t > time.time() - 300]
    if len(recent) >= 20:
        raise HTTPException(429, "Too many attempts; wait 5 minutes")
    _fails[ip] = recent + [time.time()]


@app.get("/api/me")
def api_me(request: Request):
    acct = _account(request)
    out = accounts.public(acct)
    out["uses_server_identity"] = acct["role"] == "admin" and not (acct["jellyfin_user"] and acct["abs_api_key"])
    return out


@app.post("/api/me")
async def api_me_update(request: Request):
    """Link your own Jellyfin user and Audiobookshelf API key (so playback and progress are
    yours). The ABS key is checked before it's saved; a masked key means "keep the stored one"."""
    acct = _account(request)
    body = await request.json()
    fields, message = {}, []
    if "jellyfin_user" in body:
        fields["jellyfin_user"] = body["jellyfin_user"]
        name = (body["jellyfin_user"] or "").strip()
        if name and cfg.source("jellyfin"):
            ok, msg = await asyncio.to_thread(registry.BY_KEY["jellyfin"]["test"], {**cfg.source("jellyfin"), "user": name})
            if not ok:
                raise HTTPException(400, msg)
            message.append(f"Jellyfin user {name} linked")
    key = body.get("abs_api_key")
    if key is not None and not str(key).startswith("••••"):
        fields["abs_api_key"] = key
        if str(key).strip() and cfg.source("abs"):
            abs_s = cfg.source("abs")
            ok, msg = await asyncio.to_thread(registry.BY_KEY["abs"]["test"], {"url": abs_s.get("url"), "api_key": str(key).strip()})
            if not ok:
                raise HTTPException(400, msg)
            message.append(msg)
    for k in ("email", "notify_email"):
        if k in body:
            fields[k] = body[k]
    con = state()
    try:
        accounts.update(con, acct["id"], **fields)
        out = accounts.public(accounts.get(con, acct["id"]))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()
    live.invalidate("jf:")
    return {"ok": True, "account": out, "message": "; ".join(message) or "Saved"}


# ── private (adult) section: a PIN per person; unlock lasts adult.unlock_minutes (default 30) ──
def _set_adult(request, until):
    con = state()
    with con:
        con.execute("UPDATE sessions SET adult_until=? WHERE token=?", (until, (_session(request) or {}).get("token")))
    con.close()


def _adult_gate(request):
    if not _adult_enabled():
        raise HTTPException(404, "Adult section is disabled")
    acct = _account(request)
    if not acct:
        raise HTTPException(401, "Sign in first")
    if not acct["adult_allowed"]:
        raise HTTPException(403, "The private section isn't enabled for your account")
    return acct


@app.post("/api/adult/setup")
async def adult_setup(request: Request):
    acct = _adult_gate(request)
    pin = (await request.json()).get("pin")
    con = state()
    try:
        if accounts.get(con, acct["id"])["pin_hash"]:
            raise HTTPException(409, "PIN already set")
        try:
            accounts.set_pin(con, acct["id"], pin)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        con.close()
    _set_adult(request, time.time() + 60 * int(cfg.get("adult.unlock_minutes", 30)))
    return {"ok": True}


@app.get("/api/adult/status")
def adult_status(request: Request):
    sess = _session(request)
    acct = sess["account"] if sess else None
    return {"enabled": _adult_enabled(), "allowed": bool(acct and acct["adult_allowed"]),
            "pin_set": bool(acct and acct["pin_hash"]),
            "unlocked": _adult_ok(sess), "until": (sess or {}).get("adult_until") or 0}


@app.post("/api/adult/unlock")
async def adult_unlock(request: Request):
    acct = _adult_gate(request)
    key = f"pin:{acct['id']}"
    recent = [t for t in _fails.get(key, []) if t > time.time() - 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts; wait 10 minutes")
    pin = str((await request.json()).get("pin") or "")
    con = state()
    try:
        row = accounts.get(con, acct["id"])
    finally:
        con.close()
    if not accounts.verify_pin(row, pin):
        _fails[key] = recent + [time.time()]
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
    response.delete_cookie(COOKIE, path=_base_path(request) + "/")
    return {"ok": True}


# ── search / browse ──────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": os.path.exists(INDEX)}


# ── setup wizard: connections to the other apps ─────────────────────────────
@app.get("/api/setup/apps")
def setup_apps():
    return {"apps": registry.public_specs(cfg.connections(fresh=True)),
            "options": {"adult_enabled": _adult_enabled(), **_upload_settings()}}


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
    pub = (body.get("public_url") or "").strip()
    if pub and not pub.startswith(("https://", "http://")):
        raise HTTPException(400, "The public address must start with https:// (or http://)")
    for key in ("upload_dir", "upload_audio_dir"):      # validate before saving anything
        path = (body.get(key) or "").strip()
        if path and not (os.path.isdir(path) and os.access(path, os.W_OK)):
            raise HTTPException(400, f"{path} isn't a writable folder inside Omnarr's container (mount it read-write)")
    con = state()
    with con:
        if "adult_enabled" in body:
            con.execute("INSERT OR REPLACE INTO settings VALUES ('adult_enabled', ?)", ("1" if body["adult_enabled"] else "0",))
        for key in ("upload_dir", "upload_audio_dir"):
            if key in body:
                con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, (body[key] or "").strip()))
        if "upload_max_mb" in body:
            con.execute("INSERT OR REPLACE INTO settings VALUES ('upload_max_mb', ?)", (str(max(1, int(body["upload_max_mb"]))),))
        if "public_url" in body:
            con.execute("INSERT OR REPLACE INTO settings VALUES ('public_url', ?)", (pub.rstrip("/") + "/" if pub else "",))
    con.close()
    return {"ok": True, "options": {"adult_enabled": _adult_enabled(), **_upload_settings()}}


@app.get("/api/search")
def api_search(request: Request):
    if not os.path.exists(INDEX):
        raise HTTPException(503, "Index is still being built")
    sess = _session(request)
    return search.search(INDEX, dict(request.query_params), _adult_ok(sess), account_id=sess["account"]["id"])


@app.get("/api/work/{wid}")
def api_work(wid: str, request: Request):
    w = search.work(INDEX, wid, _adult_ok(_session(request)), account_id=(_account(request) or {}).get('id'))
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


def _library_index():
    """Lookup tables for matching outside works to the library:
    (kind, tmdb) -> work id for screens, and (kind, title key) -> [(work id, author surnames)]."""
    con = search.connect(INDEX)
    try:
        by_tmdb, by_title = {}, {}
        for wid_, kind, tmdb in con.execute(
                "SELECT e.work_id, w.kind, json_extract(e.extra,'$.ids.tmdb') FROM editions e JOIN works w ON w.id=e.work_id "
                "WHERE w.kind IN ('show','movie') AND json_extract(e.extra,'$.ids.tmdb') IS NOT NULL"):
            by_tmdb[("tv" if kind == "show" else "movie", str(tmdb))] = wid_
        for wid_, kind, title, authors in con.execute(
                "SELECT id, kind, title, authors FROM works WHERE kind IN ('book','comic','game','show','movie') AND hidden=0"):
            k = {"show": "tv"}.get(kind, kind)
            surnames = {normalize.surname(a) for a in json.loads(authors or "[]")}
            for v in normalize.variants(title):
                by_title.setdefault((k, v), []).append((wid_, surnames))
        return by_tmdb, by_title
    finally:
        con.close()


def _match_library(item, by_tmdb, by_title):
    if item["kind"] in ("movie", "tv") and item.get("tmdb"):
        # the id is authoritative: "The Last Airbender" (2010 film) must not match the
        # animated show just because the titles look alike
        return by_tmdb.get((item["kind"], str(item["tmdb"])))
    want = {normalize.surname(a) for a in item.get("authors") or []} - {""}
    for v in normalize.variants(item["label"]):
        for wid_, surnames in by_title.get((item["kind"], v), []):
            if not want or not surnames - {""} or want & surnames:
                return wid_
    return None


def _related_items(w):
    """Wikidata lookup for a work: by external ids (shows/movies) or title + author (books)."""
    con = state()
    try:
        if w["kind"] in ("show", "movie"):
            ids = {}
            for e in w["editions"]:
                for k, v in ((e.get("extra") or {}).get("ids") or {}).items():
                    ids.setdefault(k, v)
            clause = requests_.seed_from_ids(w["kind"], ids)
            if not clause:
                return []
            key = f"{w['kind']}:" + ",".join(f"{k}={ids[k]}" for k in sorted(ids) if k in ("tmdb", "tvdb", "imdb"))
            return requests_.related(con, clause, key)
        if w["kind"] in ("book", "comic"):
            surname = normalize.surname((w["authors"] or [""])[0])
            for t in normalize.lookup_titles(w["title"]):
                found = requests_.related(con, requests_.seed_from_book(t, surname), f"book:{t.lower()}|{surname}")
                if found:
                    return found             # the most specific title that Wikidata knows wins
        return []
    finally:
        con.close()


@app.get("/api/work/{wid}/requests")
def api_work_requests(wid: str, request: Request):
    """What can be requested from this item: missing book formats, plus everything related to it
    (same franchise/series, adaptations, what it was based on), matched to the library."""
    w = search.work(INDEX, wid, _adult_ok(_session(request)), account_id=(_account(request) or {}).get('id'))
    if not w:
        raise HTTPException(404, "Not found")
    out = {"missing_formats": [], "adaptations": [], "shelfmark_enabled": requests_.shelfmark_enabled(cfg),
           "seerr_enabled": bool(cfg.source("seerr")), "romarr_enabled": requests_.romarr_enabled(cfg)}
    if w.get("adult"):
        return out                           # private items are never looked up outside
    if w["kind"] == "book":
        out["missing_formats"] = [f for f in BOOK_FORMATS if f not in w["formats"]]
    found = _related_items(w)
    if not found:
        return out
    by_tmdb, by_title = _library_index()
    shown = {wid} | {x["id"] for k in ("series_works", "universe_works") for x in w.get(k) or []}
    seerr_lookups = 0
    for a in found:
        item = dict(a, status="unknown", in_library=None, poster="")
        owned = _match_library(a, by_tmdb, by_title)
        if owned in shown:
            continue                         # already on this page ("More in…")
        if owned:
            item["status"], item["in_library"] = "available", owned
        elif a["kind"] in ("movie", "tv") and a.get("tmdb") and cfg.source("seerr") and seerr_lookups < 16:
            seerr_lookups += 1
            try:
                d = requests_.seerr_details(cfg, a["kind"], a["tmdb"])
                if d:
                    item.update(status=d["status"], poster=d["poster"], url=d["url"] or a["url"],
                                label=d["title"] or a["label"], year=d["year"] or a["year"])
            except Exception as e:
                log.debug("seerr details failed: %s", e)
        elif a["kind"] == "game":
            item.update(status="not_requested" if requests_.romarr_enabled(cfg) else "no_requester",
                        url=f"https://www.igdb.com/games/{a['igdb']}" if a.get("igdb") else a["url"])
        elif a["kind"] in ("book", "comic"):
            item["status"] = "not_requested" if requests_.shelfmark_enabled(cfg) else "no_requester"
        out["adaptations"].append(item)
    return out


def _do_screen(body, account_id=None):
    try:
        return requests_.seerr_request(cfg, body["kind"], body["tmdb"])
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/request/screen")
async def api_request_screen(request: Request):
    body = await request.json()
    if body.get("kind") not in ("movie", "tv") or not str(body.get("tmdb", "")).isdigit():
        raise HTTPException(400, "kind must be movie|tv and tmdb a number")
    if _needs_approval(request):
        title = (body.get("title") or "").strip()
        if not title:
            try:
                title = (requests_.seerr_details(cfg, body["kind"], body["tmdb"]) or {}).get("title") or ""
            except Exception:
                pass
        return _queue(request, "screen", title or f"{body['kind']} {body['tmdb']}", body)
    return _do_screen(body)


# ── live detail, actions, activity ───────────────────────────────────────────
@app.get("/api/work/{wid}/live")
def api_work_live(wid: str, request: Request):
    """Fresh detail from the owning apps: episodes/seasons (TV), file + queue (movies),
    per-app reading positions + chapters (books)."""
    w = search.work(INDEX, wid, _adult_ok(_session(request)), account_id=(_account(request) or {}).get('id'))
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


_episode_series = {}                                   # Jellyfin episode id -> series id (for progress)


# ── in-app playback ──────────────────────────────────────────────────────────
@app.get("/api/play/video/{item_id}")
def api_play_video(item_id: str):
    """Jellyfin movie/episode id -> {url (direct or HLS through our proxy), resume, subtitles...}."""
    if not cfg.source("jellyfin"):
        raise HTTPException(404, "Jellyfin not configured")
    try:
        uid = live._jf_user(cfg)                       # this person's Jellyfin user, or None
        info = play.video_info(cfg, item_id, uid or live._jf_user(cfg, owner=True))
    except Exception as e:
        raise HTTPException(502, f"Jellyfin: {type(e).__name__}: {e}")
    if not uid:                                        # never someone else's position: use your own
        info["resume"], _ = playstate.get(STATE, identity.account_id.get(), "jellyfin", item_id)
    if info.get("series_id"):
        _episode_series[item_id] = info["series_id"]
    info["progress_sync"] = True                       # always saved in Omnarr
    info["app_sync"] = bool(uid)                       # …and in Jellyfin when you have an identity there
    return info


@app.get("/api/play/audio/{item_id}")
def api_play_audio(item_id: str):
    """Audiobookshelf library item id -> {tracks (through our proxy), chapters, resume}."""
    info = play.audio_info(cfg, item_id) if cfg.source("abs") else None
    if not info:
        raise HTTPException(404, "Not found")
    if not play.abs_token(cfg):
        info["resume"], _ = playstate.get(STATE, identity.account_id.get(), "abs", item_id)
    info["progress_sync"] = True
    info["app_sync"] = bool(play.abs_token(cfg))
    return info


@app.post("/api/play/progress")
async def api_play_progress(request: Request):
    """{source: jellyfin|abs, item_id, position (s), duration (s), finished} -> saved in the owning app."""
    b = await request.json()
    try:
        pos, dur = float(b.get("position") or 0), float(b.get("duration") or 0)
        if b.get("source") not in ("jellyfin", "abs"):
            raise HTTPException(400, "source must be jellyfin or abs")
        # always kept in Omnarr (so a single Omnarr account is enough)…
        playstate.record(STATE, identity.account_id.get(), b["source"], b["item_id"], pos, dur,
                         bool(b.get("finished")), parent_id=_episode_series.get(b["item_id"]))
        ok = True
        # …and written to the app too when this person has an identity there
        if b["source"] == "jellyfin":
            uid = live._jf_user(cfg)
            if uid:
                ok = play.video_progress(cfg, uid, b["item_id"], pos, dur, bool(b.get("finished")))
        elif play.abs_token(cfg):
            ok = play.audio_progress(cfg, b["item_id"], pos, dur, bool(b.get("finished")))
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


PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag",
                "content-disposition")


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
        return Response(play.rewrite_playlist(body.decode("utf-8", "replace"), _base_path(request)), status_code=r.status_code,
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
    tok = play.abs_token(cfg, owner=True)              # reading files uses the server's key
    return await _proxy(f"{play.abs_base(cfg)}/api/items/{item_id}/file/{ino}", request, {"Authorization": f"Bearer {tok}"})


# ── save to device (can_download) ────────────────────────────────────────────
@app.get("/api/download/{unit_key:path}")
async def api_download(unit_key: str, request: Request, format: str = ""):
    """The original file of one edition (an ebook, read-along, audiobook, comic, movie or game)."""
    from . import files
    from urllib.parse import quote
    sess = _session(request)
    con = search.connect(INDEX)
    try:
        e = con.execute("""SELECT e.source, e.source_id, e.title, e.extra, w.kind, w.adult FROM editions e
                           JOIN works w ON w.id=e.work_id WHERE e.unit_key=?""", (unit_key,)).fetchone()
    finally:
        con.close()
    if not e or (e["adult"] and not _adult_ok(sess)):
        raise HTTPException(404, "Not found")
    if e["source"] not in files.DOWNLOADABLE or not cfg.source(e["source"]):
        raise HTTPException(400, "This item can't be downloaded")
    extra = json.loads(e["extra"] or "{}")
    try:
        if e["source"] == "calibre":
            f = files.calibre(cfg, e["source_id"], format)
        elif e["source"] == "storyteller":
            f = files.storyteller(cfg, e["source_id"])
        elif e["source"] == "abs":
            f = await asyncio.to_thread(files.abs_item, cfg, e["source_id"], play.abs_token(cfg, owner=True))
        elif e["source"] == "komga":
            f = files.komga_book(cfg, e["source_id"])
        elif e["source"] == "jellyfin":
            f = files.jellyfin_item(cfg, e["source_id"], e["kind"])
        else:
            f = files.romm_rom(cfg, e["source_id"], extra.get("fs_name"))
    except files.NotDownloadable as ex:
        raise HTTPException(400, str(ex))
    live._audit(STATE, "download", {"by": sess["account"]["username"], "title": e["title"], "source": e["source"]}, "ok")
    if "path" in f:
        return FileResponse(f["path"], filename=f["filename"])
    resp = await _proxy(f["url"], request, f["headers"])
    if f.get("filename"):
        resp.headers["content-disposition"] = f"attachment; filename*=UTF-8''{quote(f['filename'])}"
    return resp


# ── in-app reading: comics (Komga pages) and ebooks (EPUB) ──────────────────
# Reading is like playing: anyone signed in can read what they can see. Comic pages stream one
# at a time; an ebook reader needs the whole EPUB. Positions are kept in Omnarr for everyone and
# also written to Komga for admins (Komga's API key is the owner's, so never for members).

def _reader_edition(unit_key, request):
    sess = _session(request)
    con = search.connect(INDEX)
    try:
        e = con.execute("""SELECT e.source, e.source_id, e.title, e.extra, w.title AS work_title, w.adult
                           FROM editions e JOIN works w ON w.id=e.work_id WHERE e.unit_key=?""", (unit_key,)).fetchone()
    finally:
        con.close()
    if not e or (e["adult"] and not _adult_ok(sess)) or e["source"] not in ("komga", "calibre"):
        raise HTTPException(404, "Not found")
    if not cfg.source(e["source"]):
        raise HTTPException(404, f"{e['source'].title()} is not connected")
    return e, sess


def _owner_identity(sess):
    return bool(sess) and sess["account"].get("role") == "admin"


def _iso_ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t or 0))


def _bridge_doc(sess, e):
    """BookBridge's document id when this person's ebook place should sync through it
    (admins only: the KOSync login is the owner's), else None."""
    s = cfg.source("bookbridge")
    if e["source"] != "calibre" or not _owner_identity(sess) or not bookbridge_sync.configured(s):
        return None
    return bookbridge_sync.document_for_calibre(s, e["source_id"])


def _place_updated(aid, source, item_id):
    con = playstate._con(STATE)
    try:
        r = con.execute("SELECT updated FROM play_progress WHERE account_id=? AND source=? AND item_id=?",
                        (aid, source, str(item_id))).fetchone()
        return r["updated"] or 0 if r else 0
    finally:
        con.close()


@app.get("/api/read/info/{unit_key:path}")
def api_read_info(unit_key: str, request: Request):
    """How to read one edition: {mode: pages|epub, title, pages | file_url, resume, next/previous}."""
    from .connectors import komga
    e, sess = _reader_edition(unit_key, request)
    aid = identity.account_id.get()
    mine = playstate.get_place(STATE, aid, e["source"], e["source_id"])
    out = {"unit_key": unit_key, "title": e["title"] or e["work_title"], "source": e["source"],
           "progress_sync": True, "app_sync": False}
    if e["source"] == "calibre":
        fmts = set(json.loads(e["extra"] or "{}").get("formats") or [])
        if not fmts & {"EPUB", "KEPUB"}:
            raise HTTPException(400, "This book has no EPUB to read in the browser")
        resume = {"locator": (mine or {}).get("locator"), "xpath": None, "fraction": (mine or {}).get("position") or 0,
                  "finished": bool(mine and mine["finished"]), "from": "omnarr"}
        bb_doc = _bridge_doc(sess, e)
        if bb_doc:
            out["app_sync"] = True
            theirs = bookbridge_sync.get_position(cfg.source("bookbridge"), bb_doc)
            own_at = _place_updated(aid, "calibre", e["source_id"])
            if theirs and theirs["fraction"] > 0 and (not mine or (theirs["updated"] or 0) > own_at + 5):
                # BookBridge heard about a newer place (the Kobo, an audiobook...): start there
                resume.update(locator=None, xpath=theirs["xpath"] or None, fraction=theirs["fraction"],
                              finished=theirs["fraction"] >= 0.995, **{"from": theirs["device"] or "BookBridge"})
        out.update(mode="epub", file_url=f"api/read/file/{unit_key}", resume=resume)
        return out
    s = cfg.source("komga")
    if not komga.api_mode(s):
        raise HTTPException(400, "Reading comics needs a Komga API key (Settings → Connections)")
    try:
        with komga.client(s, timeout=30) as c:
            book = c.get(f"/api/v1/books/{e['source_id']}").json()
            pages = c.get(f"/api/v1/books/{e['source_id']}/pages").json() if (book.get("media") or {}).get("pagesCount") else []
            nxt = c.get(f"/api/v1/books/{e['source_id']}/next")
            prv = c.get(f"/api/v1/books/{e['source_id']}/previous")
    except Exception as ex:
        raise HTTPException(502, f"Komga: {type(ex).__name__}: {ex}")
    media = book.get("media") or {}
    out["series"] = book.get("seriesTitle") or ""
    out["next"] = f"komga:{nxt.json()['id']}" if nxt.status_code == 200 else None
    out["previous"] = f"komga:{prv.json()['id']}" if prv.status_code == 200 else None
    if not pages and media.get("mediaProfile") == "EPUB":         # a text EPUB kept in Komga
        out.update(mode="epub", file_url=f"api/read/file/{unit_key}",
                   resume={"locator": (mine or {}).get("locator"), "fraction": (mine or {}).get("position") or 0,
                           "finished": bool(mine and mine["finished"])})
        return out
    if not pages:
        raise HTTPException(400, "Komga hasn't analysed this book yet (no pages)")
    page, finished = (int(mine["position"] or 0), mine["finished"]) if mine else (0, False)
    when = _iso_ts(_place_updated(aid, "komga", e["source_id"])) if mine else ""
    rp = book.get("readProgress") or {}
    if _owner_identity(sess) and rp and str(rp.get("lastModified") or "")[:19] >= when:
        page, finished = int(rp.get("page") or 0), bool(rp.get("completed"))   # Komga's own reader was later
    out.update(mode="pages", app_sync=_owner_identity(sess),
               pages=[{"n": p["number"], "w": p.get("width"), "h": p.get("height")} for p in pages],
               page_url=f"api/read/page/{e['source_id']}/",
               resume={"page": max(1, min(page or 1, len(pages))), "finished": finished})
    return out


@app.get("/api/read/page/{book_id}/{n}")
async def api_read_page(book_id: str, n: int, request: Request):
    """One comic page image from Komga (1-based)."""
    _reader_edition(f"komga:{book_id}", request)
    s = cfg.source("komga") or {}
    if not s.get("api_key"):
        raise HTTPException(400, "Reading comics needs a Komga API key")
    resp = await _proxy(f"{s['url'].rstrip('/')}/api/v1/books/{book_id}/pages/{int(n)}", request,
                        {"X-API-Key": s["api_key"]})
    if resp.status_code == 200:
        resp.headers["cache-control"] = "private, max-age=86400"
    return resp


@app.get("/api/read/file/{unit_key:path}")
async def api_read_file(unit_key: str, request: Request):
    """The EPUB behind the ebook reader (Calibre: EPUB, else KEPUB; Komga: the book file)."""
    from . import files
    e, _ = _reader_edition(unit_key, request)
    if e["source"] == "calibre":
        fmts = set(json.loads(e["extra"] or "{}").get("formats") or [])
        try:
            f = files.calibre(cfg, e["source_id"], "EPUB" if "EPUB" in fmts else "KEPUB")
        except files.NotDownloadable as ex:
            raise HTTPException(400, str(ex))
        return FileResponse(f["path"], media_type="application/epub+zip",
                            headers={"Cache-Control": "private, max-age=3600"})
    f = files.komga_book(cfg, e["source_id"])
    resp = await _proxy(f["url"], request, f["headers"])
    if "content-disposition" in resp.headers:             # read inline, don't save
        del resp.headers["content-disposition"]
    return resp


@app.post("/api/read/progress")
async def api_read_progress(request: Request):
    """{unit_key, page, pages} for comics or {unit_key, fraction, locator} for ebooks; finished optional."""
    from .connectors import komga
    b = await request.json()
    e, sess = _reader_edition(str(b.get("unit_key") or ""), request)
    finished = bool(b.get("finished"))
    if b.get("page") is not None:
        page, pages = max(1, int(b["page"])), max(1, int(b.get("pages") or 1))
        playstate.record(STATE, identity.account_id.get(), e["source"], e["source_id"], page, pages, finished)
        finished = finished or page >= pages
        if e["source"] == "komga" and _owner_identity(sess):
            def patch():
                with komga.client(cfg.source("komga"), timeout=15) as c:
                    c.patch(f"/api/v1/books/{e['source_id']}/read-progress", json={"page": page, "completed": finished})
            try:
                await asyncio.to_thread(patch)
            except Exception as ex:
                log.warning("komga read-progress failed: %s", ex)
                return {"ok": True, "app_sync": False}
        return {"ok": True, "app_sync": e["source"] == "komga" and _owner_identity(sess)}
    fraction = min(1.0, max(0.0, float(b.get("fraction") or 0)))
    locator = str(b.get("locator") or "")[:2000] or None
    playstate.record(STATE, identity.account_id.get(), e["source"], e["source_id"], fraction, 1.0,
                     finished or fraction >= 0.995, locator=locator)
    bb_doc = await asyncio.to_thread(_bridge_doc, sess, e)
    if bb_doc and fraction > 0:
        xpath = str(b.get("xpath") or "")[:1000]
        bookbridge_sync.put_position(cfg.source("bookbridge"), bb_doc, 1.0 if finished else fraction,
                                     xpath if xpath.startswith("/body/DocFragment[") else "")
    return {"ok": True, "app_sync": bool(bb_doc)}


# ── uploads (can_upload): into drop-off folders an admin chooses ────────────
UPLOAD_TYPES = {"book": {"epub", "kepub", "azw3", "mobi", "pdf", "fb2", "djvu", "cbz", "cbr", "cb7"},
                "audio": {"m4b", "m4a", "mp3", "aac", "flac", "ogg", "opus"}}


def _upload_settings():
    con = state()
    try:
        return {"upload_dir": _setting(con, "upload_dir") or "", "upload_audio_dir": _setting(con, "upload_audio_dir") or "",
                "upload_max_mb": int(_setting(con, "upload_max_mb") or 2048), "public_url": _setting(con, "public_url") or ""}
    finally:
        con.close()


@app.get("/api/upload")
def api_upload_info():
    s = _upload_settings()
    return {"books_enabled": bool(s["upload_dir"]), "audio_enabled": bool(s["upload_audio_dir"]),
            "max_mb": s["upload_max_mb"], "types": {k: sorted(v) for k, v in UPLOAD_TYPES.items()}}


@app.post("/api/upload")
async def api_upload(request: Request):
    """Multipart upload of one or more files. Ebooks and comics go to the books drop-off folder
    (e.g. your Calibre-Web-Automated ingest), audiobooks to the audio folder (a folder each)."""
    from . import files
    s = _upload_settings()
    me = _account(request)["username"]
    form = await request.form()
    results = []
    for item in form.getlist("files"):
        name = files.safe_name(getattr(item, "filename", "") or "upload")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        kind = next((k for k, exts in UPLOAD_TYPES.items() if ext in exts), None)
        dest = {"book": s["upload_dir"], "audio": s["upload_audio_dir"]}.get(kind or "")
        if not kind:
            results.append({"file": name, "ok": False, "message": f".{ext or '?'} files aren't accepted"})
            continue
        if not dest or not os.path.isdir(dest):
            results.append({"file": name, "ok": False, "message": "Uploads for this type aren't set up (ask an admin)"})
            continue
        if kind == "audio":                        # Audiobookshelf wants a folder per book
            dest = os.path.join(dest, "Uploads", name.rsplit(".", 1)[0])
            os.makedirs(dest, exist_ok=True)
        final = os.path.join(dest, name)
        n = 2
        while os.path.exists(final):
            stem, dot, e2 = name.rpartition(".")
            final = os.path.join(dest, f"{stem} ({n}).{e2}")
            n += 1
        tmp = final + ".part"                      # ingest tools skip .part files until renamed
        size, limit = 0, s["upload_max_mb"] * 1024 * 1024
        try:
            with open(tmp, "wb") as out:
                while chunk := await item.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"larger than {s['upload_max_mb']} MB")
                    out.write(chunk)
            os.replace(tmp, final)
        except Exception as ex:
            if os.path.exists(tmp):
                os.remove(tmp)
            results.append({"file": name, "ok": False, "message": f"Upload failed: {ex}"})
            continue
        live._audit(STATE, "upload", {"by": me, "file": os.path.basename(final), "bytes": size, "kind": kind}, "ok")
        notify.send(cfg, STATE, "upload", "New upload", f"{me} uploaded {os.path.basename(final)}.")
        results.append({"file": os.path.basename(final), "ok": True, "message": "Uploaded; it appears after the next library scan"})
    if not results:
        raise HTTPException(400, "No files received (field name: files)")
    return {"ok": all(r["ok"] for r in results), "results": results}


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


def _do_game(body, account_id=None):
    try:
        return requests_.game_request(cfg, body["game"].strip(), body["platform"].strip())
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/request/game")
async def api_request_game(request: Request):
    body = await request.json()
    game, platform = (body.get("game") or "").strip(), (body.get("platform") or "").strip()
    if not game or not platform:
        raise HTTPException(400, "game and platform are required")
    if _needs_approval(request):
        return _queue(request, "game", f"{game} ({platform})", body)
    return _do_game(body)


@app.get("/api/request/search")
def api_request_search(q: str):
    """Search Seerr for movies/TV not in the library."""
    return {"results": requests_.seerr_search(cfg, q) if q.strip() else []}


@app.get("/api/request/book/candidates")
def api_book_candidates(format: str, request: Request, work: str = "", title: str = "", author: str = ""):
    """Shelfmark candidates for a library work, or for any title + author (related works)."""
    if format not in wanted.FORMATS:
        raise HTTPException(400, "format must be ebook, audiobook or comic")
    if work:
        w = search.work(INDEX, work, _adult_ok(_session(request)))
        if not w:
            raise HTTPException(404, "Not found")
    elif title.strip():
        w = {"title": title.strip(), "authors": [author.strip()] if author.strip() else []}
    else:
        raise HTTPException(400, "work or title required")
    query = " ".join(x for x in (w["title"], (w["authors"] or [""])[0]) if x)
    try:
        cands = requests_.book_candidates(cfg, query, wanted.shelfmark_type(format))
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
    if format not in wanted.FORMATS:
        raise HTTPException(400, "format must be ebook, audiobook or comic")
    try:
        return {"releases": requests_.book_releases(cfg, provider, book_id, wanted.shelfmark_type(format))}
    except Exception as e:
        raise HTTPException(502, f"Shelfmark: {e}")


@app.post("/api/request/book/download")
async def api_book_download(request: Request):
    body = await request.json()
    rel = body.get("release")
    if not isinstance(rel, dict) or "source" not in rel or "source_id" not in rel:
        raise HTTPException(400, "release with source and source_id required")
    if _needs_approval(request):
        return _queue(request, "book_download", body.get("title") or rel.get("title") or "a book", body)
    return _do_book_download(body, (_account(request) or {}).get("id"))


def _do_book_download(body, account_id=None):
    rel = body["release"]
    try:
        out = requests_.book_download(cfg, rel)
    except Exception as e:
        raise HTTPException(502, f"Shelfmark: {e}")
    # Track it: if this copy fails, the wanted-list job finds another one.
    w = search.work(INDEX, body.get("work") or "", True) if body.get("work") else None
    fmt = body.get("format") if body.get("format") in wanted.FORMATS else rel.get("content_type")
    if fmt in wanted.FORMATS:
        wanted.add(STATE, (w or {}).get("title") or body.get("title") or rel.get("title") or "",
                   ((w or {}).get("authors") or [body.get("author") or ""])[0],
                   fmt, work_id=(w or {}).get("id"), provider=body.get("provider"), book_id=body.get("book_id"),
                   current=rel.get("source_id"), current_title=rel.get("title"), account_id=account_id)
    return out


# ── wanted books (keep looking until it arrives) ─────────────────────────────
@app.get("/api/wanted")
def api_wanted_list():
    return {"items": wanted.list_all(STATE), "retry_days": wanted.RETRY_DAYS}


@app.post("/api/wanted")
async def api_wanted_add(request: Request):
    """{work, format} or {title, author, format} -> keep looking for that format of this book
    (or a book/comic not in the library yet), picking a copy automatically."""
    body = await request.json()
    fmt = body.get("format")
    w = search.work(INDEX, body.get("work") or "", _adult_ok(_session(request))) if body.get("work") else None
    if not w and (body.get("title") or "").strip():
        w = {"id": None, "title": body["title"].strip(), "authors": [(body.get("author") or "").strip()]}
    if not w or fmt not in wanted.FORMATS:
        raise HTTPException(400, "work (or title) and format (ebook|audiobook|comic) required")
    payload = {"title": w["title"], "author": (w["authors"] or [""])[0], "format": fmt, "work": w["id"],
               "provider": body.get("provider"), "book_id": body.get("book_id")}
    if _needs_approval(request):
        return _queue(request, "wanted", f"{w['title']} ({fmt})", payload)
    return _do_wanted(payload, _account(request)["id"])


def _do_wanted(p, account_id=None):
    wid = wanted.add(STATE, p["title"], p.get("author") or "", p["format"], work_id=p.get("work"),
                     provider=p.get("provider"), book_id=p.get("book_id"), account_id=account_id)
    threading.Thread(target=_wanted_tick, daemon=True).start()       # first search right away
    return {"ok": True, "id": wid}


# ── approvals: "can ask" members' requests wait here for an admin ────────────
QUEUE_SCHEMA = """CREATE TABLE IF NOT EXISTS request_queue (
  id INTEGER PRIMARY KEY, account_id INTEGER, kind TEXT, title TEXT, payload TEXT, created REAL,
  status TEXT, decided_by TEXT, decided_at REAL, note TEXT)"""
_DO = {"screen": _do_screen, "game": _do_game, "book_download": _do_book_download, "wanted": _do_wanted}


def _needs_approval(request):
    acct = _account(request)
    return bool(acct) and not accounts.allowed(acct, "can_request")


def _queue(request, kind, title, payload):
    acct = _account(request)
    con = state()
    con.execute(QUEUE_SCHEMA)
    with con:
        cur = con.execute("INSERT INTO request_queue (account_id, kind, title, payload, created, status) VALUES (?,?,?,?,?,'pending')",
                          (acct["id"], kind, title[:200], json.dumps(payload), time.time()))
    con.close()
    live._audit(STATE, "request.queued", {"by": acct["username"], "kind": kind, "title": title}, "pending")
    notify.send(cfg, STATE, "approval_needed", "Request waiting for approval",
                f"{acct['username']} asked for {title}. Approve or deny it in Omnarr (Activity → Requests).", to_admins=True)
    return {"ok": True, "queued": True, "id": cur.lastrowid, "message": "Sent to an admin for approval"}


@app.get("/api/requests")
def api_requests(request: Request):
    """Admins: everything pending + recent decisions. Members: their own."""
    acct = _account(request)
    con = state()
    con.execute(QUEUE_SCHEMA)
    try:
        q = ("SELECT q.*, a.username FROM request_queue q LEFT JOIN accounts a ON a.id=q.account_id")
        if acct["role"] == "admin":
            rows = con.execute(q + " ORDER BY q.status<>'pending', q.created DESC LIMIT 100").fetchall()
        else:
            rows = con.execute(q + " WHERE q.account_id=? ORDER BY q.created DESC LIMIT 50", (acct["id"],)).fetchall()
    finally:
        con.close()
    return {"requests": [{"id": r["id"], "kind": r["kind"], "title": r["title"], "status": r["status"], "by": r["username"],
                          "created": r["created"], "decided_by": r["decided_by"], "decided_at": r["decided_at"],
                          "note": r["note"]} for r in rows],
            "pending": sum(1 for r in rows if r["status"] == "pending")}


def _decide(request, rid, approve, note=""):
    me = _account(request)
    if me["role"] != "admin":
        raise HTTPException(403, "Only an admin can do that")
    con = state()
    con.execute(QUEUE_SCHEMA)
    row = con.execute("SELECT * FROM request_queue WHERE id=?", (rid,)).fetchone()
    con.close()
    if not row or row["status"] != "pending":
        raise HTTPException(404, "No pending request with that id")
    status, msg = "denied", note or ""
    if approve:
        try:
            _DO[row["kind"]](json.loads(row["payload"]), row["account_id"])
            status = "approved"
        except HTTPException as e:
            status, msg = "failed", str(e.detail)
    con = state()
    with con:
        con.execute("UPDATE request_queue SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                    (status, me["username"], time.time(), msg[:300], rid))
    con.close()
    live._audit(STATE, f"request.{status}", {"by": me["username"], "title": row["title"]}, status)
    words = {"approved": "was approved and is on its way", "denied": "was declined", "failed": "was approved but couldn't be started"}
    notify.send(cfg, STATE, "request_decided", f"Your request: {row['title']}",
                f"Your request for {row['title']} {words[status]}." + (f" Note: {msg}" if msg else ""), to=[row["account_id"]])
    return {"ok": status != "failed", "status": status, "message": msg}


@app.post("/api/requests/{rid}/approve")
def api_request_approve(rid: int, request: Request):
    return _decide(request, rid, True)


@app.post("/api/requests/{rid}/deny")
async def api_request_deny(rid: int, request: Request):
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    return _decide(request, rid, False, (body.get("note") or "").strip())


@app.delete("/api/requests/{rid}")
def api_request_cancel(rid: int, request: Request):
    """Withdraw your own pending request (admins can remove any pending one)."""
    me = _account(request)
    con = state()
    con.execute(QUEUE_SCHEMA)
    with con:
        n = con.execute("DELETE FROM request_queue WHERE id=? AND status='pending' AND (account_id=? OR ?)",
                        (rid, me["id"], int(me["role"] == "admin"))).rowcount
    con.close()
    if not n:
        raise HTTPException(404, "No pending request of yours with that id")
    return {"ok": True}


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


def _startup():
    os.makedirs(os.path.dirname(INDEX) or ".", exist_ok=True)
    threading.Thread(target=_scheduler, name="indexer", daemon=True).start()
    threading.Thread(target=_wanted_loop, name="wanted", daemon=True).start()


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
