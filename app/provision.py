"""Let people make their own logins in the server's apps ("Set up my apps").

Omnarr uses the admin access it already has to create an account in each app, with the
person's Omnarr username and a password they choose (never stored here), then links the new
identities into their Omnarr account so progress lands in their own apps.

  Jellyfin, Audiobookshelf, Komga, RomM, Storyteller: through each app's own API.
  Calibre-Web (Automated): its admin "Add user" form, plus the user's Kobo sync token.
  BookBridge: its admin Users form, then the person's per-user integrations (their ABS key,
  Calibre-Web/Kobo login, Storyteller login and a KOSync login for their e-reader).

Every account made here is recorded in app_accounts so it can be shown and removed later.
Stash is deliberately not offered.
"""
import json
import logging
from contextlib import contextmanager
import re
import secrets
import sqlite3
import time

import httpx

log = logging.getLogger("omnarr.provision")

ORDER = ("jellyfin", "abs", "komga", "calibre_web", "storyteller", "romm", "bookbridge")
LABELS = {"jellyfin": "Jellyfin", "abs": "Audiobookshelf", "komga": "Komga", "calibre_web": "Calibre-Web (Kobo sync)",
          "storyteller": "Storyteller", "romm": "RomM", "bookbridge": "BookBridge (reading sync)"}
ABOUT = {"jellyfin": "Movies and shows in the Jellyfin app on a TV, phone or streaming stick.",
         "abs": "Audiobooks in the Audiobookshelf app.",
         "komga": "Comics in a comic app (Komga, Mihon, Panels).",
         "calibre_web": "Ebooks on a Kobo, synced over Wi-Fi.",
         "storyteller": "Read-alongs in the Storyteller app.",
         "romm": "Games in RomM.",
         "bookbridge": "Keeps your Kobo, audiobook and read-along places in step."}
SCHEMA = """CREATE TABLE IF NOT EXISTS app_accounts (
  account_id INTEGER, app TEXT, remote_id TEXT, username TEXT, extra TEXT, created REAL,
  PRIMARY KEY (account_id, app))"""
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")


class Failed(Exception):
    """A step failed; the message is shown to the person."""


# ── what's available ─────────────────────────────────────────────────────────
def _src(cfg, key):
    return cfg.source(key) or {}


def ready(cfg):
    """{app: True} for the apps Omnarr can create logins in with its current connections."""
    jf, ab, ko, rm = _src(cfg, "jellyfin"), _src(cfg, "abs"), _src(cfg, "komga"), _src(cfg, "romm")
    cal, st, bb = _src(cfg, "calibre"), _src(cfg, "storyteller"), _src(cfg, "bookbridge")
    return {"jellyfin": bool(jf.get("url") and jf.get("api_key")),
            "abs": bool(ab.get("url") and ab.get("api_key")),
            "komga": bool(ko.get("url") and ko.get("api_key")),
            "calibre_web": bool(cal.get("web_url") and cal.get("admin_user") and cal.get("admin_password")),
            "storyteller": bool(st.get("url") and st.get("admin_user") and st.get("admin_password")),
            "romm": bool(rm.get("url") and rm.get("api_key")),
            "bookbridge": bool(bb.get("sync_url") and bb.get("admin_user") and bb.get("admin_password") and bb.get("db"))}


def _con(state_path):
    con = sqlite3.connect(state_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def mine(state_path, account_id):
    """{app: {username, extra}} for the logins already made for this account."""
    con = _con(state_path)
    try:
        return {r["app"]: {"username": r["username"], "remote_id": r["remote_id"], "extra": json.loads(r["extra"] or "{}"),
                           "created": r["created"]}
                for r in con.execute("SELECT * FROM app_accounts WHERE account_id=?", (account_id,))}
    finally:
        con.close()


def _save(state_path, account_id, app, remote_id, username, extra=None):
    con = _con(state_path)
    with con:
        con.execute("INSERT OR REPLACE INTO app_accounts VALUES (?,?,?,?,?,?)",
                    (account_id, app, str(remote_id), username, json.dumps(extra or {}), time.time()))
    con.close()


def _forget(state_path, account_id, app):
    con = _con(state_path)
    with con:
        con.execute("DELETE FROM app_accounts WHERE account_id=? AND app=?", (account_id, app))
    con.close()


def contact_email(acct):
    """Apps that insist on an email get the person's own, or a placeholder that never receives mail."""
    email = (acct.get("email") or "").strip()
    return email or f"{acct['username'].lower()}@omnarr.local"


def _err(r, app):
    try:
        body = r.json()
        msg = body.get("message") or body.get("detail") or body.get("error") or ""
    except Exception:
        msg = r.text
    msg = re.sub(r"<[^>]+>", " ", str(msg or "")).strip()[:200]
    return Failed(f"{LABELS[app]} said {r.status_code}{': ' + msg if msg else ''}")


# ── Jellyfin ─────────────────────────────────────────────────────────────────
def _jf(cfg):
    s = _src(cfg, "jellyfin")
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=30,
                        headers={"Authorization": f'MediaBrowser Token="{s["api_key"]}"'})


def jellyfin_create(cfg, acct, username, password):
    adult_names = {n.strip().lower() for n in (_src(cfg, "jellyfin").get("adult_libraries") or []) if n.strip()}
    with _jf(cfg) as c:
        if any(u.get("Name", "").lower() == username.lower() for u in c.get("/Users").json()):
            raise Failed(f"Jellyfin already has a user called {username}")
        r = c.post("/Users/New", json={"Name": username, "Password": password})
        if r.status_code >= 400:
            raise _err(r, "jellyfin")
        user = r.json()
        policy = dict(user.get("Policy") or {})
        policy.update(IsAdministrator=False, EnableContentDownloading=bool(acct.get("can_download")),
                      EnableContentDeletion=False, EnableRemoteAccess=True)
        if adult_names and not acct.get("adult_allowed"):
            folders = c.get("/Library/MediaFolders").json().get("Items", [])
            policy.update(EnableAllFolders=False,
                          EnabledFolders=[f["Id"] for f in folders if f.get("Name", "").lower() not in adult_names])
        r = c.post(f"/Users/{user['Id']}/Policy", json=policy)
        if r.status_code >= 400:
            c.delete(f"/Users/{user['Id']}")
            raise _err(r, "jellyfin")
    return user["Id"], {}


def jellyfin_delete(cfg, remote_id):
    with _jf(cfg) as c:
        r = c.delete(f"/Users/{remote_id}")
    return r.status_code < 400 or r.status_code == 404


# ── Audiobookshelf ───────────────────────────────────────────────────────────
def _abs(cfg):
    s = _src(cfg, "abs")
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=30, headers={"Authorization": f"Bearer {s['api_key']}"})


def abs_create(cfg, acct, username, password):
    with _abs(cfg) as c:
        r = c.post("/api/users", json={"username": username, "password": password, "type": "user", "isActive": True,
                                       "permissions": {"download": bool(acct.get("can_download")), "update": False,
                                                       "delete": False, "upload": False,
                                                       "accessExplicitContent": bool(acct.get("adult_allowed"))}})
        if r.status_code >= 400:
            raise _err(r, "abs")
        body = r.json()
        uid = (body.get("user") or body).get("id")
        k = c.post("/api/api-keys", json={"name": f"Omnarr ({username})", "userId": uid, "isActive": True})
        if k.status_code >= 400 or not (k.json().get("apiKey") or {}).get("apiKey"):
            c.delete(f"/api/users/{uid}")
            raise _err(k, "abs")
    return uid, {"api_key": k.json()["apiKey"]["apiKey"]}


def abs_delete(cfg, remote_id):
    with _abs(cfg) as c:
        r = c.delete(f"/api/users/{remote_id}")
    return r.status_code < 400 or r.status_code == 404


# ── Komga ────────────────────────────────────────────────────────────────────
def _komga(cfg):
    s = _src(cfg, "komga")
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=30, headers={"X-API-Key": s["api_key"]})


def komga_create(cfg, acct, username, password):
    email = contact_email(acct)
    roles = ["PAGE_STREAMING", "KOREADER_SYNC", "KOBO_SYNC"] + (["FILE_DOWNLOAD"] if acct.get("can_download") else [])
    with _komga(cfg) as c:
        r = c.post("/api/v2/users", json={"email": email, "password": password, "roles": roles,
                                          "sharedLibraries": {"all": True, "libraryIds": []}})
        if r.status_code >= 400:
            raise _err(r, "komga")
    return r.json().get("id"), {"login": email}


def komga_delete(cfg, remote_id):
    with _komga(cfg) as c:
        r = c.delete(f"/api/v2/users/{remote_id}")
    return r.status_code < 400 or r.status_code == 404


# ── RomM ─────────────────────────────────────────────────────────────────────
def _romm(cfg):
    s = _src(cfg, "romm")
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=30,
                        headers={"Authorization": f"Bearer {s['api_key']}", "Accept": "application/json"})


def romm_create(cfg, acct, username, password):
    with _romm(cfg) as c:
        r = c.post("/api/users", json={"username": username, "email": (acct.get("email") or "").strip(),
                                       "password": password, "role": "viewer"})
        if r.status_code == 403:
            raise Failed("RomM's API token in Omnarr can't add users. Make one with the users.write permission "
                         "(RomM → your profile → Client API Tokens) and paste it in Connections → RomM")
        if r.status_code >= 400:
            raise _err(r, "romm")
    return r.json().get("id"), {}


def romm_delete(cfg, remote_id):
    with _romm(cfg) as c:
        r = c.delete(f"/api/users/{remote_id}")
    return r.status_code < 400 or r.status_code == 404


# ── Storyteller ──────────────────────────────────────────────────────────────
@contextmanager
def _storyteller(cfg):
    s = _src(cfg, "storyteller")
    with httpx.Client(base_url=s["url"].rstrip("/"), timeout=30) as c:
        r = c.post("/api/v2/token", data={"usernameOrEmail": s["admin_user"], "password": s["admin_password"]})
        if r.status_code >= 400 or not r.json().get("access_token"):
            raise Failed("Storyteller didn't accept the admin login saved in Connections → Storyteller")
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c


def storyteller_create(cfg, acct, username, password):
    email = contact_email(acct)
    with _storyteller(cfg) as c:
        r = c.post("/api/v2/invites", json={"email": email, "bookRead": True, "bookList": True,
                                            "bookDownload": bool(acct.get("can_download"))})
        if r.status_code >= 400:
            raise _err(r, "storyteller")
        key = r.json()["key"]
        r = c.post("/api/v2/users", json={"inviteKey": key, "email": email, "username": username,
                                          "name": username, "password": password})
        if r.status_code >= 400:
            c.delete(f"/api/v2/invites/{key}")
            raise _err(r, "storyteller")
        uid = next((u["id"] for u in c.get("/api/v2/users").json() if u.get("username") == username), "")
    return uid, {"login": username}


def storyteller_delete(cfg, remote_id):
    with _storyteller(cfg) as c:
        r = c.delete(f"/api/v2/users/{remote_id}")
    return r.status_code < 400 or r.status_code == 404


# ── Calibre-Web (Automated) ─────────────────────────────────────────────────
CSRF = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="csrf_token"')


def _csrf(html):
    m = CSRF.search(html or "")
    if not m:
        raise Failed("Calibre-Web's page didn't look as expected (no form token)")
    return m.group(1) or m.group(2)


@contextmanager
def _cwa(cfg):
    s = _src(cfg, "calibre")
    with httpx.Client(base_url=s["web_url"].rstrip("/"), timeout=30, follow_redirects=True) as c:
        token = _csrf(c.get("/login").text)
        r = c.post("/login", data={"csrf_token": token, "username": s["admin_user"], "password": s["admin_password"],
                                   "remember_me": "on"})
        if "/login" in str(r.url):
            raise Failed("Calibre-Web didn't accept the admin login saved in Connections → Calibre")
        yield c


def _kobo_base(cfg):
    s = _src(cfg, "calibre")
    return (s.get("browser_url") or s["web_url"]).rstrip("/")


def calibre_web_create(cfg, acct, username, password):
    with _cwa(cfg) as c:
        form = c.get("/admin/user/new").text
        data = {"csrf_token": _csrf(form), "name": username, "email": contact_email(acct), "password": password,
                "locale": "en", "default_language": "all", "viewer_role": "on", "passwd_role": "on",
                "edit_shelf_role": "on", "download_role": "on"}   # a Kobo downloads books through this role
        for name in re.findall(r'<input type="checkbox" name="((?:show_\d+)|Show_detail_random)"[^>]*\schecked', form):
            data[name] = "on"                     # keep the default sidebar the form starts with
        r = c.post("/admin/user/new", data=data)
        users = c.get("/ajax/listusers", params={"search": username, "limit": 50, "offset": 0}).json()
        rows = users.get("rows", users) if isinstance(users, dict) else users
        uid = next((u["id"] for u in rows if str(u.get("name", "")).lower() == username.lower()), None)
        if not uid:
            flash = re.search(r'class="alert[^"]*"[^>]*>(.*?)</div>', r.text, re.S)
            raise Failed("Calibre-Web didn't create the user" + (f": {re.sub('<[^>]+>', ' ', flash.group(1)).strip()[:160]}" if flash else ""))
        page = c.get(f"/kobo_auth/generate_auth_token/{uid}").text
        m = re.search(r"/kobo/([A-Za-z0-9]{8,})", page)
    token = m.group(1) if m else ""
    return uid, {"kobo_token": token, "kobo_endpoint": f"{_kobo_base(cfg)}/kobo/{token}" if token else ""}


def calibre_web_delete(cfg, remote_id):
    with _cwa(cfg) as c:
        token = _csrf(c.get(f"/admin/user/{remote_id}").text)
        r = c.post("/ajax/deleteuser", data={"userid": remote_id, "csrf_token": token},
                   headers={"X-CSRFToken": token})
    return r.status_code < 400


# ── BookBridge ───────────────────────────────────────────────────────────────
BB_CSRF = re.compile(r"""csrf[_-]?token["']?\s*(?:[:=]|content=)\s*["']([A-Za-z0-9_\-.]{16,})""", re.I)


@contextmanager
def _bookbridge(cfg):
    s = _src(cfg, "bookbridge")
    with httpx.Client(base_url=s["sync_url"].rstrip("/"), timeout=30, follow_redirects=True) as c:
        c.get("/login")
        r = c.post("/login", data={"username": s["admin_user"], "password": s["admin_password"]})
        if "/login" in str(r.url):
            raise Failed("BookBridge didn't accept the admin login saved in Connections → BookBridge")
        yield c


def _bb_post(c, path, data):
    m = BB_CSRF.search(c.get(path).text)
    token = m.group(1) if m else ""
    r = c.post(path, data={**data, "csrf_token": token}, headers={"X-CSRF-Token": token} if token else {})
    if r.status_code >= 400:
        raise _err(r, "bookbridge")
    return r


def _bb_user_id(cfg, username):
    con = sqlite3.connect(f"file:{_src(cfg, 'bookbridge')['db']}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT id FROM users WHERE lower(username)=lower(?)", (username,)).fetchone()
    finally:
        con.close()
    return r[0] if r else None


def bookbridge_create(cfg, acct, username, password, made):
    """made: what was just created ({app: extra}) so BookBridge gets this person's logins."""
    with _bookbridge(cfg) as c:
        _bb_post(c, "/admin/users", {"action": "create", "username": username, "password": password, "role": "user"})
        uid = _bb_user_id(cfg, username)
        if not uid:
            raise Failed("BookBridge didn't create the user")
        sync_key = secrets.token_urlsafe(18)    # Omnarr's reader syncs with this; the person's own password is never kept
        form = {"KOSYNC_ENABLED": "on", "KOSYNC_AUTH_METHOD": "kosync", "KOSYNC_USER": username, "KOSYNC_KEY": sync_key,
                "DEVICE_SYNC_COLLECTION_SOURCE": "off", "DEVICE_SYNC_COLLECTIONS": "off", "DEVICE_SYNC_HARDCOVER_LISTS": "all"}
        if made.get("abs", {}).get("api_key"):
            form.update(ABS_ENABLED="on", ABS_KEY=made["abs"]["api_key"])
        if "calibre_web" in made:
            form.update(CWA_ENABLED="on", CWA_USERNAME=username, CWA_PASSWORD=password)
            if made["calibre_web"].get("kobo_token"):
                form.update(CWA_SYNC_ENABLED="on", CWA_SYNC_TOKEN=made["calibre_web"]["kobo_token"])
        if "storyteller" in made:
            form.update(STORYTELLER_ENABLED="on", STORYTELLER_USER=username, STORYTELLER_PASSWORD=password)
        _bb_post(c, f"/admin/users/{uid}/integrations", form)
    return uid, {"kosync_user": username, "kosync_key": sync_key}


def bookbridge_delete(cfg, remote_id):
    with _bookbridge(cfg) as c:
        _bb_post(c, "/admin/users", {"action": "delete", "user_id": str(remote_id)})
    return True


CREATE = {"jellyfin": jellyfin_create, "abs": abs_create, "komga": komga_create, "romm": romm_create,
          "storyteller": storyteller_create, "calibre_web": calibre_web_create}
DELETE = {"jellyfin": jellyfin_delete, "abs": abs_delete, "komga": komga_delete, "romm": romm_delete,
          "storyteller": storyteller_delete, "calibre_web": calibre_web_delete, "bookbridge": bookbridge_delete}


# ── the whole run ────────────────────────────────────────────────────────────
def create(cfg, state_path, acct, apps, password):
    """Make the requested logins in order. Returns {"results": [{app, ok, message, username, ...}],
    "link": {jellyfin_user, abs_api_key, kosync_user, kosync_key}} for the account to save."""
    username = acct["username"]
    if not USERNAME.match(username):
        raise Failed("Your Omnarr username has characters these apps don't accept. Ask an admin to rename you first")
    if len(password or "") < 8:
        raise Failed("Pick a password of at least 8 characters")
    have = mine(state_path, acct["id"])
    can = ready(cfg)
    wanted = [a for a in ORDER if a in apps and can.get(a) and a not in have]
    made, results, link = {}, [], {}
    for app in wanted:
        try:
            if app == "bookbridge":
                remote_id, extra = bookbridge_create(cfg, acct, username, password,
                                                     {**{k: v["extra"] for k, v in have.items()}, **made})
            else:
                remote_id, extra = CREATE[app](cfg, acct, username, password)
        except Failed as e:
            results.append({"app": app, "label": LABELS[app], "ok": False, "message": str(e)})
            continue
        except Exception as e:                    # an app that's down, or a page that changed
            log.warning("creating %s login for %s failed: %s", app, username, e)
            msg = (f"Omnarr couldn't reach {LABELS[app]}" if isinstance(e, (httpx.ConnectError, httpx.TimeoutException))
                   else f"{type(e).__name__}: {e}"[:240])
            results.append({"app": app, "label": LABELS[app], "ok": False, "message": msg})
            continue
        made[app] = extra
        _save(state_path, acct["id"], app, remote_id, username, {k: v for k, v in extra.items() if k not in ("api_key", "kosync_key")})
        results.append({"app": app, "label": LABELS[app], "ok": True, "username": extra.get("login", username),
                        "kobo_endpoint": extra.get("kobo_endpoint", "")})
        if app == "jellyfin":
            link["jellyfin_user"] = username
        elif app == "abs":
            link["abs_api_key"] = extra["api_key"]
        elif app == "bookbridge":
            link.update(kosync_user=username, kosync_key=extra["kosync_key"])
    return {"results": results, "link": link}


def remove_all(cfg, state_path, account_id):
    """Delete every login made for this account. Returns the apps that need removing by hand."""
    left = []
    for app, row in mine(state_path, account_id).items():
        try:
            ok = DELETE[app](cfg, row["remote_id"]) if row["remote_id"] else False
        except Exception as e:
            log.warning("removing %s login %s failed: %s", app, row["username"], e)
            ok = False
        if ok:
            _forget(state_path, account_id, app)
        else:
            left.append(LABELS[app])
    return left
