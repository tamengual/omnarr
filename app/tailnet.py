"""Private access through Tailscale: share this server's machine with someone you approve.

Uses a Tailscale API access token (Settings → Keys → Generate access token). Device invites can't
be created with OAuth-client tokens, only with a person's own token, and those last at most 90 days.
An invite shares ONE machine with the person's own Tailscale account; they get no other access.
"""
import re
from datetime import datetime, timezone

import httpx

API = "https://api.tailscale.com/api/v2"


def _client(s, timeout=20):
    return httpx.Client(base_url=API, timeout=timeout, auth=(s["api_key"], ""))


def enabled(s):
    return bool(s and s.get("api_key") and s.get("device"))


def _tailnet(s):
    return (s.get("tailnet") or "-").strip() or "-"


def find_device(s):
    """The device to share: matched on its machine name or hostname (case-insensitive)."""
    want = s["device"].strip().lower()
    with _client(s) as c:
        r = c.get(f"/tailnet/{_tailnet(s)}/devices")
        r.raise_for_status()
    for d in r.json().get("devices", []):
        names = {(d.get("hostname") or "").lower(), (d.get("name") or "").lower(), (d.get("name") or "").split(".")[0].lower()}
        if want in names:
            return {"id": d.get("nodeId") or d.get("id"), "name": (d.get("name") or d.get("hostname") or "").split(".")[0]}
    raise LookupError(f"No machine called {s['device']} in this tailnet")


def key_expires(s):
    """When the API token expires (UTC datetime), or None if Tailscale doesn't say."""
    m = re.match(r"tskey-api-([A-Za-z0-9]+)-", s["api_key"].strip())
    if not m:
        return None
    with _client(s) as c:
        r = c.get(f"/tailnet/{_tailnet(s)}/keys/{m.group(1)}")
    if r.status_code != 200 or not r.json().get("expires"):
        return None
    return datetime.fromisoformat(r.json()["expires"].replace("Z", "+00:00"))


def create_invite(s, email=None):
    """A single-use invite to the shared machine. Returns {"id", "url"}."""
    dev = find_device(s)
    body = [{"multiUse": False, "allowExitNode": False, **({"email": email} if email else {})}]
    with _client(s) as c:
        r = c.post(f"/device/{dev['id']}/device-invites", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Tailscale refused the invite ({r.status_code}): {r.text[:200]}")
    inv = r.json()[0]
    return {"id": str(inv["id"]), "url": inv.get("inviteUrl") or ""}


def invite_status(s, invite_id):
    """{"accepted": bool, "by": login name or ""} — or None if the invite no longer exists."""
    with _client(s) as c:
        r = c.get(f"/device-invites/{invite_id}")
    if r.status_code == 404 or (r.status_code == 400 and "invalid invite" in r.text):
        return None                                  # deleted (Tailscale answers 400 "invalid invite") or never existed
    r.raise_for_status()
    d = r.json()
    return {"accepted": bool(d.get("accepted")), "by": ((d.get("acceptedBy") or {}).get("loginName") or "")}


def delete_invite(s, invite_id):
    with _client(s) as c:
        r = c.delete(f"/device-invites/{invite_id}")
    return r.status_code in (200, 204, 404) or (r.status_code == 400 and "invalid invite" in r.text)


def test(s):
    try:
        dev = find_device(s)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        return False, ("Tailscale didn't accept the token" if code in (401, 403) else f"Tailscale answered {code}")
    except LookupError as e:
        return False, str(e)
    except httpx.HTTPError as e:
        return False, f"Couldn't reach Tailscale: {e}"
    msg = f"Found {dev['name']}"
    try:
        exp = key_expires(s)
        if exp:
            days = (exp - datetime.now(timezone.utc)).days
            msg += f" · the token expires in {days} days ({exp:%b %d})"
    except httpx.HTTPError:
        pass
    return True, msg
