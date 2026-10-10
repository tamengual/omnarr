"""Every app Omnarr can connect to: what the setup wizard asks for, and how to test it.

A spec's `test(settings)` must exercise the same code path the indexer/feature uses and
return (ok, human message) — e.g. "Connected: 323 audiobooks". Field types:
url | secret | path | text | list | number | choice.
"""
import os

import httpx

from .base import ro_connect

BROWSER = {"key": "browser_url", "label": "Browser address", "type": "url", "required": False,
           "help": "How your browser/phone reaches it, if different from the address above (e.g. a Tailscale or public name). Used for \"Open in …\" links."}


def _get(url, headers=None, params=None, timeout=20):
    with httpx.Client(timeout=timeout, headers=headers or {}) as c:
        r = c.get(url, params=params)
    return r


def _arr_test(app, path="/api/v3/system/status"):
    def test(s):
        r = _get(s["url"].rstrip("/") + path, {"X-Api-Key": s["api_key"]})
        if r.status_code == 401:
            return False, "API key rejected"
        r.raise_for_status()
        d = r.json()
        return True, f"Connected: {app} {d.get('version', '')}".strip()
    return test


def _sqlite_count(path, sql, what):
    if not os.path.exists(path):
        return False, f"File not found inside the container: {path} — check the volume mount"
    con = ro_connect(path)
    try:
        n = con.execute(sql).fetchone()[0]
    finally:
        con.close()
    return True, f"Connected: {n} {what}"


def _test_calibre(s):
    return _sqlite_count(s["db"], "SELECT count(*) FROM books", "books in Calibre")


def _test_storyteller(s):
    return _sqlite_count(s["db"], "SELECT count(*) FROM readaloud WHERE status='ALIGNED'", "finished read-alongs")


def _test_bookbridge(s):
    ok, msg = _sqlite_count(s["db"], "SELECT count(*) FROM books", "linked books")
    if ok and s.get("sync_url"):
        from .. import bookbridge_sync
        if not bookbridge_sync.configured(s):
            return False, "Position sync needs the KOSync username and password too"
        good, why = bookbridge_sync.check(s)
        if not good:
            return False, f"{msg}, but position sync failed: {why}"
        msg += "; position sync signed in"
    return ok, msg


def _test_plex(s):
    from . import plex
    return plex.test(s)


def _test_readmeabook(s):
    from . import readmeabook
    return readmeabook.test(s)


def _test_custom_library(s):
    from .. import extend
    return extend.test_custom(s)


def _test_custom_requests(s):
    from .. import extend
    return extend.test_webhook(s)


def _test_abs(s):
    if s.get("api_key"):
        h = {"Authorization": f"Bearer {s['api_key']}"}
        me = _get(s["url"].rstrip("/") + "/api/me", h)
        if me.status_code in (401, 403):
            return False, "API key rejected"
        me.raise_for_status()
        libs = _get(s["url"].rstrip("/") + "/api/libraries", h).json().get("libraries", [])
        books = [l for l in libs if l.get("mediaType") == "book"]
        return True, f"Connected as {me.json().get('username')}: {len(books)} audiobook librar{'y' if len(books) == 1 else 'ies'}"
    if s.get("db"):
        return _sqlite_count(s["db"], "SELECT count(*) FROM libraryItems WHERE mediaType='book'", "audiobooks (database mode)")
    return False, "Add an API key (Audiobookshelf → Settings → API Keys)"


def _test_jellyfin(s):
    h = {"Authorization": f'MediaBrowser Token="{s["api_key"]}"'}
    r = _get(s["url"].rstrip("/") + "/Users", h)
    if r.status_code == 401:
        return False, "API key rejected"
    r.raise_for_status()
    users = [u["Name"] for u in r.json()]
    want = (s.get("user") or "").lower()
    if want and want not in [u.lower() for u in users]:
        return False, f"User '{s.get('user')}' not found. Users: {', '.join(users)}"
    return True, f"Connected: {len(users)} user{'s' if len(users) != 1 else ''} ({', '.join(users[:5])})"


def _test_seerr(s):
    r = _get(s["url"].rstrip("/") + "/api/v1/settings/main", {"X-Api-Key": s["api_key"]})
    if r.status_code in (401, 403):
        return False, "API key rejected"
    r.raise_for_status()
    st = _get(s["url"].rstrip("/") + "/api/v1/status").json()
    return True, f"Connected: Seerr {st.get('version', '')}".strip()


def _test_shelfmark(s):
    r = _get(s["url"].rstrip("/") + "/api/downloads/active", {"X-Api-Key": s["api_key"]})
    if r.status_code in (401, 403):
        return False, "API key rejected — set SHELFMARK_API_KEY on Shelfmark and paste the same value here"
    r.raise_for_status()
    return True, "Connected: Shelfmark"


def _test_romm(s):
    r = _get(s["url"].rstrip("/") + "/api/roms", {"Authorization": f"Bearer {s['api_key']}"}, {"limit": 1, "with_total": "true"})
    if r.status_code in (401, 403):
        return False, "Token rejected (needs roms.read, platforms.read, assets.read)"
    r.raise_for_status()
    d = r.json()
    return True, f"Connected: {d.get('total', '?')} games"


def _test_romarr(s):
    r = _get(s["url"].rstrip("/") + "/api/v1/system/status", {"X-Api-Key": s["api_key"]})
    if r.status_code in (401, 403):
        return False, "API key rejected"
    r.raise_for_status()
    return True, f"Connected: ROMarr {r.json().get('version', '')}".strip()


def _test_komga(s):
    r = _get(s["url"].rstrip("/") + "/api/v1/series", {"X-API-Key": s["api_key"]}, {"size": 1})
    if r.status_code in (401, 403):
        return False, "API key rejected (Komga → Account settings → API keys)"
    r.raise_for_status()
    return True, f"Connected: {r.json().get('totalElements', 0)} comic series"


def _test_stash(s):
    with httpx.Client(timeout=20) as c:
        r = c.post(s["url"].rstrip("/") + "/graphql", headers={"ApiKey": s.get("api_key", "")},
                   json={"query": "{ findScenes(filter:{per_page:0}){ count } }"})
    if r.status_code in (401, 403):
        return False, "API key rejected (Stash → Settings → Security → API key)"
    r.raise_for_status()
    return True, f"Connected: {r.json()['data']['findScenes']['count']} scenes"


def _test_notify(s):
    from .. import notify
    return notify.test(s)


def _test_tailscale(s):
    from .. import tailnet
    return tailnet.test(s)


def _test_email(s):
    from .. import mailer
    return mailer.test(s)


URL = lambda label, ph: {"key": "url", "label": f"{label} address", "type": "url", "required": True, "placeholder": ph,
                         "help": "How Omnarr's server reaches it, e.g. http://nas.local:8989, or http://sonarr:8989 on the same Docker network."}
KEY = lambda where: {"key": "api_key", "label": "API key", "type": "secret", "required": True, "help": where}


def SIGNUP(app, url_key, placeholder):
    """Optional admin login so people can make their own account in this app (Set up my apps)."""
    help_ = f"Only needed to let people make their own {app} login from Omnarr. Use an admin account."
    fields = [{"key": url_key, "label": f"{app} address for Omnarr's server", "type": "url", "required": False,
               "placeholder": placeholder, "help": help_}] if url_key else []
    return fields + [{"key": "admin_user", "label": f"{app} admin username (optional)", "type": "text", "required": False,
                      "help": help_ if not url_key else ""},
                     {"key": "admin_password", "label": f"{app} admin password", "type": "secret", "required": False}]

APPS = [
    # ── books ──
    {"key": "calibre", "label": "Calibre (ebooks)", "category": "Books", "test": _test_calibre,
     "about": "Your ebook library. Read from Calibre's metadata.db through a read-only volume mount. \"Open\" goes to Calibre-Web.",
     "fields": [{"key": "db", "label": "metadata.db path", "type": "path", "required": True, "placeholder": "/src/calibre/metadata.db",
                 "help": "Path INSIDE the Omnarr container. Mount your Calibre library folder read-only, e.g. /path/to/calibre:/src/calibre:ro."},
                {"key": "library", "label": "Library folder", "type": "path", "required": True, "placeholder": "/src/calibre"},
                {"key": "hide_tags", "label": "Hide books with these tags", "type": "list", "required": False, "placeholder": "not-mine"},
                {"key": "adult_tags", "label": "Private-section tags (default: NSFW, XXX, 18+)", "type": "list", "required": False, "placeholder": "NSFW"},
                {**BROWSER, "label": "Calibre-Web address", "help": "Where \"Read\" opens the book, e.g. http://host:8083."},
                *SIGNUP("Calibre-Web", "web_url", "http://host:8083")]},
    {"key": "abs", "label": "Audiobookshelf", "category": "Books", "test": _test_abs,
     "about": "Audiobooks, listening progress, and in-app listening.",
     "fields": [URL("Audiobookshelf", "http://host:13378"),
                KEY("Audiobookshelf → Settings → API Keys → Add (read + update progress)."),
                {**BROWSER, "placeholder": "http://host:13378/audiobookshelf"}]},
    {"key": "storyteller", "label": "Storyteller (read-alongs)", "category": "Books", "test": _test_storyteller,
     "about": "Read-along ebooks with synced narration. Read-only database mount.",
     "fields": [{"key": "db", "label": "storyteller.db path", "type": "path", "required": True, "placeholder": "/src/storyteller/storyteller.db"},
                {"key": "library", "label": "Library folder (optional)", "type": "path", "required": False, "placeholder": "/src/storyteller-library",
                 "help": "Mount Storyteller's import folder to match read-alongs to audiobooks/ebooks exactly by file."},
                BROWSER, *SIGNUP("Storyteller", "url", "http://host:8001")]},
    {"key": "bookbridge", "label": "BookBridge (optional)", "category": "Books", "test": _test_bookbridge,
     "about": "If you use BookBridge to sync reading positions, Omnarr reuses its book links and shows each app's position. "
              "Add its address and your KOSync login to make Omnarr's ebook reader one of your synced devices (admins).",
     "fields": [{"key": "db", "label": "database.db path", "type": "path", "required": True, "placeholder": "/src/bookbridge/database.db"},
                {"key": "sync_url", "label": "Position sync address (optional)", "type": "url", "required": False,
                 "placeholder": "http://host:8080", "help": "BookBridge's own address. Omnarr reports and reads positions like a KOReader device."},
                {"key": "kosync_user", "label": "KOSync username", "type": "text", "required": False,
                 "help": "The KOSync username and password set in BookBridge Settings (the same login your e-reader uses)."},
                {"key": "kosync_key", "label": "KOSync password", "type": "secret", "required": False},
                *SIGNUP("BookBridge", None, None)]},
    {"key": "shelfmark", "label": "Shelfmark (book requests)", "category": "Books", "test": _test_shelfmark,
     "about": "Request missing ebooks/audiobooks; Omnarr keeps looking until a good copy arrives.",
     "fields": [URL("Shelfmark", "http://host:8084"), KEY("Set SHELFMARK_API_KEY in Shelfmark's environment, then paste the same value.")]},
    {"key": "komga", "label": "Komga (comics)", "category": "Books", "test": _test_komga,
     "about": "Comics and manga series.", "fields": [URL("Komga", "http://host:25600"), KEY("Komga → Account settings → API keys."), BROWSER]},
    # ── screen ──
    {"key": "jellyfin", "label": "Jellyfin", "category": "Movies & TV", "test": _test_jellyfin,
     "about": "Movies and shows you have, watched state, and in-app playback.",
     "fields": [URL("Jellyfin", "http://host:8096"), KEY("Jellyfin → Dashboard → API Keys → +."),
                {"key": "user", "label": "Your Jellyfin user", "type": "text", "required": True, "help": "Whose watched state and progress to use."},
                {"key": "adult_libraries", "label": "Adult libraries (hidden behind the PIN)", "type": "list", "required": False}, BROWSER]},
    {"key": "sonarr", "label": "Sonarr", "category": "Movies & TV", "test": _arr_test("Sonarr"),
     "about": "Episodes you have/miss, downloads, search and monitor.", "fields": [URL("Sonarr", "http://host:8989"), KEY("Sonarr → Settings → General → API Key."), BROWSER]},
    {"key": "radarr", "label": "Radarr", "category": "Movies & TV", "test": _arr_test("Radarr"),
     "about": "Movie files, downloads, search and monitor.", "fields": [URL("Radarr", "http://host:7878"), KEY("Radarr → Settings → General → API Key."), BROWSER]},
    {"key": "seerr", "label": "Seerr / Jellyseerr / Overseerr", "category": "Movies & TV", "test": _test_seerr,
     "about": "Request movies and shows (including adaptations of your books).", "fields": [URL("Seerr", "http://host:5055"), KEY("Seerr → Settings → General → API Key."), BROWSER]},
    {"key": "prowlarr", "label": "Prowlarr (search harder)", "category": "Movies & TV", "test": _arr_test("Prowlarr", "/api/v1/system/status"),
     "about": "Searches every indexer directly when Sonarr/Radarr can't find something.", "fields": [URL("Prowlarr", "http://host:9696"), KEY("Prowlarr → Settings → General → API Key.")]},
    # ── games ──
    {"key": "romm", "label": "RomM (games)", "category": "Games", "test": _test_romm,
     "about": "Your game library.", "fields": [URL("RomM", "http://host:8080"), KEY("RomM → your profile → Client API Tokens (roms.read, platforms.read, assets.read)."), BROWSER]},
    {"key": "romarr", "label": "ROMarr (game requests)", "category": "Games", "test": _test_romarr,
     "about": "Request games by title and platform.", "fields": [URL("ROMarr", "http://host:6868"), KEY("ROMarr → Settings → General → API key.")]},
    # ── private ──
    {"key": "stash", "label": "Stash (private, PIN-locked)", "category": "Private", "test": _test_stash,
     "about": "Adult scenes, shown only in the PIN-locked private section.", "fields": [URL("Stash", "http://host:9999"), KEY("Stash → Settings → Security → API key."), BROWSER]},
    # ── other ──
    {"key": "email", "label": "Email (for invitations)", "category": "Other", "test": _test_email,
     "about": "Lets Omnarr email sign-up invitations. Any SMTP server works; for Gmail use smtp.gmail.com, port 587 and an app password.",
     "fields": [{"key": "host", "label": "SMTP server", "type": "text", "required": True, "placeholder": "smtp.gmail.com"},
                {"key": "port", "label": "Port", "type": "text", "required": True, "placeholder": "587"},
                {"key": "security", "label": "Security (starttls, ssl or none)", "type": "text", "required": False, "placeholder": "starttls"},
                {"key": "username", "label": "Username", "type": "text", "required": False, "placeholder": "you@gmail.com"},
                {"key": "password", "label": "Password / app password", "type": "secret", "required": False},
                {"key": "from_address", "label": "Send as", "type": "text", "required": False, "placeholder": "Omnarr <you@gmail.com>"}]},
    {"key": "plex", "label": "Plex", "category": "Movies & TV", "test": _test_plex,
     "about": "Movies and shows from Plex: watched state, artwork, and in-browser playback through Omnarr.",
     "fields": [URL("Plex", "http://host:32400"),
                {"key": "api_key", "label": "Plex token (optional)", "type": "secret", "required": False,
                 "help": "Plex Web → any item → ⋯ → Get Info → View XML: the X-Plex-Token in the address. "
                         "Not needed if Omnarr's address is in Plex's \"allowed without auth\" networks."},
                {"key": "adult_libraries", "label": "Adult libraries (hidden behind the PIN)", "type": "list", "required": False},
                BROWSER]},
    {"key": "readmeabook", "label": "ReadMeABook (audiobook requests)", "category": "Books", "test": _test_readmeabook,
     "about": "Send audiobook requests to ReadMeABook instead of Shelfmark: it finds the Audible match, downloads and "
              "imports it. Use an admin's token, so Omnarr's own approvals are the only gate.",
     "fields": [URL("ReadMeABook", "http://host:3030"),
                {"key": "api_key", "label": "API token", "type": "secret", "required": True,
                 "help": "In ReadMeABook: Profile → API Tokens → create (starts with rmab_)."},
                {"key": "audiobooks", "label": "Send audiobook requests here (yes/no)", "type": "text", "required": False,
                 "placeholder": "yes", "help": "\"no\" keeps it connected but leaves audiobook requests with Shelfmark."},
                BROWSER]},
    {"key": "custom_library", "label": "Custom library (JSON)", "category": "Your own apps", "test": _test_custom_library,
     "about": "Add any app Omnarr doesn't support: point it at a URL that returns your items as JSON "
              "(a script, n8n, or an app's API behind a small adapter). Format: docs/extending.md.",
     "fields": [{"key": "url", "label": "Items URL", "type": "url", "required": True, "placeholder": "http://host:8080/omnarr-items.json"},
                {"key": "header_name", "label": "Header name (optional)", "type": "text", "required": False, "placeholder": "Authorization"},
                {"key": "header_value", "label": "Header value (optional)", "type": "secret", "required": False,
                 "help": "Sent with every request to that URL, e.g. Bearer <token>."}]},
    {"key": "custom_requests", "label": "Custom requests (webhook)", "category": "Your own apps", "test": _test_custom_requests,
     "about": "Send requests for the formats you pick to your own URL as JSON, instead of Seerr, Shelfmark or ROMarr. "
              "Payload: docs/extending.md.",
     "fields": [{"key": "url", "label": "Request URL", "type": "secret", "required": True},
                {"key": "formats", "label": "Formats to send here", "type": "list", "required": True,
                 "placeholder": "audiobook, comic", "help": "Any of: movie, tv, ebook, audiobook, comic, game"},
                {"key": "header_name", "label": "Header name (optional)", "type": "text", "required": False},
                {"key": "header_value", "label": "Header value (optional)", "type": "secret", "required": False}]},
    {"key": "notifications", "label": "Notifications (webhook)", "category": "Other", "test": _test_notify,
     "about": "Sends events (a request is ready, a request needs approval, someone joined, an upload arrived) to one URL: "
              "ntfy, a Discord webhook, a Home Assistant webhook, or anything that accepts JSON. Test sends a real message.",
     "fields": [{"key": "url", "label": "Webhook URL", "type": "secret", "required": True,
                 "help": "e.g. https://ntfy.sh/your-topic, a Discord channel webhook, or http://homeassistant:8123/api/webhook/<id>"},
                {"key": "format", "label": "Format (json, ntfy or discord)", "type": "text", "required": False, "placeholder": "json"},
                {"key": "events", "label": "Only these events (leave empty for all)", "type": "list", "required": False,
                 "help": "request_ready, approval_needed, request_decided, account_joined, upload"}]},
    {"key": "tailscale", "label": "Tailscale (private access)", "category": "Other", "test": _test_tailscale,
     "about": "Let people ask for private-network access from \"Use on your devices\". When you approve, Omnarr creates a "
              "single-use Tailscale invite that shares just this one machine with their own Tailscale account.",
     "fields": [{"key": "api_key", "label": "API access token", "type": "secret", "required": True,
                 "help": "Tailscale admin console → Settings → Keys → Generate access token (tskey-api-…). Tokens last up to "
                         "90 days; Test shows when this one runs out. Tailscale doesn't allow invites with OAuth-client tokens."},
                {"key": "device", "label": "Machine to share", "type": "text", "required": True, "placeholder": "e.g. my-server",
                 "help": "Its name as listed under Machines in the Tailscale admin console."},
                {"key": "tailnet", "label": "Tailnet (optional)", "type": "text", "required": False, "placeholder": "-",
                 "help": "Leave empty for the token's own tailnet."}]},
]
BY_KEY = {a["key"]: a for a in APPS}


def public_specs(connections):
    """Specs for the wizard, with current values (secrets masked)."""
    out = []
    for a in APPS:
        cur = connections.get(a["key"]) or {}
        vals = {}
        for f in a["fields"]:
            v = cur.get(f["key"])
            vals[f["key"]] = ("••••" + str(v)[-4:]) if (f["type"] == "secret" and v) else v
        out.append({k: v for k, v in a.items() if k != "test"} | {"connected": bool(cur), "enabled": cur.get("enabled", False),
                                                                     "values": vals, "mode": _mode(a["key"], cur)})
    return out


def _mode(app, cur):
    if app in ("abs", "komga", "stash") and cur and not cur.get("api_key") and cur.get("db"):
        return "database (legacy) — add an API key to switch"
    return ""


def merge_secret(app, new, old):
    """Keep stored secrets when the wizard sends back the masked placeholder."""
    spec = BY_KEY[app]
    out = dict(old or {})
    for f in spec["fields"]:
        if f["key"] not in new:
            continue
        v = new[f["key"]]
        if f["type"] == "secret" and isinstance(v, str) and v.startswith("••••"):
            continue
        if f["type"] == "list" and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        if v in ("", None):
            out.pop(f["key"], None)
        else:
            out[f["key"]] = v
    return out
