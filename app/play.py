"""In-app playback: video from Jellyfin, audiobooks from Audiobookshelf, streamed THROUGH
Omnarr so app credentials never reach the browser. Progress is written back to the
owning app (Jellyfin user data / ABS media progress) — BookBridge then carries audiobook
positions on to the Kobo and Storyteller exactly as if Prologue had been used.

Video: Jellyfin decides per file (PlaybackInfo with a browser device profile): direct
stream when the browser can play it (mp4/h264/aac), otherwise HLS transcoded by Jellyfin
(with whatever hardware acceleration Jellyfin is configured for). Playlists are rewritten so any api_key Jellyfin echoes is removed.
"""
import json
import re
import time
import uuid

import httpx

from .connectors.base import ro_connect

DEVICE_ID = "omnarr-web"
BROWSER_PROFILE = {
    "Name": "Omnarr browser",
    "MaxStreamingBitrate": 20_000_000,
    "MaxStaticBitrate": 100_000_000,
    "DirectPlayProfiles": [
        {"Container": "mp4,m4v", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac,mp3"},
        {"Container": "webm", "Type": "Video", "VideoCodec": "vp8,vp9,av1", "AudioCodec": "opus,vorbis"},
    ],
    "TranscodingProfiles": [
        {"Container": "ts", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac", "Protocol": "hls",
         "Context": "Streaming", "MaxAudioChannels": "2", "MinSegments": "1", "BreakOnNonKeyFrames": True},
    ],
    "SubtitleProfiles": [{"Format": "vtt", "Method": "External"}, {"Format": "srt", "Method": "External"},
                         {"Format": "ass", "Method": "Encode"}, {"Format": "pgssub", "Method": "Encode"}],
    "ContainerProfiles": [], "CodecProfiles": [],
}


def _jf_headers(cfg):
    k = cfg.source("jellyfin")["api_key"]
    return {"Authorization": f'MediaBrowser Client="Omnarr", Device="Omnarr", DeviceId="{DEVICE_ID}", '
                             f'Version="1.0", Token="{k}"'}


def _jf_base(cfg):
    return cfg.source("jellyfin")["url"].rstrip("/")


def _strip_key(url):
    url = re.sub(r"([?&])(api_key|ApiKey)=[^&]*&?", r"\1", url)
    return url.rstrip("?&")


def jf_user(cfg, live_mod):
    return live_mod._jf_user(cfg)


def video_info(cfg, item_id, user_id):
    """What the browser should load for a Jellyfin item (movie or episode)."""
    psid = uuid.uuid4().hex
    with httpx.Client(base_url=_jf_base(cfg), headers=_jf_headers(cfg), timeout=60) as c:
        item = c.get(f"/Users/{user_id}/Items/{item_id}").json()
        info = c.post(f"/Items/{item_id}/PlaybackInfo", params={"userId": user_id},
                      json={"DeviceProfile": BROWSER_PROFILE, "UserId": user_id, "PlaySessionId": psid,
                            "EnableDirectPlay": True, "EnableDirectStream": True, "EnableTranscoding": True,
                            "AllowVideoStreamCopy": True, "AllowAudioStreamCopy": True,
                            "MaxStreamingBitrate": BROWSER_PROFILE["MaxStreamingBitrate"]}).json()
    ms = (info.get("MediaSources") or [{}])[0]
    psid = info.get("PlaySessionId") or psid
    if ms.get("TranscodingUrl"):
        url, mode = "api/stream/jf" + _strip_key(ms["TranscodingUrl"]), "hls"
    else:
        url = f"api/stream/jf/Videos/{item_id}/stream?static=true&mediaSourceId={ms.get('Id')}&playSessionId={psid}"
        mode = "direct"
    subs = []
    for s in ms.get("MediaStreams") or []:
        if s.get("Type") == "Subtitle" and s.get("DeliveryUrl") and s.get("DeliveryMethod") == "External":
            subs.append({"index": s["Index"], "label": s.get("DisplayTitle") or s.get("Language") or "Subtitles",
                         "lang": s.get("Language") or "", "url": "api/stream/jf" + _strip_key(s["DeliveryUrl"]),
                         "default": s["Index"] == ms.get("DefaultSubtitleStreamIndex")})
    ud = item.get("UserData") or {}
    title = item.get("Name") or ""
    if item.get("Type") == "Episode":
        title = f"{item.get('SeriesName', '')} — S{item.get('ParentIndexNumber', 0):02d}E{item.get('IndexNumber', 0):02d} · {title}"
    return {"type": "video", "mode": mode, "url": url, "play_session_id": psid, "item_id": item_id,
            "title": title, "duration": (item.get("RunTimeTicks") or 0) / 1e7,
            "resume": (ud.get("PlaybackPositionTicks") or 0) / 1e7, "subtitles": subs,
            "poster": f"api/cover/jellyfin:{item.get('SeriesId') or item_id}", "series_id": item.get("SeriesId")}


def video_progress(cfg, user_id, item_id, position, duration, finished):
    body = {"PlaybackPositionTicks": int(max(0, position) * 1e7), "LastPlayedDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if finished or (duration and position >= duration * 0.92):
        body.update(Played=True, PlaybackPositionTicks=0)
    with httpx.Client(base_url=_jf_base(cfg), headers=_jf_headers(cfg), timeout=30) as c:
        r = c.post(f"/UserItems/{item_id}/UserData", params={"userId": user_id}, json=body)
    return r.status_code < 400


def video_stop(cfg, play_session_id):
    with httpx.Client(base_url=_jf_base(cfg), headers=_jf_headers(cfg), timeout=30) as c:
        c.delete("/Videos/ActiveEncodings", params={"deviceId": DEVICE_ID, "playSessionId": play_session_id})


def rewrite_playlist(text, base=""):
    """Remove api_key from every line; make root-relative URLs go through our proxy.
    `base` is the path Omnarr is served under ("" at the site root, HA ingress path, ...)."""
    proxy = base.rstrip("/") + "/api/stream/jf"
    out = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = _strip_key(line)
            if line.startswith("/"):
                line = proxy + line
        elif "URI=" in line:
            line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + ((proxy + _strip_key(m.group(1))) if m.group(1).startswith("/") else _strip_key(m.group(1))) + '"', line)
        out.append(line)
    return "\n".join(out) + "\n"


# ── Audiobookshelf ───────────────────────────────────────────────────────────
def abs_token(cfg, owner=False):
    """ABS token for the current request's identity (identity.py): the person's own API key,
    the server's configured one (OWNER), or None (no personal progress). owner=True always
    returns the configured token, for reading files and metadata."""
    from . import identity
    who = identity.OWNER if owner else identity.abs_key.get()
    if who is None:
        return None
    if who != identity.OWNER:
        return who
    s = cfg.source("abs")
    if s.get("api_key"):
        return s["api_key"]
    con = ro_connect(s["db"])
    try:
        r = con.execute("SELECT token FROM users WHERE username=?", (s.get("user"),)).fetchone()
    finally:
        con.close()
    return r["token"] if r else None


def abs_base(cfg):
    s = cfg.source("abs")
    url = s.get("url") or s.get("api_url")
    if not url:
        raise RuntimeError("Audiobookshelf address is not set (Settings → Connections)")
    return url.rstrip("/")


def _audio_info_api(cfg, s, item_id):
    from .connectors import abs as abs_c
    with abs_c.client(s) as c:
        det = c.get(f"/api/items/{item_id}", params={"expanded": 1}).json()
    p = {}
    tok = abs_token(cfg)                      # the listener's own progress, if they have an identity
    if tok:
        with httpx.Client(base_url=abs_base(cfg), timeout=30, headers={"Authorization": f"Bearer {tok}"}) as c:
            pr = c.get(f"/api/me/progress/{item_id}")
            p = pr.json() if pr.status_code == 200 else {}
    m = det.get("media") or {}
    tracks, offset = [], 0.0
    for f in sorted(m.get("audioFiles") or [], key=lambda f: f.get("index") or 0):
        if f.get("exclude") or f.get("invalid"):
            continue
        d = f.get("duration") or 0
        tracks.append({"url": f"api/stream/abs/{item_id}/{f['ino']}", "duration": d, "offset": offset,
                       "mime": f.get("mimeType") or "audio/mp4"})
        offset += d
    chapters = [{"title": c.get("title"), "start": c.get("start"), "end": c.get("end")} for c in m.get("chapters") or []]
    return {"type": "audio", "item_id": item_id, "title": (m.get("metadata") or {}).get("title") or "",
            "duration": m.get("duration") or offset, "tracks": tracks, "chapters": chapters,
            "resume": (p.get("currentTime") if p and not p.get("isFinished") else 0) or 0,
            "cover": f"api/cover/abs:{item_id}" if m.get("coverPath") else ""}


def audio_info(cfg, item_id):
    from . import identity
    s = cfg.source("abs")
    if s.get("api_key"):
        return _audio_info_api(cfg, s, item_id)
    if identity.abs_key.get() != identity.OWNER:
        return None                           # database mode only knows the configured user
    con = ro_connect(s["db"])
    try:
        r = con.execute("""SELECT b.id AS book_id, b.title, b.audioFiles, b.chapters, b.duration, b.coverPath
                           FROM libraryItems li JOIN books b ON b.id=li.mediaId WHERE li.id=?""", (item_id,)).fetchone()
        p = con.execute("""SELECT p.currentTime, p.isFinished FROM mediaProgresses p JOIN users u ON u.id=p.userId
                           WHERE u.username=? AND p.mediaItemId=?""", (s.get("user"), r["book_id"] if r else "")).fetchone() if r else None
    finally:
        con.close()
    if not r:
        return None
    files = sorted(json.loads(r["audioFiles"] or "[]"), key=lambda f: f.get("index") or 0)
    tracks, offset = [], 0.0
    for f in files:
        if f.get("exclude") or f.get("invalid"):
            continue
        d = f.get("duration") or 0
        tracks.append({"url": f"api/stream/abs/{item_id}/{f['ino']}", "duration": d, "offset": offset,
                       "mime": f.get("mimeType") or "audio/mp4"})
        offset += d
    chapters = [{"title": c.get("title"), "start": c.get("start"), "end": c.get("end")} for c in json.loads(r["chapters"] or "[]")]
    return {"type": "audio", "item_id": item_id, "title": r["title"], "duration": r["duration"] or offset,
            "tracks": tracks, "chapters": chapters, "resume": (p["currentTime"] if p and not p["isFinished"] else 0) or 0,
            "cover": f"api/cover/abs:{item_id}" if r["coverPath"] else ""}


def audio_progress(cfg, item_id, position, duration, finished):
    tok = abs_token(cfg)
    if not tok:
        return False                          # no linked Audiobookshelf account: nothing written
    finished = bool(finished or (duration and position >= duration - 30))
    body = {"currentTime": max(0.0, position), "duration": duration,
            "progress": min(1.0, position / duration) if duration else 0, "isFinished": finished}
    with httpx.Client(base_url=abs_base(cfg), timeout=30, headers={"Authorization": f"Bearer {tok}"}) as c:
        r = c.patch(f"/api/me/progress/{item_id}", json=body)
    return r.status_code < 400
