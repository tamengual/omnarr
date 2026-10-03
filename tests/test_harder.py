from app.harder import title_ok_movie, title_ok_tv


def test_tv_title_rejects_spinoffs_and_accepts_packs():
    assert title_ok_tv("The L Word S03 720P HULU WEBRip x264 [i_c]", "The L Word", 2004, 3)
    assert title_ok_tv("The L Word Season 3 Complete 720P HULU WEBRip", "The L Word", 2004, 3)
    assert title_ok_tv("The.L.Word.2004.S03E01.720p.WEB", "The L Word", 2004, 3)
    assert not title_ok_tv("The L Word Generation Q S03 COMPLETE 720p", "The L Word", 2004, 3)
    assert not title_ok_tv("The L Word S13 720p", "The L Word", 2004, 3)
    assert not title_ok_tv("The L Word S02 720p", "The L Word", 2004, 3)


def test_country_tag_titles():
    assert title_ok_tv("Queer as Folk US S01 DVDRip", "Queer as Folk (US)", 2000, 1)
    assert title_ok_tv("Queer.as.Folk.S01.720p.WEB", "Queer as Folk (US)", 2000, 1)
    assert not title_ok_tv("Queer as Folk 2022 S01 1080p", "Queer as Folk (US)", 2000, 1)


def test_movie_title_needs_year():
    assert title_ok_movie("Project.Hail.Mary.2026.2160p.WEB", "Project Hail Mary", 2026)
    assert not title_ok_movie("Project Hail Mary Behind the Scenes 2026", "Project Hail Mary", 2026)
    assert not title_ok_movie("Dune.1984.1080p", "Dune", 2021)
