"""Stash: adult scenes. Every Unit is adult=True, so the API never returns them unless
the session has been unlocked with the PIN.

API mode (default): Stash's GraphQL API with its API key (ApiKey header).
Database mode (legacy): reads stash-go.sqlite through a read-only mount.
"""
import os
import re

import httpx

from .base import Unit, ro_connect, year_of

SCENES_QUERY = """query($page:Int!){ findScenes(filter:{per_page:500, page:$page, sort:"id"}){ count scenes {
  id title details date rating100 created_at resume_time
  studio { name } performers { name } tags { name }
  files { basename duration height } paths { screenshot } } } }"""


def api_mode(s):
    return bool(s and s.get("api_key") and s.get("url"))


def client(s, timeout=60):
    return httpx.Client(base_url=s["url"].rstrip("/"), timeout=timeout, headers={"ApiKey": s["api_key"]})


def _title(t, basename):
    if t:
        return t
    s = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", basename or "")
    return re.sub(r"[._]+", " ", s).strip() or "Untitled scene"


def read(cfg):
    s = cfg.source("stash")
    if not s:
        return []
    return _read_api(cfg, s) if api_mode(s) else _read_db(cfg, s)


def _read_api(cfg, s):
    base = cfg.link("stash")
    units, page = [], 1
    with client(s) as c:
        while True:
            r = c.post("/graphql", json={"query": SCENES_QUERY, "variables": {"page": page}})
            r.raise_for_status()
            d = r.json()["data"]["findScenes"]
            for sc in d["scenes"]:
                f = (sc.get("files") or [{}])[0]
                dur = f.get("duration")
                studio = (sc.get("studio") or {}).get("name") or ""
                units.append(Unit(
                    source="stash", source_id=str(sc["id"]), kind="scene", format="scene",
                    title=_title(sc.get("title"), f.get("basename")),
                    authors=[p["name"] for p in sc.get("performers") or []],
                    year=year_of(sc.get("date")), description=(sc.get("details") or "").strip(),
                    genres=[t["name"] for t in sc.get("tags") or []],
                    url=f"{base}/scenes/{sc['id']}" if base else "",
                    cover=f"stash:{sc['id']}" if (sc.get("paths") or {}).get("screenshot") else "",
                    duration=dur, added=str(sc.get("created_at") or "")[:10],
                    progress=(sc["resume_time"] / dur) if (dur and sc.get("resume_time")) else None,
                    adult=True, library=studio or "Stash",
                    rating=(sc["rating100"] / 20.0) if sc.get("rating100") else None,
                    extra={"studio": studio, "resolution": f"{f['height']}p" if f.get("height") else ""},
                ))
            if page * 500 >= d["count"] or not d["scenes"]:
                break
            page += 1
    return units


def cover_bytes(cfg, scene_id):
    s = cfg.source("stash") or {}
    if not api_mode(s):
        return None
    with client(s, timeout=30) as c:
        r = c.get(f"/scene/{int(scene_id)}/screenshot")
    return r.content if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") else None


def _read_db(cfg, s):
    con = ro_connect(s["db"])
    base = cfg.link("stash")
    performers, tags = {}, {}
    for r in con.execute("SELECT ps.scene_id, p.name FROM performers_scenes ps JOIN performers p ON p.id=ps.performer_id"):
        performers.setdefault(r["scene_id"], []).append(r["name"])
    for r in con.execute("SELECT st.scene_id, t.name FROM scenes_tags st JOIN tags t ON t.id=st.tag_id"):
        tags.setdefault(r["scene_id"], []).append(r["name"])
    units = []
    for r in con.execute("""SELECT s.id, s.title, s.details, s.date, s.rating, s.created_at, s.cover_blob, s.resume_time,
                                   st.name AS studio, f.basename, vf.duration, vf.width, vf.height
                            FROM scenes s LEFT JOIN studios st ON st.id=s.studio_id
                            LEFT JOIN scenes_files sf ON sf.scene_id=s.id AND sf."primary"=1
                            LEFT JOIN files f ON f.id=sf.file_id
                            LEFT JOIN video_files vf ON vf.file_id=f.id"""):
        dur = r["duration"]
        units.append(Unit(
            source="stash", source_id=str(r["id"]), kind="scene", format="scene",
            title=_title(r["title"], r["basename"]), authors=performers.get(r["id"], []),
            year=year_of(r["date"]), description=(r["details"] or "").strip(),
            genres=tags.get(r["id"], []),
            url=f"{base}/scenes/{r['id']}" if base else "",
            cover=f"stash:{r['id']}" if r["cover_blob"] else "",
            duration=dur, added=str(r["created_at"] or "")[:10],
            progress=(r["resume_time"] / dur) if (dur and r["resume_time"]) else None,
            adult=True, library=r["studio"] or "Stash",
            rating=(r["rating"] / 20.0) if r["rating"] else None,      # Stash rating100 -> 0..5
            extra={"studio": r["studio"] or "", "resolution": f"{r['height']}p" if r["height"] else ""},
        ))
    con.close()
    return units


def cover_file(cfg, scene_id):
    """Stash keeps covers as content-addressed blobs: <blobs>/<aa>/<bb>/<checksum>."""
    s = cfg.source("stash") or {}
    if not s.get("db"):
        return None
    con = ro_connect(s["db"])
    try:
        r = con.execute("SELECT cover_blob FROM scenes WHERE id=?", (int(scene_id),)).fetchone()
    finally:
        con.close()
    if not r or not r["cover_blob"]:
        return None
    c = r["cover_blob"]
    for root in s.get("blobs") or []:
        for p in (os.path.join(root, c[:2], c[2:4], c), os.path.join(root, c)):
            if os.path.exists(p):
                return p
    return None
