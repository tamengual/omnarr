"""Tidy finished Private downloads: give each one its real details in Stash and a proper file name.

For every download Omnarr sent (adult_grabs) that qBittorrent has finished:
  1. Work out what it is, best source first:
       - Stash already matched it on StashDB (by video fingerprint): use Stash's own details.
       - The StashDB scene it was downloaded for (picked in Find, or a followed studio's new scene).
       - A StashDB match on the studio and title read from the release name.
       - The release name itself: "[Site.com] Title (Performer, Performer) [2025, Tag, Tag, 1080p]"
         or scene-style "Studio.26.09.13.Performer.Name.XXX.1080p".
  2. Fill in Stash: title, date, studio, performers (created as needed) and tags.
  3. Rename the file through qBittorrent, so it keeps seeding: "Studio - 2025-06-09 - Title (Performers).mp4".
  4. Have Stash rescan (it follows the renamed file by fingerprint), then Omnarr re-indexes.
"""
import difflib
import json
import logging
import os
import re
import time
from contextlib import contextmanager

import httpx

from . import adultfind as af

log = logging.getLogger("omnarr.adulttidy")
VIDEO = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".wmv", ".webm", ".ts", ".flv")
NOT_TAGS = re.compile(r"^(\d{3,4}p|4k|uhd|hd|sd|hevc|x26[45]|h\.?26[45]|web-?dl|webrip|mp4|mkv|vod|siterip|\d{4}|\.|\?+)$", re.I)


# ── reading a release name ───────────────────────────────────────────────────
def _clean(s):
    s = re.sub(r"[Ѐ-ӿ]+", " ", s or "")               # Cyrillic left over from PornoLab titles
    s = re.sub(r"\b(2160|1080|720|480)p\b|\b4k\b", " ", s, flags=re.I)
    s = re.sub(r"\s*/\s*$", "", re.sub(r"\s+", " ", s)).strip(" -–—/|.,")
    return s.strip()


def _split_names(s):
    """'James Ash (james ashxxx), James Cassidy & Bryce Jax, ???' -> ['James Ash', 'James Cassidy', 'Bryce Jax']."""
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    names = []
    for part in out:
        part = re.sub(r"\([^)]*\)", "", part)
        for name in re.split(r"\s+&\s+|\s+and\s+(?=[A-Z])", part):
            name = _clean(name)
            if name and not re.fullmatch(r"\?+|others?|and others|etc\.?|\.+", name, re.I) and len(name) <= 40:
                names.append(name)
    return list(dict.fromkeys(names))


def _studio_from_site(site):
    site = re.split(r"\s*/\s*", site.strip())[0]
    site = re.sub(r"\.(com|net|tv|xxx|org)$", "", site.strip(), flags=re.I)
    return site.strip()


def parse_release(name):
    """{studio, title, performers, date, tags} read from a release name; missing parts are empty."""
    t = re.sub(r"\.(mp4|mkv|m4v|mov|avi|wmv)$", "", (name or "").strip(), flags=re.I)
    out = {"studio": "", "title": "", "performers": [], "date": "", "tags": []}
    m = re.match(r"^\[([^\]]+)\]\s*(.*)$", t)
    if m and re.search(r"\.[a-z]{2,4}\b", m.group(1), re.I):               # [Site.com / Other.com] ...
        out["studio"], t = _studio_from_site(m.group(1)), m.group(2)
        info = re.search(r"\[([^\[\]]*)\]\s*$", t)
        if info:
            t = t[:info.start()]
            items = [i.strip() for i in info.group(1).split(",")]
            for i in items:
                d = re.search(r"(\d{4})-(\d{2})-(\d{2})", i)
                if d and not out["date"]:
                    out["date"] = d.group(0)
                elif re.fullmatch(r"\d{4}\s*\.?", i) and not out["date"]:
                    out["date"] = i[:4]
            out["tags"] = [_clean(i) for i in items if _clean(i) and not NOT_TAGS.match(_clean(i))
                           and not re.search(r"\d{4}", i)][:15]
        t = t.strip()
        if t.endswith(")"):                                            # Title (Performer, Performer)
            depth, i = 0, len(t) - 1
            while i >= 0:
                depth += t[i] == ")"
                depth -= t[i] == "("
                if depth == 0:
                    break
                i -= 1
            out["performers"], t = _split_names(t[i + 1:-1]), t[:i]
        elif " - " in t:                                               # Performer, Performer - Title
            left, right = t.split(" - ", 1)
            if len(left.split()) <= 12:
                out["performers"], t = _split_names(left), right
        elif "," in t or " & " in t:                                   # just the performers' names
            out["performers"], t = _split_names(t), ""
        out["title"] = _clean(t) or ", ".join(out["performers"])
        return out
    m = re.match(r"^([A-Za-z0-9]+)[ .](\d{2})[ .](\d{2})[ .](\d{2})[ .](.+?)(?:[ .]XXX\b.*)?$", t)   # Studio.26.09.13.Names.XXX...
    if m:
        out["studio"], out["date"] = m.group(1), f"20{m.group(2)}-{m.group(3)}-{m.group(4)}"
        rest = re.sub(r"[._]+", " ", m.group(5))
        rest = re.sub(r"\b(1080p|720p|2160p|4k|mp4|web|x264|x265|hevc)\b.*$", "", rest, flags=re.I).strip()
        out["title"] = rest
        out["performers"] = _split_names(re.sub(r"\s+And\s+", ", ", rest))[:4] if len(rest.split()) <= 8 else []
        return out
    if " - " in t:                                                     # Studio - Title / Names - Title / Title - Names
        d = re.search(r"\d{4}-\d{2}-\d{2}", t)
        out["date"] = d.group(0) if d else ""
        t = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)\s*$", "", t)
        left, right = t.split(" - ", 1)
        if "," in left or " & " in left:
            out["performers"], out["title"] = _split_names(left), _clean(right)
        elif "," in right or " & " in right:
            out["performers"], out["title"] = _split_names(right), _clean(left)
        else:
            out["studio"], out["title"] = _clean(left), _clean(right)
        return out
    out["title"] = _clean(re.sub(r"[._]+", " ", t))
    if "," in out["title"]:
        out["performers"] = _split_names(out["title"])
    return out


def file_name(meta, ext):
    """'Studio - 2025-06-09 - Title (A, B).mp4', safe on every filesystem, at most ~180 characters."""
    names = meta.get("performers") or []
    who = ", ".join(names[:4]) + (" & more" if len(names) > 4 else "")
    title = meta.get("title") or ""
    if who and title and all(n.lower() in title.lower() for n in names[:2]):
        who = ""                                                     # the title already says who
    parts = [meta.get("studio"), meta.get("date"), title or who]
    base = " - ".join(p for p in parts if p)
    if who and title:
        base += f" ({who})"
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", base)
    base = re.sub(r"\s+", " ", base).strip(" .")[:180 - len(ext)]
    return (base or "Untitled") + ext


# ── qBittorrent ─────────────────────────────────────────────────────────────
@contextmanager
def qbit(cfg):
    s = cfg.source("qbittorrent") or {}
    with httpx.Client(base_url=s["url"].rstrip("/") + "/api/v2", timeout=30, headers={"Referer": s["url"]}) as c:
        if s.get("username") or s.get("password"):
            r = c.post("/auth/login", data={"username": s.get("username") or "", "password": s.get("password") or ""})
            if not (r.status_code == 204 or (r.status_code == 200 and r.text.strip() == "Ok.")):
                raise RuntimeError("qBittorrent didn't accept the login in Connections")
        yield c


def _video_file(c, h):
    files = c.get("/torrents/files", params={"hash": h}).json()
    vids = [f for f in files if f["name"].lower().endswith(VIDEO)]
    return max(vids, key=lambda f: f.get("size") or 0) if vids else None


# ── Stash ───────────────────────────────────────────────────────────────────
def _gql(c, query, variables=None):
    r = c.post("/graphql", json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"][0].get("message", "Stash error"))
    return body["data"]


def stash_scene(c, basename):
    d = _gql(c, """query($p:String!){ findScenes(scene_filter:{path:{value:$p, modifier:INCLUDES}}, filter:{per_page:5}) {
        scenes { id title date organized studio { name } performers { name } stash_ids { stash_id } files { path } } } }""",
             {"p": '"' + basename.replace('"', "") + '"'})          # quoted: unquoted INCLUDES matches any of the words
    for s in d["findScenes"]["scenes"]:
        if any(os.path.basename(f["path"]) == basename for f in s["files"]):
            return s
    return None


def _find_or_create(c, kind, name, extra=None):
    """Id of the Stash studio/performer/tag called name (case-insensitive), created when missing."""
    q = {"studio": ("findStudios", "studio_filter", "studios", "studioCreate"),
         "performer": ("findPerformers", "performer_filter", "performers", "performerCreate"),
         "tag": ("findTags", "tag_filter", "tags", "tagCreate")}[kind]
    d = _gql(c, f"""query($n:String!){{ {q[0]}({q[1]}:{{name:{{value:$n, modifier:EQUALS}}}}) {{ {q[2]} {{ id name }} }} }}""", {"n": name})
    found = d[q[0]][q[2]]
    if found:
        return found[0]["id"]
    in_type = {"studio": "StudioCreateInput", "performer": "PerformerCreateInput", "tag": "TagCreateInput"}[kind]
    d = _gql(c, f"mutation($i:{in_type}!){{ {q[3]}(input:$i) {{ id }} }}", {"i": {"name": name, **(extra or {})}})
    return d[q[3]]["id"]


def apply(c, scene_id, meta, endpoint=None, men=False):
    """Write what we know into Stash."""
    inp = {"id": scene_id}
    if meta.get("title"):
        inp["title"] = meta["title"]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta.get("date") or ""):
        inp["date"] = meta["date"]
    elif re.fullmatch(r"\d{4}", meta.get("date") or ""):
        inp["date"] = f"{meta['date']}-01-01"
    if meta.get("studio"):
        inp["studio_id"] = _find_or_create(c, "studio", meta["studio"])
    if meta.get("performers"):
        inp["performer_ids"] = [_find_or_create(c, "performer", p, {"gender": "MALE"} if men else None) for p in meta["performers"][:12]]
    if meta.get("tags"):
        inp["tag_ids"] = [_find_or_create(c, "tag", t) for t in meta["tags"][:15]]
    if meta.get("url"):
        inp["urls"] = [meta["url"]]
    if meta.get("details"):
        inp["details"] = meta["details"]
    if meta.get("stashdb_id") and endpoint:
        inp["stash_ids"] = [{"endpoint": endpoint, "stash_id": meta["stashdb_id"]}]
    _gql(c, "mutation($i:SceneUpdateInput!){ sceneUpdate(input:$i) { id } }", {"i": inp})


def _similar(a, b):
    return difflib.SequenceMatcher(None, af.norm(a), af.norm(b)).ratio()


def _match_score(torrent, grab):
    """How well a torrent fits a grab: its name against the full release title or just studio + title
    (long tag lists drown the real name), plus a little for starting within 15 minutes of the grab."""
    p = parse_release(grab["title"])
    core = " ".join(x for x in (p["studio"], p["title"]) if x)
    s = max(_similar(torrent["name"], grab["title"]), _similar(torrent["name"], core) if core else 0)
    return s + (0.2 if abs(torrent.get("added_on", 0) - grab["at"]) < 900 else 0)


def work_out(cfg, grab_row, scene_now):
    """(meta, how) for one finished download."""
    if scene_now and scene_now.get("stash_ids"):                       # Stash matched it by fingerprint
        return ({"studio": (scene_now.get("studio") or {}).get("name") or "", "title": scene_now.get("title") or "",
                 "date": scene_now.get("date") or "", "performers": [p["name"] for p in scene_now.get("performers") or []]},
                "StashDB (fingerprint)")
    if grab_row and grab_row["scene"]:
        s = json.loads(grab_row["scene"])
        return ({"studio": s.get("studio") or "", "title": s.get("title") or "", "date": s.get("date") or "",
                 "performers": s.get("performers") or [], "stashdb_id": s.get("id") or "", "url": s.get("url") or ""},
                "the StashDB scene it was downloaded for")
    parsed = parse_release(grab_row["title"] if grab_row else "")
    if parsed["studio"]:
        try:                                                           # use StashDB's proper names when it knows the scene
            hints = af.studio_hints(cfg, parsed["studio"])
            if hints:
                parsed["studio"] = next(iter(hints))
            if parsed["title"]:
                for s in af.understand(cfg, f"{parsed['studio']} {parsed['title']}", limit=5):
                    same_studio = af.compact(s["studio"]) == af.compact(parsed["studio"]) or s["studio"] in hints
                    if same_studio and _similar(s["title"], parsed["title"]) >= 0.8:
                        return ({**parsed, "title": s["title"], "date": s["date"] or parsed["date"],
                                 "performers": s["performers"] or parsed["performers"], "stashdb_id": s["id"],
                                 "url": s["url"]}, "a StashDB match on studio and title")
        except Exception as e:
            log.info("stashdb lookup for %s failed: %s", parsed["title"], e)
    return parsed, "the release name"


def tidy(cfg, state_path, reindex=None):
    """Tidy every finished, not-yet-tidied download. Returns [{file, name, how}] for what changed."""
    if not (cfg.source("qbittorrent") and cfg.source("stash")):
        return []
    category = (cfg.source("qbittorrent") or {}).get("category") or "stash"
    con = af.grabs_con(state_path)
    grabs = [dict(r) for r in con.execute("SELECT * FROM adult_grabs WHERE tidied IS NULL AND at > ?", (time.time() - 14 * 86400,))]
    if not grabs:
        con.close()
        return []
    done, men = [], af.prefs(state_path)["gay_only"]
    with qbit(cfg) as q, af._stash(cfg) as st:
        endpoint = af.stashbox_endpoint(st)
        torrents = q.get("/torrents/info", params={"category": category}).json()
        claimed = {g["hash"] for g in grabs if g["hash"]}
        for g in grabs:
            t = next((t for t in torrents if t["hash"] == g["hash"]), None) if g["hash"] else None
            if not t:                                                  # match by name, close to when it was sent
                # finished or not, so a grab whose own download is still running can't take a finished neighbour's
                near = [t for t in torrents if t["hash"] not in claimed and abs(t.get("added_on", 0) - g["at"]) < 6 * 3600]
                scored = sorted(((_match_score(t, g), t) for t in near), key=lambda x: -x[0])
                if not scored or scored[0][0] < 0.4:
                    continue
                t = scored[0][1]
                claimed.add(t["hash"])
            if t.get("progress", 0) < 1:                               # still downloading: next time
                continue
            f = _video_file(q, t["hash"])
            if not f:
                continue
            base = os.path.basename(f["name"])
            scene = stash_scene(st, base)
            if not scene:                                              # Stash hasn't scanned it yet: next time
                continue
            try:
                meta, how = work_out(cfg, g, scene)
                apply(st, scene["id"], meta, endpoint, men)
                new = file_name(meta, os.path.splitext(base)[1].lower() or ".mp4")
                if new != base:
                    new_path = f["name"][: -len(base)] + new
                    r = q.post("/torrents/renameFile", data={"hash": t["hash"], "oldPath": f["name"], "newPath": new_path})
                    if r.status_code >= 400:
                        raise RuntimeError(f"qBittorrent couldn't rename it ({r.status_code})")
                note = f"{new} (from {how})"
                done.append({"file": base, "name": new, "how": how})
            except Exception as e:
                log.warning("tidying %s failed: %s", base, e)
                note = f"failed: {e}"[:200]
            with con:
                con.execute("UPDATE adult_grabs SET hash=?, tidied=?, tidy_note=? WHERE guid=?",
                            (t["hash"], time.time(), note, g["guid"]))
        if done:
            path = (cfg.source("stash") or {}).get("downloads_path") or ""
            if path:
                time.sleep(3)                                          # let qBittorrent finish the rename
                _gql(st, "mutation($p:[String!]){ metadataScan(input:{paths:$p}) }", {"p": [path]})
    con.close()
    if done and reindex:
        reindex()
    return done
