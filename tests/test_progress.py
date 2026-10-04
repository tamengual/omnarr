"""Per-person progress: members see their own (or none), never the owner's."""
import sqlite3

from app import progress, search
from app.connectors.base import Unit


def _unit(source, sid, pct=None, finished=False, kind="book"):
    return Unit(source=source, source_id=sid, kind=kind, format="audiobook", title=sid, progress=pct, finished=finished,
                extra={"last_listened": "1700000000"})


def test_build_rows_per_account(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    con = sqlite3.connect(state)
    con.execute("CREATE TABLE accounts (id INTEGER, role TEXT, jellyfin_user TEXT, abs_api_key TEXT)")
    con.executemany("INSERT INTO accounts VALUES (?,?,?,?)", [
        (1, "admin", None, None),          # owner identity everywhere
        (2, "member", None, "sam-key"),    # own ABS account only
        (3, "member", None, None)])        # nothing linked
    con.commit()
    editions = [("w-dune", _unit("abs", "abs1", 0.5), ""), ("w-dune", _unit("calibre", "c1", None, True), ""),
                ("w-silo", _unit("abs", "abs2", 0.1), "")]
    monkeypatch.setattr(progress, "_abs_progress", lambda cfg, key: {"abs2": (0.9, False, "1700000500")})
    rows = {(a, w): (p, s) for a, w, p, s, _ in progress.build(None, str(state), editions)}
    assert rows[(1, "w-dune")] == (0.5, "finished")          # owner: ABS + Calibre "read"
    assert rows[(1, "w-silo")][1] == "in_progress"
    assert rows[(2, "w-silo")] == (0.9, "in_progress")        # Sam's own ABS progress
    assert (2, "w-dune") not in rows                          # not the owner's Dune
    assert not any(a == 3 for a, _ in rows)                   # unlinked member: nothing


def test_omnarr_own_progress_counts(tmp_path, monkeypatch):
    """A member with no linked apps still gets progress from what they played in Omnarr."""
    from app import playstate
    state = tmp_path / "state.db"
    con = sqlite3.connect(state)
    con.execute("CREATE TABLE accounts (id INTEGER, role TEXT, jellyfin_user TEXT, abs_api_key TEXT)")
    con.execute("INSERT INTO accounts VALUES (3, 'member', NULL, NULL)")
    con.commit(); con.close()
    playstate.record(str(state), 3, "abs", "abs2", 450, 1000, False)
    playstate.record(str(state), 3, "jellyfin", "ep1", 2700, 2700, True, parent_id="show9")
    show = Unit(source="jellyfin", source_id="show9", kind="show", format="series", title="Korra", extra={"episodes": 4})
    editions = [("w-silo", _unit("abs", "abs2"), ""), ("w-korra", show, "")]
    rows = {(a, w): (p, s) for a, w, p, s, _ in progress.build(None, str(state), editions)}
    assert rows[(3, "w-silo")] == (0.45, "in_progress")
    assert rows[(3, "w-korra")] == (0.25, "in_progress")          # 1 of 4 episodes watched


def test_search_reads_own_progress(tmp_path):
    idx = tmp_path / "index.db"
    con = sqlite3.connect(idx)
    from app import indexer
    con.executescript(indexer.SCHEMA)
    con.execute("INSERT INTO works (id, kind, title, sort_title, authors, narrators, genres, tags, formats, sources, "
                "libraries, info, hidden, adult, progress, status, series, universe) "
                "VALUES ('w1','book','Dune','dune','[]','[]','[]','[]','[]','[]','[]','{}',0,0,0.5,'in_progress','','')")
    con.execute("INSERT INTO user_progress VALUES (2, 'w1', 1.0, 'finished', '')")
    con.commit()
    con.close()
    owner = search.search(str(idx), {}, account_id=None)["items"][0]
    sam = search.search(str(idx), {}, account_id=2)["items"][0]
    nobody = search.search(str(idx), {}, account_id=3)["items"][0]
    assert owner["status"] == "in_progress" and sam["status"] == "finished" and nobody["status"] == "unread"
    assert search.search(str(idx), {"status": "finished"}, account_id=2)["total"] == 1
    assert search.search(str(idx), {"status": "finished"}, account_id=3)["total"] == 0
    assert search.work(str(idx), "w1", account_id=2)["progress"] == 1.0
