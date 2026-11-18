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
