"""Generates the README diagrams (docs/images/*.svg).

Standalone SVGs: styles are embedded, with a dark-mode variant that follows the
viewer's colour scheme (GitHub's light and dark themes). Run: python docs/diagrams.py
"""
from pathlib import Path

OUT = Path(__file__).parent / "images"

STYLE = """
  .t { font: 500 14px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  .s { font: 400 12px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  .box rect { fill: #F1EFE8; stroke: #888780; } .box .t { fill: #2C2C2A; } .box .s { fill: #5F5E5A; }
  .teal rect { fill: #E1F5EE; stroke: #0F6E56; } .teal .t { fill: #085041; } .teal .s { fill: #0F6E56; }
  .coral rect { fill: #FAECE7; stroke: #993C1D; } .coral .t { fill: #712B13; } .coral .s { fill: #993C1D; }
  .legend { fill: #5F5E5A; }
  .line { stroke: #888780; } .line-teal { stroke: #1D9E75; } .line-coral { stroke: #D85A30; }
  #ah-gray path { stroke: #888780; } #ah-teal path { stroke: #1D9E75; } #ah-coral path { stroke: #D85A30; }
  @media (prefers-color-scheme: dark) {
    .box rect { fill: #2C2C2A; stroke: #888780; } .box .t { fill: #F1EFE8; } .box .s { fill: #B4B2A9; }
    .teal rect { fill: #085041; stroke: #5DCAA5; } .teal .t { fill: #E1F5EE; } .teal .s { fill: #9FE1CB; }
    .coral rect { fill: #712B13; stroke: #F0997B; } .coral .t { fill: #FAECE7; } .coral .s { fill: #F5C4B3; }
    .legend { fill: #B4B2A9; }
    .line { stroke: #B4B2A9; } #ah-gray path { stroke: #B4B2A9; }
  }
"""


def marker(name):
    return (f'<marker id="ah-{name}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" '
            f'orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/></marker>')


def node(x, y, w, h, title, lines=(), kind="box"):
    cx = x + w / 2
    out = [f'<g class="{kind}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" stroke-width="0.75"/>']
    if not lines:
        out.append(f'<text class="t" x="{cx}" y="{y + h / 2}" text-anchor="middle" dominant-baseline="central">{title}</text>')
    else:
        ty = y + (18 if len(lines) == 1 else 20)
        out.append(f'<text class="t" x="{cx}" y="{ty}" text-anchor="middle" dominant-baseline="central">{title}</text>')
        for i, line in enumerate(lines):
            out.append(f'<text class="s" x="{cx}" y="{ty + 20 + i * 17}" text-anchor="middle" dominant-baseline="central">{line}</text>')
    out.append("</g>")
    return "".join(out)


def path(d, color="gray", dashed=False, start=False):
    cls = {"gray": "line", "teal": "line-teal", "coral": "line-coral"}[color]
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    ms = f' marker-start="url(#ah-{color})"' if start else ""
    return f'<path d="{d}" class="{cls}" fill="none" stroke-width="1"{dash}{ms} marker-end="url(#ah-{color})"/>'


def svg(name, height, title, desc, body):
    doc = (f'<svg xmlns="http://www.w3.org/2000/svg" width="680" height="{height}" viewBox="0 0 680 {height}" role="img">'
           f"<title>{title}</title><desc>{desc}</desc><style>{STYLE}</style>"
           f'<defs>{marker("gray")}{marker("teal")}{marker("coral")}</defs>{"".join(body)}</svg>\n')
    (OUT / name).write_text(doc, encoding="utf-8")
    print("wrote", OUT / name)


# 1. How Omnarr fits in
body = [
    node(230, 20, 220, 56, "You", ["Browser, phone, HA sidebar"]),
    path("M340 76 V106"),
    node(200, 110, 280, 56, "Omnarr", ["One search, request, play"], "coral"),
]
for x in (115, 265, 415, 565):
    body.append(path(f"M340 166 V196 H{x} V226", "teal", dashed=True))
body += [
    node(50, 230, 130, 72, "Movies and TV", ["Sonarr, Radarr", "Jellyfin"], "teal"),
    node(200, 230, 130, 72, "Books", ["Calibre, ABS", "Storyteller"], "teal"),
    node(350, 230, 130, 72, "Comics", ["Komga"], "teal"),
    node(500, 230, 130, 72, "Games", ["RomM"], "teal"),
]
for x in (115, 265, 415, 565):
    body.append(path(f"M555 400 V345 H{x} V306"))
body += [
    path("M200 138 H25 V428 H41"),
    node(45, 400, 180, 56, "Request apps", ["Seerr/Shelfmark/ROMarr"]),
    path("M225 428 H251"),
    node(255, 400, 180, 56, "Indexers", ["Prowlarr"]),
    path("M435 428 H461"),
    node(465, 400, 180, 56, "Download client", ["e.g. qBittorrent"]),
    '<line x1="60" y1="486" x2="90" y2="486" class="line-teal" stroke-width="1" stroke-dasharray="4 3"/>',
    '<text class="s legend" x="98" y="486" dominant-baseline="central">Omnarr reads and plays</text>',
    '<line x1="330" y1="486" x2="360" y2="486" class="line" stroke-width="1"/>',
    '<text class="s legend" x="368" y="486" dominant-baseline="central">Requests and downloads</text>',
]
svg("architecture.svg", 510, "How Omnarr fits in",
    "You use Omnarr, which reads and plays from your library apps (movies and TV, books, comics, games). "
    "Requests go from Omnarr to request apps, then indexers, then a download client, which delivers files into the libraries.",
    body)

# 2. How a requested book arrives
body = [
    node(230, 20, 220, 56, "Omnarr", ["Keeps looking for a copy"], "coral"),
    path("M340 76 V102"),
    node(230, 106, 220, 56, "Shelfmark", ["Picks the best release"]),
    path("M340 162 V188"),
    node(230, 192, 220, 56, "Download client", ["e.g. qBittorrent"]),
    path("M340 248 V272 H135 V296"),
    path("M340 248 V272 H555 V296"),
    node(45, 300, 180, 56, "Ingest folder", ["Ebooks and comics"]),
    node(465, 300, 180, 56, "Audiobook folder", ["Separate drop-off"]),
    path("M135 356 V416"),
    path("M135 356 V386 H345 V416"),
    path("M555 356 V416"),
    node(45, 420, 180, 56, "Calibre", ["CWA ingests ebooks"], "teal"),
    node(255, 420, 180, 56, "Komga", ["Comic archives moved"], "teal"),
    node(465, 420, 180, 56, "Audiobookshelf", ["Scans its folder"], "teal"),
]
svg("book-requests.svg", 500, "How a requested book arrives",
    "Omnarr keeps looking via Shelfmark, which picks a release; the download client fetches it. Ebooks and comics land in the "
    "ingest folder: Calibre-Web-Automated ingests ebooks, comic archives are moved to Komga. Audiobooks land in their own "
    "folder, which Audiobookshelf scans.", body)

# 3. Example setup: read-alongs and position sync (one person's server, not part of Omnarr)
body = [
    node(90, 20, 200, 56, "Calibre", ["The ebook"], "teal"),
    node(390, 20, 200, 56, "Audiobookshelf", ["The audiobook"], "teal"),
    path("M190 76 V100 H340 V124"),
    path("M490 76 V100 H340 V124"),
    node(230, 128, 220, 56, "Pairing script", ["Matches ebook and audio"]),
    path("M340 184 V210"),
    node(230, 214, 220, 56, "Storyteller", ["Aligns text to narration"], "teal"),
    path("M340 270 V296"),
    node(230, 300, 220, 56, "BookBridge", ["Keeps positions in sync"], "coral"),
    path("M340 360 V384 H135 V412", "coral", start=True),
    path("M340 360 V412", "coral"),
    path("M340 360 V384 H545 V412", "coral"),
    node(45, 416, 180, 56, "Kobo", ["Reading position"]),
    node(250, 416, 180, 56, "ABS app", ["Listening position"]),
    node(455, 416, 180, 56, "Storyteller app", ["Read-along position"]),
]
svg("example-readalongs.svg", 500, "Example: read-alongs and position sync",
    "An ebook from Calibre and an audiobook from Audiobookshelf are paired, aligned by Storyteller into a read-along, and "
    "BookBridge keeps the position in sync across a Kobo, the Audiobookshelf app and the Storyteller app.", body)
