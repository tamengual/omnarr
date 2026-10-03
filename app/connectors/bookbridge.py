"""BookBridge (database.db, read-only). Not a media source: it contributes the
links it already made between Audiobookshelf, Calibre and Storyteller.

The DB is large (transcripts); we only ever SELECT from `books`.
"""
from .base import ro_connect


def read_links(cfg):
    """Yield (unit_key_a, unit_key_b) pairs that BookBridge says are the same book."""
    s = cfg.source("bookbridge")
    if not s:
        return []
    con = ro_connect(s["db"])
    pairs = []
    try:
        for r in con.execute("""SELECT abs_id, audio_source, audio_source_id, ebook_source, ebook_source_id,
                                       storyteller_uuid FROM books"""):
            audio = None
            if (r["audio_source"] or "ABS") == "ABS":
                audio = f"abs:{r['audio_source_id'] or r['abs_id']}"
            ebook = f"calibre:{r['ebook_source_id']}" if (r["ebook_source"] == "CWA" and r["ebook_source_id"]) else None
            st = f"storyteller:{r['storyteller_uuid']}" if r["storyteller_uuid"] else None
            nodes = [n for n in (audio, ebook, st) if n]
            for a, b in zip(nodes, nodes[1:]):
                pairs.append((a, b))
    finally:
        con.close()
    return pairs
