"""Example Omnarr plug-in: a folder of video files as a library, plus requests sent to a log.

Copy this file into Omnarr's plug-ins folder (/data/plugins by default; mount it with
`- ./plugins:/data/plugins`), restart Omnarr, and it appears in Settings → Connections as
"Folder library (plug-in)". Fill in its settings and press Test.

A plug-in is a normal Python module with:
  PLUGIN                     dict: key, label, category, about, fields, and optionally "requests"
  read(settings)   -> list   items in Omnarr's ITEM format (see docs/extending.md)
  test(settings)   -> (ok, message)                       optional
  request(settings, item) -> {"message", "id"}            optional; for the formats in PLUGIN["requests"]
  cover(settings, cover_url) -> bytes | None              optional; when covers need special fetching

`settings` holds the values from the plug-in's fields. Secrets ("type": "secret") are stored
server-side and never shown to the browser again. Plug-ins run inside Omnarr with its
permissions: only install plug-ins you have read and trust.
"""
import json
import os
import time

PLUGIN = {
    "key": "folder_videos",
    "label": "Folder library",
    "category": "Your own apps",
    "about": "Lists the video files in a folder (mounted into Omnarr) as movies, and logs requests to a file.",
    "fields": [
        {"key": "folder", "label": "Folder inside the container", "type": "path", "required": True, "placeholder": "/media/home-videos"},
        {"key": "request_log", "label": "Request log file (optional)", "type": "path", "required": False,
         "placeholder": "/data/plugins/requests.jsonl"},
    ],
    "requests": ["movie"],          # Omnarr sends movie requests here instead of Seerr
}
VIDEO = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".webm")


def read(settings):
    folder = settings.get("folder") or ""
    items = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(VIDEO):
                continue
            path = os.path.join(root, name)
            title = os.path.splitext(name)[0].replace(".", " ").replace("_", " ").strip()
            items.append({
                "id": os.path.relpath(path, folder),            # stable id: the path inside the folder
                "kind": "movie",
                "title": title,
                "added": time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(path))),
                "library": os.path.basename(folder.rstrip("/")) or "Folder",
            })
    return items


def test(settings):
    folder = settings.get("folder") or ""
    if not os.path.isdir(folder):
        return False, f"{folder or 'The folder'} isn't visible inside Omnarr's container: check the volume mount"
    n = len(read(settings))
    return True, f"Found {n} video file{'s' if n != 1 else ''}"


def request(settings, item):
    """item: {"format", "title", "ids": {...}, "requested_by", ...}"""
    log = settings.get("request_log")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), **item}) + "\n")
    return {"message": f"Logged a request for {item.get('title') or 'something'}"}
