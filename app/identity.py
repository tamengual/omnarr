"""Whose app identity the current request acts as.

Set once per request from the signed-in account; read wherever Jellyfin/Audiobookshelf user
data is fetched or progress is written back. Values:
  OWNER - the server's configured identity (Settings -> Connections). Admins without their
          own mapping use it.
  None  - no identity: browse and play, but no personal progress is read or written. Members
          who haven't linked their own accounts get this, never the owner's.
  str   - this person's own Jellyfin user name / Audiobookshelf API key.
"""
import contextvars

OWNER = "__owner__"
jellyfin_user = contextvars.ContextVar("jellyfin_user", default=OWNER)
abs_key = contextvars.ContextVar("abs_key", default=OWNER)
account_id = contextvars.ContextVar("account_id", default=None)
is_admin = contextvars.ContextVar("is_admin", default=True)       # Plex: its token is the owner's


def set_for(account):
    account_id.set(account.get("id") if account else None)
    is_admin.set(bool(account) and account.get("role") == "admin")
    if not account:
        jellyfin_user.set(None)
        abs_key.set(None)
        return
    admin = account.get("role") == "admin"
    jellyfin_user.set(account.get("jellyfin_user") or (OWNER if admin else None))
    abs_key.set(account.get("abs_api_key") or (OWNER if admin else None))
