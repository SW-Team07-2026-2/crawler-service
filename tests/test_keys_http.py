from datetime import timedelta

import pytest
from sqlalchemy import select

from crawler import db as dbm
from crawler.http import Blocked, redact
from crawler.keys import content_hash, full_date, norm

from conftest import FakeWeb, make_http


def test_norm_full_date_hash():
    assert norm("The Black Skirts") == norm("black  skirts") == "blackskirts"
    assert norm("검정 치마") == "검정치마"
    assert norm("ＳＥ ＳＯ ＮＥＯＮ") == norm("SE SO NEON")
    assert full_date("2022-09-15") == "2022-09-15"
    assert full_date("2022") is None and full_date("2022-09") is None and full_date("2022-00-00") is None
    assert content_hash({"a": 1, "b": [1, 2]}) == content_hash({"b": [1, 2], "a": 1})


def test_robots_blocks_disallowed_paths_and_is_read_once(sessions):
    web = FakeWeb([("/robots.txt", 200, "User-agent: *\nDisallow: /private\n"), ("example.com", 200, "ok")])
    http = make_http(sessions, web)
    assert http.get("t", "https://example.com/list") == "ok"
    with pytest.raises(Blocked):
        http.get("t", "https://example.com/private/1")
    assert http.get("t", "https://example.com/list2") == "ok"
    assert web.calls.count("https://example.com/robots.txt") == 1
    assert "https://example.com/private/1" not in web.calls        # 막힌 주소는 부르지도 않음


def test_musicbrainz_api_follows_api_policy_not_robots(sessions):
    """MusicBrainz robots.txt 는 /ws 를 막지만 API 이용 정책이 허락 - robots 를 읽지 않고 1.5초 간격으로 부름"""
    web = FakeWeb([("/robots.txt", 200, "User-agent: *\nDisallow: /ws\n"), ("/ws/2/", 200, "{}")])
    waits = []
    http = make_http(sessions, web)
    http._sleep = waits.append
    http.get("musicbrainz", "https://musicbrainz.org/ws/2/artist?query=a")
    http.get("musicbrainz", "https://musicbrainz.org/ws/2/artist?query=b")
    assert not any("robots" in c for c in web.calls)
    assert waits and 1.0 < waits[0] <= 1.5
    with pytest.raises(Blocked):                                   # 정책 밖 경로는 robots 그대로
        http.get("musicbrainz", "https://musicbrainz.org/ws/other")


def test_cache_retry_redact_and_errors(sessions):
    web = FakeWeb([("/robots.txt", 404, "")])
    http = make_http(sessions, web)
    web.add("api.test/x", "first")
    url = "https://api.test/x?service=SECRET123&rows=1"
    assert http.get("t", url, max_age=timedelta(hours=1)) == "first"
    web.add("api.test/x", "second")
    assert http.get("t", url, max_age=timedelta(hours=1)) == "first"          # 캐시
    assert http.get("t", url) == "second"                                      # max_age 없으면 새로
    with sessions() as s:
        stored = s.scalars(select(dbm.RawPage.url)).all()
    assert stored == ["https://api.test/x?service=***&rows=1"]                 # 키는 저장하지 않음
    assert redact("a?token=abc&key=def") == "a?token=***&key=***"

    flaky = iter([(503, ""), (200, "ok")])
    http2 = make_http(sessions, lambda u, h: (200, "") if u.endswith("robots.txt") else next(flaky))
    assert http2.get("t", "https://flaky.test/y") == "ok"

    web.add("api.test/gone", "", status=404)
    with pytest.raises(RuntimeError, match="404") as e:
        http.get("t", "https://api.test/gone?key=SECRET")
    assert "SECRET" not in str(e.value)


def test_log_filter_hides_keys_whoever_logs_the_url():
    import logging
    from crawler.__main__ import _Redact
    rec = logging.LogRecord("scrapling", logging.WARNING, __file__, 1, "Fetched <GET %s>", ("https://x/a?service=SECRET&b=1",), None)
    assert _Redact().filter(rec) and rec.getMessage() == "Fetched <GET https://x/a?service=***&b=1>"


def test_failure_log_label_is_not_mistaken_for_a_secret():
    from crawler.http import redact
    line = "crawl_concert_fetch_failed item=PF1 error=GET https://x/pblprfr/PF1?service=K → 400"
    assert redact(line) == "crawl_concert_fetch_failed item=PF1 error=GET https://x/pblprfr/PF1?service=*** → 400"


def test_kopis_errors_are_retried_later_but_other_sites_are_not(sessions):
    """KOPIS 가 400 을 주다가 곧 200 - KOPIS 만 30, 60초 쉬었다가 다시 (그래도 안 되면 그 항목만 실패)"""
    from crawler.http import Http
    from conftest import UA
    answers = {"www.kopis.or.kr": iter([(400, "<html>Bad Request</html>"), (200, "<dbs/>")]),
               "other.test": iter([(400, "bad")])}
    sleeps = []
    http = Http(sessions, UA, fetch=lambda u, h: (200, "") if u.endswith("robots.txt") else next(answers[u.split("/")[2]]),
                sleep=sleeps.append, fetch_bytes=lambda u, h: next(images))
    assert http.get("kopis", "https://www.kopis.or.kr/openApi/restful/pblprfr/PF1?service=K") == "<dbs/>"
    assert [s for s in sleeps if s >= 30] == [30]
    with pytest.raises(RuntimeError, match="400 bad"):
        http.get("t", "https://other.test/x")                                   # 다른 사이트는 다시 부르지 않음

    images = iter([(500, b""), (502, b""), (503, b"")])
    sleeps.clear()
    with pytest.raises(RuntimeError, match="503"):
        http.get_bytes("kopis", "https://www.kopis.or.kr/upload/pfmPoster/PF1.gif")   # 30, 60초 뒤에도 실패 -> 그 이미지만
    assert [s for s in sleeps if s >= 30] == [30, 60]


def test_kopis_requests_are_spaced_five_seconds_apart(sessions):
    """~22 KOPIS 가 1초 간격 연속 요청을 수시로 막음 -> KOPIS 만 3초 간격, 3초에도 막혀 5초"""
    from crawler.http import Http
    from conftest import UA
    sleeps = []
    http = Http(sessions, UA, fetch=lambda u, h: (200, "<dbs/>"), sleep=sleeps.append)
    http.get("kopis", "https://www.kopis.or.kr/openApi/restful/pblprfr/PF1?service=K")
    http.get("kopis", "https://www.kopis.or.kr/openApi/restful/pblprfr/PF2?service=K")
    assert len(sleeps) == 1 and 4.9 < sleeps[0] <= 5.0
    sleeps.clear()
    http.get("t", "https://other.test/a")
    http.get("t", "https://other.test/b")
    assert len(sleeps) == 1 and sleeps[0] <= 1.0                         # 다른 사이트는 그대로 1초


def test_include_ids_read_from_the_seed_file(tmp_path, monkeypatch):
    from crawler import config
    f = tmp_path / "include.txt"
    f.write_text("# 주석\n86bfda70-ad8c-4db4-9041-e4843d918269  # QWER\n\n", encoding="utf-8")
    monkeypatch.setenv("INCLUDE_FILE", str(f))
    assert config.load().include_ids == frozenset({"86bfda70-ad8c-4db4-9041-e4843d918269"})
    monkeypatch.setenv("INCLUDE_FILE", str(tmp_path / "none.txt"))
    assert config.load().include_ids == frozenset()
