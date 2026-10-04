"""Notifications (all optional).

Two kinds of delivery:
  * a webhook (the "notifications" connection): one URL that gets every event. Formats:
      json    - {"event", "title", "message", "url"} (Home Assistant webhooks, n8n, anything)
      ntfy    - ntfy.sh / self-hosted ntfy topic URL (title in a header, message as the body)
      discord - a Discord channel webhook
  * email to the person concerned (needs the "email" connection, an address on their account,
    and "Email me" left on in My account). Admins get an email when something needs approval.

Events: request_ready, approval_needed, request_decided, account_joined, upload.
Sending never blocks or breaks the action that triggered it.
"""
import logging
import sqlite3
import threading

import httpx

log = logging.getLogger("omnarr.notify")
EVENTS = ("request_ready", "approval_needed", "request_decided", "account_joined", "upload")


def _webhook(s, event, title, message, url):
    fmt = (s.get("format") or "json").lower()
    target = s["url"].strip()
    with httpx.Client(timeout=15) as c:
        if fmt == "ntfy":
            r = c.post(target, content=message.encode(), headers={"Title": title.encode("ascii", "replace").decode(),
                                                                   "Tags": "books", **({"Click": url} if url else {})})
        elif fmt == "discord":
            r = c.post(target, json={"content": f"**{title}**\n{message}" + (f"\n{url}" if url else "")})
        else:
            r = c.post(target, json={"event": event, "title": title, "message": message, "url": url})
    r.raise_for_status()


def test(s):
    """Registry test: send a real test message to the webhook."""
    try:
        _webhook(s, "test", "Omnarr test", "Notifications from Omnarr are working.", "")
    except httpx.HTTPStatusError as e:
        return False, f"The webhook answered {e.response.status_code}"
    return True, "Test notification sent"


def _emails_for(state_path, account_ids):
    if not account_ids:
        return []
    con = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        q = f"SELECT email FROM accounts WHERE id IN ({','.join('?' * len(account_ids))}) AND email IS NOT NULL AND notify_email=1"
        return [r[0] for r in con.execute(q, list(account_ids))]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _admin_ids(state_path):
    con = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT id FROM accounts WHERE role='admin'")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def send(cfg, state_path, event, title, message, to=(), to_admins=False, url=""):
    """Fire-and-forget: webhook (if set up and the event is enabled) + email to `to` accounts."""
    if cfg is None or not state_path:
        return

    def run():
        try:
            deliver()
        except Exception as e:  # a notification must never take anything else down
            log.warning("notification failed: %s", e)

    def deliver():
        hook = cfg.source("notifications")
        if hook and hook.get("url"):
            wanted = hook.get("events") or []
            if not wanted or event in wanted:
                try:
                    _webhook(hook, event, title, message, url)
                except Exception as e:
                    log.warning("webhook notification failed: %s", e)
        mail = cfg.source("email")
        ids = list(to) + (_admin_ids(state_path) if to_admins else [])
        if mail and ids:
            from . import mailer
            for addr in set(_emails_for(state_path, ids)):
                try:
                    mailer.send(mail, addr, title, message + (f"\n\n{url}" if url else ""))
                except Exception as e:
                    log.warning("email notification to an account failed: %s", e)
    threading.Thread(target=run, daemon=True, name="notify").start()
