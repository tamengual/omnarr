"""Omnarr accounts: who is signed in, what they may do, and which app identities are theirs.

Roles: "admin" manages connections, users and the library and may do everything. Members
have permission switches set by an admin:
  can_request  - trigger downloads/searches on the server (Seerr, Shelfmark, ROMarr, Sonarr/Radarr actions)
  can_ask      - ask for things anyway: the request waits in a queue until an admin approves it
  can_download - save files they can see to their own device
  can_upload   - upload their own files to the server's drop-off folder
  adult_allowed - use the PIN-locked private section
A "guest" is simply a member with only viewing/playing (and optionally downloading). Each account can map its own Jellyfin user and Audiobookshelf API key so playback and
progress go to *their* accounts. A member is never given the owner's identity: unmapped
members can browse and play, but nothing is written back for them.

Upgrading from the single-password version: the existing password becomes the account
"admin", the private-section PIN moves onto it, and every existing session is attached to it.
"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, salt TEXT, hash TEXT,
  role TEXT NOT NULL DEFAULT 'member', adult_allowed INTEGER NOT NULL DEFAULT 0,
  pin_salt TEXT, pin_hash TEXT, jellyfin_user TEXT, abs_user TEXT, abs_api_key TEXT,
  ha_user_id TEXT UNIQUE, created REAL,
  can_request INTEGER NOT NULL DEFAULT 1, can_download INTEGER NOT NULL DEFAULT 0,
  can_upload INTEGER NOT NULL DEFAULT 0, can_ask INTEGER NOT NULL DEFAULT 0,
  email TEXT, notify_email INTEGER NOT NULL DEFAULT 1)
"""
PERMISSIONS = ("can_request", "can_ask", "can_download", "can_upload", "adult_allowed")
INVITES = """
CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE, created_by INTEGER, created REAL, expires REAL,
  email TEXT, note TEXT, preset TEXT, used_by INTEGER, used_at REAL)
"""
ROLES = ("admin", "member")
PUBLIC_FIELDS = ("id", "username", "role", "adult_allowed", "can_request", "can_ask", "can_download", "can_upload",
                 "jellyfin_user", "abs_user", "created", "ha_user_id", "email", "notify_email")


def hash_secret(secret, salt):
    return hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def ensure(con):
    """Create the table, add account columns to older tables, and import the legacy single
    password once. Safe to call on every start."""
    con.execute(SCHEMA)
    con.execute(INVITES)
    cols = _columns(con, "accounts")
    for col, default in (("can_request", 1), ("can_download", 0), ("can_upload", 0), ("can_ask", 0), ("notify_email", 1)):
        if col not in cols:
            con.execute(f"ALTER TABLE accounts ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
    if "email" not in cols:
        con.execute("ALTER TABLE accounts ADD COLUMN email TEXT")
    if "account_id" not in _columns(con, "sessions"):
        con.execute("ALTER TABLE sessions ADD COLUMN account_id INTEGER")
    for table in ("wanted_books",):              # (not `actions`: the audit log keeps its 4 columns)
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists and "account_id" not in _columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER")
    if con.execute("SELECT count(*) FROM accounts").fetchone()[0]:
        return
    get = lambda k: (con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone() or [None])[0]
    if not get("password_hash"):
        return                                    # fresh install: the first visitor creates the admin
    conn = {r[0]: json.loads(r[1] or "{}") for r in con.execute("SELECT app, settings FROM connections")} \
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='connections'").fetchone() else {}
    cur = con.execute(
        "INSERT INTO accounts (username, salt, hash, role, adult_allowed, pin_salt, pin_hash, jellyfin_user, abs_user, created) "
        "VALUES ('admin', ?, ?, 'admin', 1, ?, ?, ?, ?, ?)",
        (get("password_salt"), get("password_hash"), get("adult_pin_salt"), get("adult_pin_hash"),
         (conn.get("jellyfin") or {}).get("user"), (conn.get("abs") or {}).get("user"), time.time()))
    con.execute("UPDATE sessions SET account_id=? WHERE account_id IS NULL", (cur.lastrowid,))
    con.commit()


def public(row):
    """Account fields safe to send to the browser (secrets reduced to set/not set)."""
    if not row:
        return None
    d = {k: row[k] for k in PUBLIC_FIELDS if k in row.keys()}
    for k in (*PERMISSIONS, "notify_email"):
        if k in d:
            d[k] = bool(d[k])
    if d.get("role") == "admin":                  # admins can do everything
        d.update({k: True for k in PERMISSIONS if k != "adult_allowed"})
    d["has_password"] = bool(row["hash"])
    d["pin_set"] = bool(row["pin_hash"])
    key = row["abs_api_key"] or ""
    d["abs_api_key"] = ("••••" + key[-4:]) if key else ""
    return d


def get(con, account_id):
    return con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone() if account_id else None


def by_username(con, username):
    return con.execute("SELECT * FROM accounts WHERE username=?", ((username or "").strip(),)).fetchone()


def count(con, role=None):
    if role:
        return con.execute("SELECT count(*) FROM accounts WHERE role=?", (role,)).fetchone()[0]
    return con.execute("SELECT count(*) FROM accounts").fetchone()[0]


def _clean_username(username):
    u = (username or "").strip()
    if not 1 <= len(u) <= 40 or any(c in u for c in "<>\"'&/\\"):
        raise ValueError("Username must be 1-40 characters, without < > \" ' & / \\")
    return u


def allowed(account, permission):
    """Admins may do everything; members only what their switches allow."""
    if not account:
        return False
    return account["role"] == "admin" or bool(account[permission])


def create(con, username, password=None, role="member", adult_allowed=False, ha_user_id=None,
           can_request=True, can_download=False, can_upload=False, can_ask=False, email=None):
    if role not in ROLES:
        raise ValueError("role must be admin or member")
    username = _clean_username(username)
    if by_username(con, username):
        raise ValueError("That username is taken")
    salt = hash_ = None
    if password is not None:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        salt = secrets.token_hex(16)
        hash_ = hash_secret(password, salt)
    cur = con.execute(
        "INSERT INTO accounts (username, salt, hash, role, adult_allowed, ha_user_id, created, can_request, can_download, "
        "can_upload, can_ask, email) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (username, salt, hash_, role, int(bool(adult_allowed)), ha_user_id, time.time(),
         int(bool(can_request)), int(bool(can_download)), int(bool(can_upload)), int(bool(can_ask)),
         _clean_email(email)))
    con.commit()
    return cur.lastrowid


def verify_password(row, password):
    return bool(row and row["hash"]) and hmac.compare_digest(hash_secret(password or "", row["salt"]), row["hash"])


def set_password(con, account_id, password):
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = secrets.token_hex(16)
    con.execute("UPDATE accounts SET salt=?, hash=? WHERE id=?", (salt, hash_secret(password, salt), account_id))
    con.commit()


def verify_pin(row, pin):
    return bool(row and row["pin_hash"]) and hmac.compare_digest(hash_secret(pin or "", row["pin_salt"]), row["pin_hash"])


def set_pin(con, account_id, pin):
    pin = str(pin or "")
    if not pin.isdigit() or not 4 <= len(pin) <= 12:
        raise ValueError("PIN must be 4-12 digits")
    salt = secrets.token_hex(16)
    con.execute("UPDATE accounts SET pin_salt=?, pin_hash=? WHERE id=?", (salt, hash_secret(pin, salt), account_id))
    con.commit()


def for_ha_user(con, ha_user_id, ha_name):
    """The account for a Home Assistant user, created on first visit. The first HA user on an
    install with no admin becomes the admin (HA users are the household that runs HA)."""
    row = con.execute("SELECT * FROM accounts WHERE ha_user_id=?", (ha_user_id,)).fetchone()
    if row:
        return row
    base = _clean_name(ha_name) or "ha-user"
    name, n = base, 2
    while by_username(con, name):
        name, n = f"{base}-{n}", n + 1
    role = "member" if count(con, "admin") else "admin"
    return get(con, create(con, name, None, role, adult_allowed=(role == "admin"), ha_user_id=ha_user_id))


def _clean_email(e):
    e = (e or "").strip()
    if not e:
        return None
    if "@" not in e or len(e) > 254 or any(c in e for c in " <>\"',;"):
        raise ValueError("That doesn't look like an email address")
    return e


def _clean_name(s):
    return "".join(c for c in (s or "") if c not in "<>\"'&/\\").strip()[:40]


def update(con, account_id, **fields):
    settable = {"role", "jellyfin_user", "abs_user", "abs_api_key", "username", "email", "notify_email", *PERMISSIONS}
    sets = {k: v for k, v in fields.items() if k in settable}
    if "role" in sets and sets["role"] not in ROLES:
        raise ValueError("role must be admin or member")
    if "username" in sets:
        sets["username"] = _clean_username(sets["username"])
        other = by_username(con, sets["username"])
        if other and other["id"] != account_id:
            raise ValueError("That username is taken")
    for k in (*PERMISSIONS, "notify_email"):
        if k in sets:
            sets[k] = int(bool(sets[k]))
    if "email" in sets:
        sets["email"] = _clean_email(sets["email"])
    for k in ("jellyfin_user", "abs_user", "abs_api_key"):
        if k in sets:
            sets[k] = (str(sets[k]).strip() or None) if sets[k] is not None else None
    if not sets:
        return
    con.execute(f"UPDATE accounts SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), account_id])
    con.commit()


def delete(con, account_id):
    con.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
    con.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    con.commit()


def admins(con):
    return [dict(r) for r in con.execute("SELECT * FROM accounts WHERE role='admin'")]


def list_all(con):
    return [public(r) for r in con.execute("SELECT * FROM accounts ORDER BY role, username COLLATE NOCASE")]


# ── invitations: a single-use sign-up link carrying the permissions it grants ──
def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_invite(con, created_by, preset, days=7, email=None, note=None):
    """Returns the raw token (only its hash is stored). preset: role + permission switches."""
    clean = {"role": "member" if preset.get("role") != "admin" else "admin"}
    clean.update({k: bool(preset.get(k, k == "can_request")) for k in PERMISSIONS})
    token = secrets.token_urlsafe(24)
    days = max(1, min(int(days or 7), 90))
    con.execute("INSERT INTO invites (token_hash, created_by, created, expires, email, note, preset) VALUES (?,?,?,?,?,?,?)",
                (_token_hash(token), created_by, time.time(), time.time() + days * 86400,
                 (email or "").strip() or None, (note or "").strip()[:200] or None, json.dumps(clean)))
    con.commit()
    return token


def find_invite(con, token):
    """The invite for a token if it's unused and unexpired, else None."""
    row = con.execute("SELECT * FROM invites WHERE token_hash=?", (_token_hash(token or ""),)).fetchone()
    if not row or row["used_by"] or row["expires"] < time.time():
        return None
    return row


def accept_invite(con, token, username, password):
    row = find_invite(con, token)
    if not row:
        raise ValueError("This invitation link has expired or was already used")
    preset = json.loads(row["preset"] or "{}")
    aid = create(con, username, password, preset.get("role", "member"), adult_allowed=preset.get("adult_allowed"),
                 can_request=preset.get("can_request"), can_download=preset.get("can_download"),
                 can_upload=preset.get("can_upload"), can_ask=preset.get("can_ask"), email=row["email"])
    con.execute("UPDATE invites SET used_by=?, used_at=? WHERE id=?", (aid, time.time(), row["id"]))
    con.commit()
    return aid


def list_invites(con):
    out = []
    for r in con.execute("""SELECT i.*, a.username AS used_by_name, c.username AS created_by_name FROM invites i
                            LEFT JOIN accounts a ON a.id=i.used_by LEFT JOIN accounts c ON c.id=i.created_by
                            ORDER BY i.created DESC LIMIT 100"""):
        status = "used" if r["used_by"] else ("expired" if r["expires"] < time.time() else "pending")
        out.append({"id": r["id"], "email": r["email"], "note": r["note"], "created": r["created"],
                    "expires": r["expires"], "status": status, "preset": json.loads(r["preset"] or "{}"),
                    "used_by": r["used_by_name"], "created_by": r["created_by_name"]})
    return out


def revoke_invite(con, invite_id):
    con.execute("DELETE FROM invites WHERE id=? AND used_by IS NULL", (invite_id,))
    con.commit()
