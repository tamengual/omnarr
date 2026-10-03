"""Title/author normalisation for matching the same book across apps.

Ported (logic-wise) from an audiobook/ebook pairing script tuned against a real
~1,000-item library: author surname must agree AND the
normalised title similarity must be >= 0.90. A looser matcher once paired
"Star Wars: Heir to the Empire" with "Star Wars: Thrawn" -- different books.
"""
import difflib
import re

THRESHOLD = 0.90

ORD = (r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
       r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
       r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
       r"eleventh|twelfth|thirteenth)")
SERIES_TAIL = re.compile(r"[:,]?\s*\(?(?:book|volume|vol\.?|part)\s+" + ORD + r"\b.*$", re.I)
SERIES_HEAD = re.compile(r"^[^:]{0,40}?\s+\d+\s*[-–—]\s*")
# Only a subtitle that NAMES a series is dropped. Cutting every subtitle would make
# "Star Wars: Heir to the Empire" equal "Star Wars: Thrawn".
SERIES_SUBTITLE = re.compile(
    r"\s*:\s*(?:the\s+)?[\w'’ -]{1,40}?\s(?:saga|series|trilogy|chronicles|cycle|sequence|quartet|quintet|sextet)\s*$",
    re.I)


def _base(t):
    t = t or ""
    t = re.sub(r"\(unabridged\)|\bunabridged\b", "", t, flags=re.I)
    t = re.sub(r"^[A-Z]{1,3}\d+(?:\.\d+)?\s+", "", t)    # reading-order codes: "EG2.0 Speaker for the Dead"
    t = t.replace("&", " and ")
    t = SERIES_HEAD.sub("", t)                            # "Hunger Games 2 - Catching Fire"
    while True:                                           # trailing "(Silo)", "(A Culture Novel Book 2)"...
        t2 = re.sub(r"\s*\([^)]*\)\s*$", " ", t)
        if t2 == t:
            break
        t = t2
    t = SERIES_TAIL.sub("", t)                            # "...: Book Five of The Wheel of Time"
    t = SERIES_SUBTITLE.sub("", t)                        # "Wool: The Silo Saga" (addition vs stage-pairs)
    t = re.sub(r"\b(a novel|the illustrated edition|tv tie-in|\d+th anniversary edition)\b", "", t, flags=re.I)
    return t


def variants(t):
    """Normalised keys for a title: with/without a leading "Series:" prefix and article."""
    b = _base(t)
    out = []
    for s in (b, re.sub(r"^\s*[^:]{1,28}:\s*", "", b)):
        s2 = re.sub(r"^\s*(the|a|an)\s+", "", s, flags=re.I)
        for x in (s, s2):
            k = re.sub(r"[^a-z0-9]+", "", x.lower())
            if k and k not in out:
                out.append(k)
    return out


def key(t):
    v = variants(t)
    return v[0] if v else ""


def surname(a):
    """Surname of the first author. Handles "Hugh Howey", "Howey, Hugh" and "Howey| Hugh"."""
    raw = (a or "").lower()
    if "," in raw or "|" in raw:
        first = re.split(r"[,|]", raw)[0]
        tok = re.findall(r"[a-z]+", first)
        return tok[-1] if tok else ""
    tok = [t for t in re.findall(r"[a-z]+", raw) if t not in ("dr", "jr", "sr", "md", "phd")]
    return tok[-1] if tok else ""


def similarity(va, vb):
    """Best ratio across every normalisation variant of both titles."""
    if not va or not vb:
        return 0.0
    return max(difflib.SequenceMatcher(None, x, y).ratio() for x in va for y in vb)


def same_book(title_a, author_a, title_b, author_b):
    sa, sb = surname(author_a), surname(author_b)
    if sa and sb and sa != sb:
        return 0.0
    return similarity(variants(title_a), variants(title_b))


_SERIESY = re.compile(r"\b(book|series|saga|novel|vol\.?|volume|part|trilogy|chronicles|cycle|quintet|sextet|universe|#\s*\d)\b|#\d", re.I)


def lookup_titles(title):
    """Title variants to try against outside databases (Wikidata etc.), most specific first:
    "The Handmaid's Tale: Special Edition" -> [..., "The Handmaid's Tale"]."""
    out = []
    for t in (title, re.sub(r"\s*\([^)]*\)\s*$", "", title or ""), (title or "").split(":")[0],
              re.sub(r"\s*[-–—]\s.*$", "", title or "")):
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out


def display_title(t, series=""):
    """A clean title for display: "Expanse 03 - Abaddon's Gate" -> "Abaddon's Gate",
    "Forward the Foundation (The Foundation Series: Prequels, Book 2)" -> "Forward the Foundation"."""
    s = re.sub(r"\s*[\(\[]\s*unabridged\s*[\)\]]", "", t or "", flags=re.I).strip()
    while True:                                           # trailing series-ish parentheticals
        start, raw = _trailing_group(s)
        inner, sw = normalize_word(raw), normalize_word(series)
        if start is None or not (_SERIESY.search(raw) or (sw and inner and (sw in inner or inner in sw))
                                 or _is_series_list(raw, series)):
            break
        s = s[:start].rstrip()
    m = re.match(r"^([^:]{1,40}?)\s+\d{1,3}(?:\.\d)?\s*[-–—]\s+(.+)$", s)    # "Expanse 03 - Title"
    if m and (not series or normalize_word(m.group(1)) in normalize_word(series) or normalize_word(series) in normalize_word(m.group(1))):
        s = m.group(2)
    s = SERIES_TAIL.sub("", s).strip()                    # ": Book Five of The Wheel of Time", ", Book 6"
    if series:                                            # trailing ": The Expanse"
        s = re.sub(r"\s*:\s*(the\s+)?" + re.escape(re.sub(r"^the\s+", "", series, flags=re.I)) + r"\s*$", "", s, flags=re.I)
    s = SERIES_SUBTITLE.sub("", s).strip(" :,-")
    return s or (t or "").strip()


def _trailing_group(s):
    """(start index, inner text) of a balanced "(...)" at the end of s, else (None, "").
    Handles nesting: "X (A (B))" -> inner "A (B)"."""
    t = s.rstrip()
    if not t.endswith(")"):
        return None, ""
    depth = 0
    for i in range(len(t) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(t[i], 0)
        if depth == 0:
            return i, t[i + 1:-1]
    return None, ""


def _is_series_list(raw, series):
    """A nested group whose own trailing part names the series, e.g. calibre's
    "(The Hedge Knight (A Game of Thrones))" on the sequel."""
    start, inner = _trailing_group(raw)
    sw, iw = normalize_word(series), normalize_word(inner)
    return start is not None and bool(sw and iw and (iw in sw or sw in iw))


def normalize_word(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def series_key(s):
    """One key per series however an app spells it: "The Silo Saga" == "Silo",
    "The Expanse Series" == "Expanse". Different words (Robots vs Robot) still need series_aliases."""
    s = re.sub(r"^\s*the\s+", "", (s or "").strip(), flags=re.I)
    s = re.sub(r"\s+(saga|series|trilogy|cycle|chronicles|sequence|books|novels)\s*$", "", s, flags=re.I)
    return normalize_word(s)


def sort_title(t):
    t = (t or "").strip()
    return re.sub(r"^(the|a|an)\s+", "", t, flags=re.I).lower()
