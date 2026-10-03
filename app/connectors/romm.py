"""RomM (HTTP API with a client token, GET only). Games, per platform."""
import datetime

import httpx

from .base import Unit, year_of


def _client(s):
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=60,
                        headers={"Authorization": f"Bearer {s['api_key']}", "Accept": "application/json"})


def _release_year(v):
    """RomM gives first_release_date as epoch ms (IGDB) or an ISO string."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or str(v).isdigit():
        n = int(v)
        return datetime.datetime.fromtimestamp(n / 1000 if n > 10**11 else n, datetime.timezone.utc).year
    return year_of(v)


def _items(data):
    if isinstance(data, list):
        return data, None
    return data.get("items") or [], data.get("total")


def read(cfg):
    s = cfg.source("romm")
    if not s or not s.get("api_key"):
        return []
    base = cfg.link("romm")
    units, offset, page = [], 0, 250
    with _client(s) as c:
        while True:
            r = c.get("/api/roms", params={"limit": page, "offset": offset, "order_by": "name", "with_total": "true"})
            r.raise_for_status()
            items, total = _items(r.json())
            for g in items:
                # skip helper entries: "_cocoonart" artwork staging, "_…-needs-matching-rom" placeholders
                if str(g.get("name") or "").startswith("_") or str(g.get("platform_slug") or "").startswith("_"):
                    continue
                fs = str(g.get("fs_name") or "").lower()          # e.g. gamelist.xml.bak-20260915
                if ".xml" in fs or ".bak" in fs or fs.endswith((".txt", ".nfo", ".json")):
                    continue
                meta = g.get("metadatum") or {}
                ru = g.get("rom_user") or {}
                plat = g.get("platform_display_name") or g.get("platform_name") or g.get("platform_slug") or ""
                cover = g.get("path_cover_large") or g.get("path_cover_l") or g.get("path_cover_small") or ""
                rel = meta.get("first_release_date")
                units.append(Unit(
                    source="romm", source_id=str(g["id"]), kind="game", format="game",
                    title=g.get("name") or g.get("fs_name_no_tags") or g.get("fs_name") or "",
                    authors=meta.get("companies") or [],
                    year=_release_year(rel),
                    description=(g.get("summary") or "").strip(),
                    genres=meta.get("genres") or [],
                    url=f"{base}/rom/{g['id']}" if base else "",
                    cover=f"romm:{g['id']}" if cover else "",
                    added=str(g.get("created_at") or "")[:10],
                    finished=bool(ru.get("status") in ("finished", "completed_100")) if ru else False,
                    library=plat, rating=(meta.get("average_rating") / 20.0) if meta.get("average_rating") else None,
                    ids={k: str(g[k]) for k in ("igdb_id", "ss_id", "ra_id") if g.get(k)},
                    extra={"platform": plat, "platform_slug": g.get("platform_slug") or "",
                           "size": g.get("fs_size_bytes"), "cover_path": cover},
                ))
            offset += len(items)
            if not items or (total is not None and offset >= total) or (total is None and len(items) < page):
                break
    return units


def image(cfg, path):
    """Fetch a cover through RomM (server side, token never reaches the browser)."""
    s = cfg.source("romm")
    p = path if path.startswith("/") else "/" + path
    if not p.startswith("/assets/"):
        p = "/assets/romm/resources" + p
    with _client(s) as c:
        r = c.get(p)
        if r.status_code == 200:
            return r.content, r.headers.get("content-type", "image/jpeg")
    return None, None
