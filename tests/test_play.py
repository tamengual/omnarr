from app.play import _strip_key, rewrite_playlist


def test_strip_key_removes_api_key_anywhere():
    assert _strip_key("/videos/1/master.m3u8?api_key=SECRET&MediaSourceId=a") == "/videos/1/master.m3u8?MediaSourceId=a"
    assert _strip_key("/videos/1/master.m3u8?MediaSourceId=a&api_key=SECRET") == "/videos/1/master.m3u8?MediaSourceId=a"
    assert _strip_key("/videos/1/main.m3u8?ApiKey=SECRET") == "/videos/1/main.m3u8"


def test_playlist_rewrite_keeps_relative_and_proxies_absolute():
    src = "#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,URI=\"/videos/1/subs.m3u8?api_key=S\"\nmain.m3u8?a=1&api_key=S\n/videos/1/hls1/main/0.ts?api_key=S&x=2\n"
    out = rewrite_playlist(src)
    assert "SECRET" not in out and "api_key" not in out
    assert "main.m3u8?a=1" in out
    assert "/api/stream/jf/videos/1/hls1/main/0.ts?x=2" in out
    assert 'URI="/api/stream/jf/videos/1/subs.m3u8"' in out
