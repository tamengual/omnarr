"""Plain-English Activity: why a download is where it is, when it should arrive, what's coming
up, and the problems Sonarr/Radarr/Prowlarr report about themselves.

Download explanations use what Sonarr/Radarr report for each queue entry and, when qBittorrent is
connected, qBittorrent's own view of the torrent (seeders, its place in line, stalled or not).
"""
import datetime
import logging
import re

import httpx

from .connectors import arr

log = logging.getLogger("omnarr.insight")


# ── when it should arrive ────────────────────────────────────────────────────
def _seconds(timeleft):
    """Sonarr/Radarr timeleft "1.02:03:04" / "02:03:04" -> seconds, or None."""
    m = re.match(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+)", str(timeleft or ""))
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def arrives(seconds):
    if seconds is None or seconds < 0 or seconds > 60 * 86400:
        return ""
    if seconds < 90:
        return "any moment now"
    if seconds < 3600:
        return f"in about {round(seconds / 60)} min"
    if seconds < 36 * 3600:
        h = seconds / 3600
        return f"in about {round(h)} hour{'s' if round(h) != 1 else ''}"
    return f"in about {round(seconds / 86400)} days"


# ── qBittorrent's view (optional) ────────────────────────────────────────────
def qbit_torrents(cfg):
    """{HASH: torrent} from qBittorrent, plus its download line, or {} without a connection."""
    s = cfg.source("qbittorrent")
    if not s:
        return {}
    from .adulttidy import qbit
    try:
        with qbit(cfg) as c:
            ts = c.get("/torrents/info", params={"filter": "all"}).json()
    except Exception as e:
        log.info("qbittorrent unavailable for activity: %s", e)
        return {}
    waiting = sorted((t for t in ts if t.get("state") == "queuedDL"), key=lambda t: t.get("priority") or 0)
    place = {t["hash"].upper(): i + 1 for i, t in enumerate(waiting)}
    out = {}
    for t in ts:
        t = dict(t)
        t["line_place"], t["line_length"] = place.get(t["hash"].upper()), len(waiting)
        out[t["hash"].upper()] = t
    return out


# ── why it's where it is ────────────────────────────────────────────────────
def explain(rec, app, torrent=None):
    """(note, stuck) for one Sonarr/Radarr queue entry."""
    state = (rec.get("state") or "").lower()
    status = (rec.get("status") or "").lower()
    msgs = [m for m in rec.get("messages") or [] if m] + ([rec["error"]] if rec.get("error") else [])
    first = msgs[0] if msgs else ""
    if any("already imported" in m.lower() for m in msgs):
        return ("Already in your library. This is a leftover queue entry that can be cleared.", False)
    if state in ("importblocked", "importpending") or (status == "completed" and msgs):
        return (f"Downloaded, but {app} couldn't add it to the library" + (f": {first}" if first else "."), True)
    if state == "importing":
        return ("Downloaded and being added to the library now.", False)
    if state in ("failedpending", "failed") or status == "failed":
        return ("The download failed" + (f": {first}" if first else "") + f". {app} will look for another copy.", True)
    if status == "downloadclientunavailable":
        return ("qBittorrent isn't answering, so this is on hold until it's back.", True)
    if status == "delay":
        return (f"{app} is waiting a little in case a better copy turns up (your delay setting).", False)
    if status == "paused":
        return ("Paused in qBittorrent.", True)
    if torrent:
        ts = torrent.get("state") or ""
        seeds, swarm = torrent.get("num_seeds") or 0, torrent.get("num_complete") or 0
        if ts == "queuedDL":
            place, length = torrent.get("line_place"), torrent.get("line_length")
            return (f"Waiting in line in qBittorrent" + (f" ({place} of {length})" if place else "") +
                    ". It starts when a download slot frees up.", bool(place and place > 20))
        if ts == "metaDL":
            return ("Getting the file list from other people sharing it.", False)
        if ts == "stalledDL":
            if seeds == 0 and swarm == 0:
                return ("Stuck: nobody is sharing this right now. It resumes if someone comes back online, or "
                        f"{app} can search for another copy.", True)
            return (f"Stalled: {swarm} {'people have' if swarm != 1 else 'person has'} it, but none are connected right now.", True)
        if ts in ("downloading", "forcedDL"):
            speed = torrent.get("dlspeed") or 0
            if speed < 20 * 1024:
                return (f"Downloading very slowly ({seeds} of {swarm} sharers connected).", seeds == 0)
            return (f"Downloading from {seeds} {'people' if seeds != 1 else 'person'}.", False)
        if ts in ("pausedDL", "stoppedDL"):
            return ("Paused in qBittorrent.", True)
        if ts in ("error", "missingFiles"):
            return ("qBittorrent reports an error with this download" + (f": {first}" if first else "."), True)
    if status == "queued":
        return ("Waiting in line for a download slot.", False)
    if (rec.get("tracked") or "").lower() in ("warning", "error") and first:
        return (first, True)
    if status in ("downloading", ""):
        return ("Downloading.", False)
    return ("", False)


def annotate(cfg, out):
    """Add note / arrives / stuck to the Sonarr and Radarr queues in an activity payload."""
    queues = [(k, n) for k, n in (("sonarr", "Sonarr"), ("radarr", "Radarr")) if isinstance(out.get(k), dict) and out[k].get("records")]
    if not queues:
        return
    torrents = qbit_torrents(cfg)
    for key, name in queues:
        for rec in out[key]["records"]:
            t = torrents.get((rec.get("download_id") or "").upper())
            rec["note"], rec["stuck"] = explain(rec, name, t)
            secs = _seconds(rec.get("eta"))
            if t and t.get("state") in ("downloading", "forcedDL") and (t.get("eta") or 0) < 8640000:
                secs = t.get("eta")
            rec["arrives"] = "" if rec["stuck"] or (t and t.get("state") == "queuedDL") else arrives(secs)


# ── what's coming up ────────────────────────────────────────────────────────
def coming_up(cfg, days=14):
    """Episodes and movies you're waiting on that come out in the next `days` days, soonest first."""
    today = datetime.date.today()
    end = today + datetime.timedelta(days=days)
    out = []
    if cfg.source("sonarr"):
        with arr.client(cfg.source("sonarr")) as c:
            for e in c.get("/calendar", params={"start": today.isoformat(), "end": end.isoformat(), "includeSeries": "true"}).json():
                if e.get("hasFile") or not e.get("monitored", True):
                    continue
                when = (e.get("airDateUtc") or e.get("airDate") or "")[:10]
                out.append({"date": when, "kind": "episode", "title": (e.get("series") or {}).get("title") or "",
                            "detail": f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}" +
                                      (f" · {e['title']}" if e.get("title") and e["title"] != "TBA" else "")})
    if cfg.source("radarr"):
        with arr.client(cfg.source("radarr")) as c:
            for m in c.get("/calendar", params={"start": today.isoformat(), "end": end.isoformat()}).json():
                if m.get("hasFile") or not m.get("monitored", True):
                    continue
                for key, label in (("digitalRelease", "streaming / digital"), ("physicalRelease", "disc"), ("inCinemas", "in cinemas")):
                    d = (m.get(key) or "")[:10]
                    if d and today.isoformat() <= d <= end.isoformat():
                        out.append({"date": d, "kind": "movie", "title": m.get("title") or "", "detail": label})
                        break
    out.sort(key=lambda x: (x["date"], x["title"]))
    return out[:60]


# ── health ───────────────────────────────────────────────────────────────────
HEALTH_APPS = (("sonarr", "Sonarr", "/health"), ("radarr", "Radarr", "/health"))


def health(cfg):
    """[{app, level, message, link}] problems the apps report about themselves (empty = all fine)."""
    out = []
    for key, name, path in HEALTH_APPS:
        if not cfg.source(key):
            continue
        try:
            with arr.client(cfg.source(key)) as c:
                items = c.get(path).json()
        except Exception as e:
            out.append({"app": name, "level": "error", "message": f"{name} isn't answering ({type(e).__name__}).", "link": ""})
            continue
        out += [{"app": name, "level": i.get("type") or "warning", "message": i.get("message") or "", "link": i.get("wikiUrl") or ""}
                for i in items]
    p = cfg.source("prowlarr")
    if p:
        try:
            with httpx.Client(base_url=p["url"].rstrip("/") + "/api/v1", timeout=20, headers={"X-Api-Key": p["api_key"]}) as c:
                out += [{"app": "Prowlarr", "level": i.get("type") or "warning", "message": i.get("message") or "",
                         "link": i.get("wikiUrl") or ""} for i in c.get("/health").json()]
        except Exception as e:
            out.append({"app": "Prowlarr", "level": "error", "message": f"Prowlarr isn't answering ({type(e).__name__}).", "link": ""})
    order = {"error": 0, "warning": 1}
    out.sort(key=lambda x: order.get(x["level"], 2))
    return out
