"""The "Your devices" help page: what people can use besides Omnarr in a browser.

Everything here is set by an admin and optional. A section that needs an outside app (Jellyfin
for TVs, Audiobookshelf for audiobook apps, Komga for comic apps, an OPDS catalog for e-reader
apps) only appears once its address is filled in, because whether those apps are reachable from
outside the home is different on every server.
"""
import json

KEY = "devices"
APPS = ("jellyfin", "abs", "komga", "opds")
TEXT = {"contact": 80, "login_note": 500, "network_note": 500}


def load(raw):
    try:
        conf = json.loads(raw or "{}")
    except ValueError:
        conf = {}
    return conf if isinstance(conf, dict) else {}


def clean(body):
    """Validate an admin's form; raises ValueError with a message for the person filling it in."""
    out = {}
    for key, limit in TEXT.items():
        value = str(body.get(key) or "").strip()
        if len(value) > limit:
            raise ValueError(f"Keep that note under {limit} characters")
        out[key] = value
    for app in APPS:
        url = str(body.get(f"{app}_url") or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("Addresses must start with https:// (or http://)")
        if any(c.isspace() for c in url):
            raise ValueError("Addresses can't contain spaces")
        out[f"{app}_url"] = url
        out[f"{app}_private"] = bool(body.get(f"{app}_private")) and bool(url)
    return out


def visible(conf):
    """What a signed-in person sees: only the filled-in parts."""
    out = {k: v for k, v in conf.items() if k in TEXT and v}
    out["apps"] = {app: {"url": conf[f"{app}_url"], "private": bool(conf.get(f"{app}_private"))}
                   for app in APPS if conf.get(f"{app}_url")}
    return out
