from crawler import notify


def test_done_message_sums_sent_items_and_errors():
    result = {"run": 7, "status": "DONE", "error": None, "input_tokens": 12345, "output_tokens": 678,
              "stats": {"send_artist_created": 2, "send_artist_unchanged": 1, "send_album_created": 5,
                        "send_album_failed": 1, "concert_indie": 3, "gimbab_error": 2}}
    text = notify.message("dawn", result, 23.4)
    lines = text.splitlines()
    assert lines[0] == "크롤러 새벽 실행 끝 · #7 · 23분"
    assert lines[1] == "Studio 로 보냄: 아티스트 3 · 앨범 5 (거절 1) · 공연 0"
    assert lines[2] == "항목 오류 2건 (아래 *_error)"
    assert lines[3] == "Gemini 토큰 입력 12,345 · 출력 678"
    assert "concert_indie 3" in text


def test_failed_and_skipped_messages():
    failed = notify.message("lunch", {"run": 8, "status": "FAILED", "error": "StudioRejected: Studio 401", "stats": {}}, 1)
    assert failed.splitlines()[:2] == ["크롤러 점심 실행 실패 · #8 · 1분", "오류: StudioRejected: Studio 401"]
    assert notify.message("dawn", {"skipped": 3}, 0) == "크롤러 새벽 실행 건너뜀 — 앞선 실행 #3 이 아직 돌고 있습니다"


def test_send_posts_text_and_never_raises():
    calls = []
    notify.send("https://hooks.slack.com/services/x", "안녕", post=lambda url, body: calls.append((url, body)))
    assert calls == [("https://hooks.slack.com/services/x", {"text": "안녕"})]

    def boom(url, body):
        raise OSError("down")
    notify.send("https://hooks.slack.com/services/x", "안녕", post=boom)   # 예외 없음
    notify.send(None, "안녕", post=boom)                                   # 주소 없으면 부르지 않음


def test_error_codes_are_short_and_say_what_failed():
    import urllib.error
    from crawler.llm import LlmSkip
    from crawler.studio import StudioRejected
    from crawler.webtools import SearchQuota
    assert notify.error_code(StudioRejected(400, '{"isSuccess":false,"code":"INVALID_REQUEST"}')) == "Studio 400 INVALID_REQUEST"
    assert notify.error_code(StudioRejected(401, "")) == "Studio 401"
    assert notify.error_code(RuntimeError("GET https://www.kopis.or.kr/x?service=*** → 404")) == "HTTP 404"
    assert notify.error_code(LlmSkip("unavailable")) == "Gemini unavailable"
    assert notify.error_code(SearchQuota()) == "검색 한도"
    assert notify.error_code(TimeoutError("read")) == "시간 초과"
    assert notify.error_code(urllib.error.URLError(ConnectionRefusedError())) == "연결 실패 (ConnectionRefusedError)"
    assert notify.error_code(KeyError("x")) == "KeyError"


def test_message_lists_where_errors_happened_with_codes_and_examples():
    result = {"run": 9, "status": "FAILED", "error": "공연 [HTTP 400]: RuntimeError: …", "input_tokens": 1, "output_tokens": 2,
              "stage_errors": ["공연 [HTTP 400]: RuntimeError: GET https://www.kopis.or.kr/openApi/restful/pblprfr?service=SECRET → 400 <dbs>"],
              "failures": {
                  "gimbab": {"count": 1, "codes": {"HTTP 404": 1}, "samples": [("Silica Gel", "GET https://www.gimbabrecords.com/x → 404")]},
                  "concert_image": {"count": 5, "codes": {"HTTP 404": 4, "시간 초과": 1},
                                    "samples": [("PF1 http://www.kopis.or.kr/a.gif", "GET … → 404")] * 5}},
              "stats": {"gimbab_error": 1, "concert_image_error": 5, "concert_llm_unavailable": 41, "artist_llm_quota": 2}}
    lines = notify.message("dawn", result, 90).splitlines()
    assert lines[1] == "단계 실패 1건"
    assert lines[2] == ("• 공연 [HTTP 400]: RuntimeError: GET https://www.kopis.or.kr/openApi/restful/pblprfr?service=*** → 400 &lt;dbs&gt;")
    i = lines.index("항목 오류 6건 (어디서 · 코드 · 예시)")
    assert lines[i + 1] == "• 공연 포스터 · 소개 이미지 (concert_image) 5건 — HTTP 404 ×4 · 시간 초과 ×1"   # 많은 순
    assert lines[i + 2] == "    예) PF1 http://www.kopis.or.kr/a.gif: GET … → 404"
    assert lines[i + 5] == "• 김밥레코즈 (gimbab) 1건 — HTTP 404 ×1"                                         # 예시는 3개까지
    assert "Gemini 로 못 한 것 (다음 실행에): artist_llm_quota 2 · concert_llm_unavailable 41" in lines
    assert all("SECRET" not in line for line in lines)
