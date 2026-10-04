"""NSFW-tagged Calibre books and 18+ Komga books go behind the private-section PIN."""
import sqlite3

from app.connectors import calibre, komga


class Cfg:
    def __init__(self, sources):
        self.sources = sources

    def source(self, name):
        return self.sources.get(name)

    def link(self, name):
        return ""


def _calibre(tmp_path):
    db = tmp_path / "metadata.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE books (id INTEGER, title TEXT, timestamp TEXT, pubdate TEXT, series_index REAL, path TEXT, has_cover INTEGER);
        CREATE TABLE authors (id INTEGER, name TEXT);
        CREATE TABLE books_authors_link (id INTEGER, book INTEGER, author INTEGER);
        CREATE TABLE tags (id INTEGER, name TEXT);
        CREATE TABLE books_tags_link (book INTEGER, tag INTEGER);
        CREATE TABLE identifiers (book INTEGER, type TEXT, val TEXT);
        CREATE TABLE data (book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
        CREATE TABLE series (id INTEGER, name TEXT);
        CREATE TABLE books_series_link (book INTEGER, series INTEGER);
        CREATE TABLE comments (book INTEGER, text TEXT);
        INSERT INTO books VALUES (1, 'Dune', '2020-01-01', '1965-01-01', 1, 'a/1', 0), (2, 'Spicy', '2020-01-01', '2020-01-01', 1, 'a/2', 0);
        INSERT INTO authors VALUES (1, 'Someone');
        INSERT INTO books_authors_link VALUES (1, 1, 1), (2, 2, 1);
        INSERT INTO tags VALUES (1, 'Fiction'), (2, 'nsfw');
        INSERT INTO books_tags_link VALUES (1, 1), (2, 2);
        INSERT INTO data VALUES (1, 'EPUB', 'x', 1), (2, 'PDF', 'y', 1);
    """)
    con.commit(); con.close()
    return str(db)


def test_calibre_nsfw_tag_is_adult(tmp_path):
    db = _calibre(tmp_path)
    units = {u.title: u for u in calibre.read(Cfg({"calibre": {"db": db}}))}
    assert units["Spicy"].adult and not units["Dune"].adult


def test_calibre_adult_tags_can_be_turned_off(tmp_path):
    db = _calibre(tmp_path)
    units = calibre.read(Cfg({"calibre": {"db": db, "adult_tags": []}}))
    assert not any(u.adult for u in units)


def test_komga_age_rating_18_is_adult(monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, params=None):
            class R:
                def json(_): return [{"id": "L", "name": "Comics"}]
            return R()

    series = [{"id": "s1", "metadata": {"ageRating": 18}, "booksCount": 1},
              {"id": "s2", "metadata": {"ageRating": None}, "booksCount": 1}]
    books = [{"id": "b1", "seriesId": "s1", "name": "Late night"}, {"id": "b2", "seriesId": "s2", "name": "Bone"}]
    monkeypatch.setattr(komga, "client", lambda s, timeout=60: C())
    monkeypatch.setattr(komga, "_pages", lambda c, path, method="get": iter(series if "series" in path else books))
    units = {u.title: u for u in komga._read_api(Cfg({}), {"url": "http://k", "api_key": "x"})}
    assert units["Late night"].adult and not units["Bone"].adult


def test_komga_reading_progress(monkeypatch):
    """Komga's readProgress (page / pages) feeds Continue, like any other progress."""
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, params=None):
            class R:
                def json(_): return [{"id": "L", "name": "Comics"}]
            return R()

    series = [{"id": "s1", "metadata": {}, "booksCount": 3}]
    books = [
        {"id": "b1", "seriesId": "s1", "name": "Part 1", "media": {"pagesCount": 80},
         "readProgress": {"page": 20, "completed": False, "lastModified": "2026-10-04T04:45:46Z"}},
        {"id": "b2", "seriesId": "s1", "name": "Part 2", "media": {"pagesCount": 80},
         "readProgress": {"page": 80, "completed": True, "lastModified": "2026-10-04T05:00:00Z"}},
        {"id": "b3", "seriesId": "s1", "name": "Part 3", "media": {"pagesCount": 80}, "readProgress": None},
    ]
    monkeypatch.setattr(komga, "client", lambda s, timeout=60: C())
    monkeypatch.setattr(komga, "_pages", lambda c, path, method="get": iter(series if "series" in path else books))
    units = {u.title: u for u in komga._read_api(Cfg({}), {"url": "http://k", "api_key": "x"})}
    assert units["Part 1"].progress == 0.25 and not units["Part 1"].finished
    assert units["Part 1"].extra["last_listened"] == "2026-10-04T04:45:46"
    assert units["Part 2"].finished and units["Part 2"].progress == 1.0
    assert units["Part 3"].progress is None and not units["Part 3"].finished
