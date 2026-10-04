"""A tiny Storyteller-style read-along EPUB: 2 chapters, sentence spans, SMIL overlays and
uncompressed WAV narration (a tone per sentence), laid out like Storyteller's own files."""
import io
import math
import struct
import wave
import zipfile

SENTENCES = {1: ["The ship left at dawn.", "Nobody waved.", "The sea was flat and grey."],
             2: ["By noon the wind came.", "It did not stop for days."]}
SECONDS = 1.0                                     # each sentence's clip length


def _wav(n_sentences, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * SECONDS * n_sentences)):
            tone = 220 + 110 * (i // int(rate * SECONDS))
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * tone * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def build(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""",
                   compress_type=zipfile.ZIP_DEFLATED)
        manifest, spine = [], []
        for c, sents in SENTENCES.items():
            cid = f"c{c}"
            spans = "".join(f'<p><span id="{cid}.xhtml-s{i}">{s}</span></p>' for i, s in enumerate(sents))
            z.writestr(f"OEBPS/{cid}.xhtml", f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter {c}</title></head>
<body><h1>Chapter {c}</h1>{spans}</body></html>""", compress_type=zipfile.ZIP_DEFLATED)
            pars = "".join(f"""<par id="{cid}.xhtml-s{i}"><text src="../{cid}.xhtml#{cid}.xhtml-s{i}"/>
<audio src="../Audio/0000{c}.wav" clipBegin="{i * SECONDS:.3f}s" clipEnd="{(i + 1) * SECONDS:.3f}s"/></par>"""
                           for i in range(len(sents)))
            z.writestr(f"OEBPS/MediaOverlays/{cid}.smil", f"""<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">
<body><seq id="{cid}.xhtml_overlay" epub:textref="../{cid}.xhtml" epub:type="chapter">{pars}</seq></body></smil>""",
                       compress_type=zipfile.ZIP_DEFLATED)
            z.writestr(f"OEBPS/Audio/0000{c}.wav", _wav(len(sents)), compress_type=zipfile.ZIP_STORED)
            manifest.append(f'<item id="{cid}.xhtml" href="{cid}.xhtml" media-type="application/xhtml+xml" media-overlay="{cid}.xhtml_overlay"/>'
                            f'<item id="{cid}.xhtml_overlay" href="MediaOverlays/{cid}.smil" media-type="application/smil+xml"/>'
                            f'<item id="a{c}" href="Audio/0000{c}.wav" media-type="audio/wav"/>')
            spine.append(f'<itemref idref="{cid}.xhtml"/>')
        z.writestr("OEBPS/content.opf", f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">readalong-test</dc:identifier>
<dc:title>A Read-Along Test</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2026-10-04T00:00:00Z</meta>
<meta property="media:active-class">-epub-media-overlay-active</meta></metadata>
<manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine></package>""", compress_type=zipfile.ZIP_DEFLATED)
    return path
