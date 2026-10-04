"""Per-person progress ("Continue", In progress / Finished, recently active).

Built by the indexer into index.db `user_progress(account_id, work_id, progress, status,
last_activity)`, one row per account per work they've touched. Each account's progress comes
from its own identities (see identity.py):
  - Jellyfin: the account's own Jellyfin user; admins without one use the configured user.
  - Audiobookshelf: the account's own API key; admins without one use the configured key.
  - Calibre "date read", Storyteller, Komga, RomM and Stash progress belong to the server's
    owner, so only admins see them.
Members who haven't linked anything simply have no progress (never the owner's).
"""
import json
import logging
import sqlite3
import time

import httpx

log = logging.getLogger("omnarr.progress")
OWNER_ONLY = {"calibre", "storyteller", "komga", "romm", "stash"}
OWNER = "__owner__"


def _jf_progress(cfg, user_name):
    """{jellyfin item id: (progress, finished, last_played)} for one Jellyfin user, or None."""
    s = cfg.source("jellyfin")
    if not s:
        return None
    with httpx.Client(base_url=s["url"].rstrip("/"), timeout=60,
                      headers={"Authorization": f'MediaBrowser Token="{s["api_key"]}"'}) as c:
        users = c.get("/Users").json()
        user = next((u for u in users if u["Name"].lower() == user_name.lower()), None)
        if not user:
            return None
        out = {}
        for v in c.get(f"/Users/{user['Id']}/Views").json().get("Items", []):
            if v.get("CollectionType") not in (None, "movies", "tvshows", "mixed", "homevideos"):
                continue
            items = c.get(f"/Users/{user['Id']}/Items", params={
                "ParentId": v["Id"], "Recursive": "true", "IncludeItemTypes": "Movie,Series",
                "Fields": "RecursiveItemCount", "EnableUserData": "true", "EnableImages": "false"}).json().get("Items", [])
            for it in items:
                ud = it.get("UserData") or {}
                pct = None
                if it["Type"] == "Movie":
                    if ud.get("PlayedPercentage") is not None:
                        pct = ud["PlayedPercentage"] / 100.0
                else:
                    total, unplayed = it.get("RecursiveItemCount") or 0, ud.get("UnplayedItemCount")
                    if total and unplayed is not None:
                        pct = (total - unplayed) / total
                out[it["Id"]] = (pct, bool(ud.get("Played")), (ud.get("LastPlayedDate") or "")[:19])
        return out


def _abs_progress(cfg, api_key):
    """{ABS library item id: (progress, finished, last_update)} for one ABS user, or None."""
    s = cfg.source("abs")
    if not s or not s.get("url"):
        return None
    with httpx.Client(base_url=s["url"].rstrip("/"), timeout=30, headers={"Authorization": f"Bearer {api_key}"}) as c:
        r = c.get("/api/me")
        if r.status_code != 200:
            return None
        out = {}
        for p in r.json().get("mediaProgress") or []:
            if not p.get("libraryItemId"):
                continue
            pct = min(1.0, (p.get("currentTime") or 0) / p["duration"]) if p.get("duration") else None
            out[p["libraryItemId"]] = (pct, bool(p.get("isFinished")), str(p.get("lastUpdate") or ""))
        return out


def _accounts(state_path):
    try:
        con = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute("SELECT id, role, jellyfin_user, abs_api_key FROM accounts")]
        finally:
            con.close()
    except sqlite3.Error:
        return []                                 # accounts not created yet (fresh install)


def build(cfg, state_path, editions):
    """editions: [(work_id, unit, how)] from the indexer. Returns user_progress rows."""
    accounts = _accounts(state_path)
    if not accounts:
        return []
    jf_cache, abs_cache = {}, {}
    from . import playstate
    native = {}                                   # account -> {(source, id): (pct, finished, last)}
    episodes = {}                                 # account -> {series id: [finished count, any started, last]}
    for r in playstate.all_rows(state_path):
        pct = min(1.0, r["position"] / r["duration"]) if r["duration"] else None
        last = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r["updated"] or 0))
        native.setdefault(r["account_id"], {})[(r["source"], r["item_id"])] = (pct, bool(r["finished"]), last)
        if r["parent_id"]:
            e = episodes.setdefault(r["account_id"], {}).setdefault(r["parent_id"], [0, False, ""])
            e[0] += int(bool(r["finished"]))
            e[1] = e[1] or (r["position"] or 0) > 0
            e[2] = max(e[2], last)
    rows = []
    for a in accounts:
        admin = a["role"] == "admin"
        jf_who = a["jellyfin_user"] or (OWNER if admin else None)
        abs_who = a["abs_api_key"] or (OWNER if admin else None)
        if jf_who not in (None, OWNER) and jf_who not in jf_cache:
            try:
                jf_cache[jf_who] = _jf_progress(cfg, jf_who) or {}
            except Exception as e:
                log.warning("jellyfin progress for %s failed: %s", jf_who, e)
                jf_cache[jf_who] = {}
        if abs_who not in (None, OWNER) and abs_who not in abs_cache:
            try:
                abs_cache[abs_who] = _abs_progress(cfg, abs_who) or {}
            except Exception as e:
                log.warning("abs progress failed for account %s: %s", a["id"], e)
                abs_cache[abs_who] = {}
        per_work = {}
        for wid, u, _how in editions:
            if u.source == "jellyfin":
                who = jf_who
                got = (u.progress, u.finished, "") if who == OWNER else (jf_cache.get(who, {}).get(u.source_id) if who else None)
            elif u.source == "abs":
                who = abs_who
                got = ((u.progress, u.finished, u.extra.get("last_listened", "")) if who == OWNER
                       else (abs_cache.get(who, {}).get(u.source_id) if who else None))
            elif u.source in OWNER_ONLY:
                got = (u.progress, u.finished, u.extra.get("last_listened", "")) if admin else None
            else:
                got = None
            mine = native.get(a["id"], {}).get((u.source, u.source_id))
            if mine is None and u.source == "jellyfin" and u.kind == "show":
                ep = episodes.get(a["id"], {}).get(u.source_id)
                total = (u.extra or {}).get("episodes") or 0
                if ep:
                    mine = ((ep[0] / total) if total else (0.01 if ep[1] else None), bool(total and ep[0] >= total), ep[2])
            if mine:
                got = mine if not got else (max(got[0] or 0, mine[0] or 0) if (got[0] or mine[0]) is not None else None,
                                            bool(got[1] or mine[1]), max(got[2] or "", mine[2] or ""))
            if not got or (got[0] is None and not got[1]):
                continue
            cur = per_work.setdefault(wid, [None, False, ""])
            if got[0] is not None:
                cur[0] = max(cur[0] or 0, got[0])
            cur[1] = cur[1] or got[1]
            cur[2] = max(cur[2], got[2] or "")
        for wid, (pct, fin, last) in per_work.items():
            status = "finished" if fin else ("in_progress" if (pct or 0) > 0.005 else "unread")
            rows.append((a["id"], wid, pct, status, last))
    return rows
