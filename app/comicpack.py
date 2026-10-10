"""Comic EPUBs (as sold by Kobo, Apple and others): fixed-layout books that are one page image per
page. Uploads like that belong in the comics library, so they're repacked as CBZ with the same
images in reading order. Nothing is re-compressed; page images are copied byte for byte."""
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

IMG = re.compile(r'(?:src|xlink:href|href)\s*=\s*["\']([^"\']+\.(?:jpe?g|png|gif|webp|avif))["\']', re.I)
MIN_PAGES = 20


def _read(path):
    z = zipfile.ZipFile(path)
    opf_path = ET.fromstring(z.read("META-INF/container.xml")).find(".//{*}rootfile").get("full-path")
    opf = ET.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)
    manifest = {i.get("id"): i.get("href") for i in opf.iter() if i.tag.endswith("item")}
    fixed = any(m.get("property") == "rendition:layout" and (m.text or "").strip() == "pre-paginated"
                for m in opf.iter() if m.tag.endswith("meta"))
    title = next(((t.text or "").strip() for t in opf.iter() if t.tag.endswith("}title") and (t.text or "").strip()), "")
    images, text_pages = [], 0
    for ref in (i for i in opf.iter() if i.tag.endswith("itemref")):
        href = manifest.get(ref.get("idref"))
        if not href:
            continue
        doc = posixpath.normpath(posixpath.join(base, href))
        html = z.read(doc).decode("utf-8", "replace")
        found = [posixpath.normpath(posixpath.join(posixpath.dirname(doc), m)) for m in IMG.findall(html)]
        words = len(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(style|script|head)\b.*?</\1>", " ", html)).split())
        images.extend(i for i in found if i not in images)
        if not found or words > 120:
            text_pages += 1
    return z, images, fixed, text_pages, title


def inspect(path):
    """{"comic": bool, "pages": n, "title": str}; never raises on a broken or non-EPUB file."""
    try:
        z, images, fixed, text_pages, title = _read(path)
        z.close()
    except Exception:
        return {"comic": False, "pages": 0, "title": ""}
    spine = len(images) + text_pages
    comic = len(images) >= MIN_PAGES and (fixed or text_pages <= max(2, spine * 0.05))
    return {"comic": comic, "pages": len(images), "title": title}


def to_cbz(path, out):
    """Write the page images to `out` (a .cbz) in reading order; returns the page count, checked."""
    z, images, *_ = _read(path)
    width = len(str(len(images)))
    with z, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as cbz:
        for n, img in enumerate(images, 1):
            cbz.writestr(f"{n:0{width}d}{posixpath.splitext(img)[1].lower()}", z.read(img))
    with zipfile.ZipFile(out) as check:
        if check.testzip() is not None or len(check.namelist()) != len(images):
            raise ValueError("the comic file didn't come out complete")
    return len(images)
