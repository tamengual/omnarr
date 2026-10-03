"""Jellyfin (HTTP API, GET only). Movies and series, per library, with watched state."""
import httpx

from .base import Unit

FIELDS = "ProviderIds,Genres,Overview,DateCreated,Studios,Tags,OfficialRating,CommunityRating,ChildCount,RecursiveItemCount"


def _client(s):
    # Jellyfin 12 dropped the legacy X-Emby-Token header; the Authorization scheme works on 10.x too.
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=60,
                        headers={"Authorization": f'MediaBrowser Token="{s["api_key"]}"', "Accept": "application/json"})


def read(cfg):
    s = cfg.source("jellyfin")
    if not s:
        return []
    base = cfg.link("jellyfin")
    adult_libs = {x.lower() for x in (s.get("adult_libraries") or [])}
    units = []
    with _client(s) as c:
        users = c.get("/Users").json()
        want = (s.get("user") or "").lower()
        user = next((u for u in users if u["Name"].lower() == want), users[0] if users else None)
        if not user:
            return []
        uid = user["Id"]
        views = c.get(f"/Users/{uid}/Views").json().get("Items", [])
        for v in views:
            if v.get("CollectionType") not in (None, "movies", "tvshows", "mixed", "homevideos"):
                continue
            items = c.get(f"/Users/{uid}/Items", params={
                "ParentId": v["Id"], "Recursive": "true", "IncludeItemTypes": "Movie,Series",
                "Fields": FIELDS, "EnableUserData": "true", "EnableImageTypes": "Primary",
            }).json().get("Items", [])
            for it in items:
                is_movie = it["Type"] == "Movie"
                ud = it.get("UserData") or {}
                pct = None
                if is_movie:
                    if ud.get("PlayedPercentage") is not None:
                        pct = ud["PlayedPercentage"] / 100.0
                else:                                  # series: share of episodes watched
                    total = it.get("RecursiveItemCount") or 0
                    unplayed = ud.get("UnplayedItemCount")
                    if total and unplayed is not None:
                        pct = (total - unplayed) / total
                pids = {k.lower(): str(val) for k, val in (it.get("ProviderIds") or {}).items() if val}
                units.append(Unit(
                    source="jellyfin", source_id=it["Id"],
                    kind="movie" if is_movie else "show", format="movie" if is_movie else "series",
                    title=it.get("Name") or "", year=it.get("ProductionYear"),
                    description=(it.get("Overview") or "").strip(),
                    genres=it.get("Genres") or [], tags=it.get("Tags") or [],
                    url=f"{base}/web/#/details?id={it['Id']}" if base else "",
                    cover=f"jellyfin:{it['Id']}" if (it.get("ImageTags") or {}).get("Primary") else "",
                    duration=(it["RunTimeTicks"] / 1e7) if it.get("RunTimeTicks") else None,
                    added=(it.get("DateCreated") or "")[:10],
                    progress=pct, finished=bool(ud.get("Played")),
                    library=v.get("Name") or "", rating=it.get("CommunityRating"),
                    adult=(v.get("Name") or "").lower() in adult_libs,
                    ids=pids,
                    extra={"studios": [x.get("Name") for x in it.get("Studios") or []],
                           "official_rating": it.get("OfficialRating") or "",
                           "episodes": it.get("RecursiveItemCount") if not is_movie else None},
                ))
    return units


def image(cfg, item_id, width=400):
    """Fetch a poster from Jellyfin (server side, so the browser never needs the key)."""
    s = cfg.source("jellyfin")
    with _client(s) as c:
        r = c.get(f"/Items/{item_id}/Images/Primary", params={"maxWidth": width, "quality": 85})
        if r.status_code == 200:
            return r.content, r.headers.get("content-type", "image/jpeg")
    return None, None
