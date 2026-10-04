"""Serve the inside of an EPUB as if it were a folder.

A Storyteller read-along is one ~1 GB EPUB with the narration inside it. The reader opens it
member by member instead (container.xml, the OPF, one chapter, one SMIL file, one audio clip),
so only what's being read or heard is ever sent. Audio clips are stored uncompressed in the zip,
so a byte range of a clip is a byte range of the file: they stream with seeking, without being
unpacked.
"""
import mimetypes
import os
import struct
import threading
import zipfile

TYPES = {"xhtml": "application/xhtml+xml", "html": "text/html", "opf": "application/oebps-package+xml",
         "ncx": "application/x-dtbncx+xml", "smil": "application/smil+xml", "css": "text/css",
         "xml": "application/xml", "mp4": "audio/mp4", "m4a": "audio/mp4", "m4b": "audio/mp4",
         "mp3": "audio/mpeg", "ogg": "audio/ogg", "opus": "audio/ogg", "wav": "audio/wav",
         "otf": "font/otf", "ttf": "font/ttf", "woff": "font/woff", "woff2": "font/woff2"}
CHUNK = 256 * 1024
_lock = threading.Lock()
_index = {}                                       # path -> ((mtime, size), {member name: ZipInfo})


def members(path):
    """{member name: ZipInfo}, cached until the file changes."""
    st = os.stat(path)
    stamp = (st.st_mtime, st.st_size)
    with _lock:
        hit = _index.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    with zipfile.ZipFile(path) as z:
        infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
    with _lock:
        if len(_index) > 32:
            _index.clear()
        _index[path] = (stamp, infos)
    return infos


def content_type(name):
    ext = name.rsplit(".", 1)[-1].lower()
    return TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def read(path, info):
    """A whole (small) member, decompressed."""
    with zipfile.ZipFile(path) as z:
        return z.read(info)


def stored_span(path, info):
    """(offset, length) of an uncompressed member's bytes in the file. The offset comes from the
    member's LOCAL header: its extra field can differ in length from the central directory's."""
    if info.compress_type != zipfile.ZIP_STORED:
        raise ValueError("member is compressed")
    with open(path, "rb") as f:
        f.seek(info.header_offset)
        head = f.read(30)
    if head[:4] != b"PK\x03\x04":
        raise ValueError("bad local header")
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    return info.header_offset + 30 + name_len + extra_len, info.file_size


def parse_range(header, size):
    """(start, end inclusive) for a single "bytes=" range, None for no/invalid range."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":                               # last N bytes
            n = int(b)
            return (max(0, size - n), size - 1) if n > 0 else None
        start = int(a)
        end = min(int(b), size - 1) if b else size - 1
    except ValueError:
        return None
    return (start, end) if 0 <= start <= end < size else None


def iter_file(path, offset, length):
    with open(path, "rb") as f:
        f.seek(offset)
        left = length
        while left > 0:
            chunk = f.read(min(CHUNK, left))
            if not chunk:
                return
            left -= len(chunk)
            yield chunk
