import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from google.genai import errors

from crawler.enums import GENRES
from crawler.llm import ARTIST_SCHEMA, CONCERT_SCHEMA, Llm, LlmSkip
from crawler.studio import Studio, StudioRejected

from conftest import fake_client, response


def llm_with(*replies, sleeps=None):
    return Llm(None, "gemini-3.5-flash-lite", client=fake_client(*replies),
               sleep=(sleeps.append if sleeps is not None else lambda s: None), gap=0)


def quota_error(quota_id, delay="23s"):
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                              "message": f"Quota exceeded for metric: …, quota_id: {quota_id}",
                                              "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


def test_schemas_fit_structured_output_limits():
    """모든 필드 필수(선택 0개) · union 없음 · additionalProperties false — 구조화 출력 제약 안"""
    for schema in (ARTIST_SCHEMA, CONCERT_SCHEMA):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        text = json.dumps(schema)
        assert "anyOf" not in text and '"type": [' not in text


def test_artist_info_request_and_cleanup():
    llm = llm_with(response('{"genres": ["shoegaze", "INDIE_ROCK", "SHOEGAZE", "NOPE", "EMO", "PUNK"]}'))
    assert llm.artist_info("Silica Gel", ["실리카겔"], ["Kyo181"], "Magic Strawberry Sound")["genres"] == ["SHOEGAZE", "INDIE_ROCK", "EMO"]
    req = llm.client.models.requests[0]
    assert req["model"] == "gemini-3.5-flash-lite"
    cfg = req["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema["properties"]["genres"]["items"]["enum"] == GENRES
    assert cfg.thinking_config.thinking_level.value == "MEDIUM" and cfg.max_output_tokens >= 16000   # 생각 토큰까지 들어갈 자리
    assert llm.input_tokens == 100 and llm.output_tokens == 20 + 30                                # 생각 토큰 포함


def test_artist_korean_name_is_kept_only_when_it_is_hangul_and_not_the_legal_name():
    """11/23 영어 이름은 한글 활동명으로 — 본명(윤진영) · 영어 그대로 · 빈 값은 None (MusicBrainz 이름을 그대로 씀)"""
    web = ["Royal 44(로얄44) - I Been Freestyle | Royal44 #로얄44 ENA 음악차트쇼"]
    ok = llm_with(response('{"genres": ["HIPHOP"], "koreanName": " 로얄44 "}'))
    assert ok.artist_info("Royal 44", [], [], None, web=web) == {"relevance": "INDIE", "genres": ["HIPHOP"], "korean_name": "로얄44"}
    sent = json.loads(ok.client.models.requests[0]["contents"])
    assert sent["legalNames"] == [] and sent["webResults"] == web
    assert ok.client.models.requests[0]["config"].response_json_schema["properties"]["koreanName"]
    for answer in ("윤진영", "ASH ISLAND", ""):
        llm = llm_with(response(json.dumps({"genres": [], "koreanName": answer})))
        assert llm.artist_info("ASH ISLAND", ["윤진영"], [], None, legal_names=["윤진영"])["korean_name"] is None


def test_artist_relevance_is_mainstream_only_when_the_model_says_so():
    """11/24 대중 쪽 아티스트 판단 — MAINSTREAM 만 그대로 · 대소문자 무시 · 그 밖(빠짐 · 이상한 값)은 INDIE (모르는 팀은 다룸)"""
    for answer, expected in (("MAINSTREAM", "MAINSTREAM"), ("mainstream", "MAINSTREAM"), ("INDIE", "INDIE"), ("", "INDIE")):
        llm = llm_with(response(json.dumps({"relevance": answer, "genres": [], "koreanName": ""})))
        assert llm.artist_info("김종국", [], ["Edition"], None)["relevance"] == expected
    assert llm.client.models.requests[0]["config"].response_json_schema["properties"]["relevance"]["enum"] == ["INDIE", "MAINSTREAM"]
    assert "힙합" in llm.client.models.requests[0]["config"].system_instruction


def test_artist_korean_name_must_be_an_alias_or_appear_next_to_the_name_in_a_search_result():
    """11/24 근거 없이 물으니 Royal 44 → '로열포티포' 를 지어냄 — 별칭이거나, 검색 결과 한 건에 영어 이름과 함께 보일 때만"""
    def ask(name, answer, aliases=(), web=()):
        return llm_with(response(json.dumps({"genres": [], "koreanName": answer}))).artist_info(
            name, list(aliases), [], None, web=list(web))["korean_name"]

    assert ask("Royal 44", "로열포티포") is None                                          # 근거 없음
    assert ask("Royal 44", "로열포티포", web=["Royal 44(로얄44) - I Been Freestyle"]) is None
    assert ask("Car, the Garden", "카더가든", aliases=["메이슨 더 소울", "카더가든"]) == "카더가든"
    assert ask("Skinny Brown", "스키니브라운", web=["Skinny Brown - 스키니브라운 | 나무위키"]) == "스키니브라운"
    assert ask("YUL2", "전유진", web=["가수 전유진 | 첫 정규앨범 아디오스", "YUL2 (율) - Lost in Paradise | 싱글"]) is None
    assert ask("YUL2", "율", web=["가수 전유진 | 첫 정규앨범 아디오스", "YUL2 (율) - Lost in Paradise | 싱글"]) == "율"
    moon = llm_with(response('{"genres": [], "koreanName": "문수진"}'))     # 본명으로 활동하면 한글 본명이 활동명
    assert moon.artist_info("Moon Sujin", ["문", "문수진"], [], None, legal_names=["Moon Sujin", "문수진"])["korean_name"] == "문수진"


def test_concert_sends_images_before_the_text():
    llm = llm_with(response('{"relevance": "INDIE"}'))
    llm.concert({"detail": {"mt20id": "PF1"}}, [b"jpeg-1", b"jpeg-2"])
    parts = llm.client.models.requests[0]["contents"]
    assert [p.inline_data.mime_type for p in parts[:2]] == ["image/jpeg", "image/jpeg"] and parts[0].inline_data.data == b"jpeg-1"
    assert json.loads(parts[2].text) == {"detail": {"mt20id": "PF1"}}


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION"])
def test_cutoff_and_safety_skip_the_item(finish):
    with pytest.raises(LlmSkip, match=finish.lower()):
        llm_with(response("{", finish=finish)).concert({"mt20id": "PF1"})


def test_blocked_prompt_and_bad_json_skip():
    with pytest.raises(LlmSkip, match="blocked"):
        llm_with(response("{}", block="PROHIBITED_CONTENT")).concert({"mt20id": "PF1"})
    with pytest.raises(LlmSkip, match="bad_json"):
        llm_with(response("이건 JSON 이 아님")).concert({"mt20id": "PF1"})


def test_minute_limit_waits_retry_delay_then_daily_limit_stops_the_rest_of_the_run():
    sleeps = []
    llm = llm_with(quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"), response('{"genres": []}'),
                   quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier"), sleeps=sleeps)
    assert llm.artist_info("A", [], [], None)["genres"] == []
    assert sleeps == [24.0]                                       # API 가 알려 준 23초 + 1
    with pytest.raises(LlmSkip, match="quota"):
        llm.artist_info("B", [], [], None)
    assert llm.exhausted
    with pytest.raises(LlmSkip, match="quota"):                   # 남은 호출은 API 를 부르지 않음
        llm.artist_info("C", [], [], None)
    assert len(llm.client.models.requests) == 3


def test_server_error_that_keeps_coming_stops_the_rest_of_the_run_not_the_stage():
    """11/19 503 "high demand" 가 20분 이어져 공연 단계 전체가 멈췄음 → 이유 unavailable 로 남은 호출만 건너뜀"""
    busy = lambda: errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}})
    sleeps = []
    llm = llm_with(*[busy() for _ in range(5)], sleeps=sleeps)
    with pytest.raises(LlmSkip, match="unavailable"):
        llm.concert({"detail": {"mt20id": "PF1"}})
    assert sleeps == [60, 120, 240, 480] and llm.exhausted and llm.stop_reason == "unavailable"   # 1 · 2 · 4 · 8분 기다린 뒤에야
    with pytest.raises(LlmSkip, match="unavailable"):              # 남은 호출은 API 를 부르지 않음
        llm.artist_info("A", [], [], None)
    assert len(llm.client.models.requests) == 5


def test_server_error_that_passes_within_the_waits_is_just_a_delay():
    busy = lambda: errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}})
    sleeps = []
    llm = llm_with(busy(), busy(), busy(), response('{"genres": ["EMO"]}'), sleeps=sleeps)
    assert llm.artist_info("A", [], [], None)["genres"] == ["EMO"] and sleeps == [60, 120, 240] and not llm.exhausted


def test_bad_request_skips_only_that_item_but_auth_errors_stop_the_run():
    with pytest.raises(LlmSkip, match="rejected"):
        llm_with(errors.ClientError(400, {"error": {"code": 400, "message": "bad image"}})).concert({"mt20id": "PF1"})
    with pytest.raises(errors.ClientError):
        llm_with(errors.ClientError(403, {"error": {"code": 403, "message": "free tier is not available"}})).concert({"mt20id": "PF1"})


def test_calls_are_spaced_for_the_minute_limit():
    sleeps = []
    llm = Llm(None, "m", client=fake_client(response('{"genres": []}'), response('{"genres": []}')), sleep=sleeps.append, gap=60)
    llm.artist_info("A", [], [], None)
    llm.artist_info("B", [], [], None)
    assert len(sleeps) == 1 and 59 < sleeps[0] <= 60


def test_research_runs_the_function_calls_until_the_model_answers():
    seen = []
    tool = ({"name": "web_search", "description": "검색", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}},
            lambda query: seen.append(query) or {"results": [{"url": "https://a"}]})
    llm = llm_with(response(calls=[("web_search", {"query": "실리카겔 신보"}), ("nope", {})]),
                   response(calls=[("web_search", {"wrong": 1})]),
                   response("찾은 것 [출처: https://a]"))
    assert llm.research("조사", "질문", [tool]) == "찾은 것 [출처: https://a]"
    assert seen == ["실리카겔 신보"]
    first, second, third = llm.client.models.requests
    decl = first["config"].tools[0].function_declarations[0]
    assert decl.name == "web_search" and decl.parameters_json_schema["properties"]["query"] == {"type": "string"}
    assert first["config"].automatic_function_calling.disable is True
    answers = second["contents"][2].parts                          # [질문, 모델의 호출(그대로), 함수 결과]
    assert second["contents"][1].parts[0].function_call.name == "web_search"
    assert [(a.function_response.id, a.function_response.name) for a in answers] == [("call-0", "web_search"), ("call-1", "nope")]
    assert answers[0].function_response.response == {"results": [{"url": "https://a"}]}
    assert "없는 도구" in answers[1].function_response.response["error"]
    assert "인자가 맞지 않음" in third["contents"][4].parts[0].function_response.response["error"]


def test_research_gives_up_after_too_many_turns_and_tool_skip_stops_it():
    loop = [response(calls=[("t", {})]) for _ in range(3)]
    tool = ({"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}, lambda: {})
    with pytest.raises(LlmSkip, match="turns"):
        llm_with(*loop).research("s", "q", [tool], turns=3)

    def out_of_credits():
        raise LlmSkip("search_quota")
    with pytest.raises(LlmSkip, match="search_quota"):
        llm_with(response(calls=[("t", {})])).research("s", "q", [(tool[0], out_of_credits)])


def serve(statuses):
    """statuses 순서대로 응답하는 작은 HTTP 서버 — 받은 요청은 seen 에"""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, dict(self.headers), json.loads(body)))
            status = statuses.pop(0)
            out = json.dumps({"result": {"items": [{"external_id": "a", "id": 1, "result": "CREATED"}]}}
                             if status == 200 else {"code": "X"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_studio_token_and_retry_on_503_only():
    server, seen = serve([503, 200, 400])
    waits = []
    studio = Studio(f"http://127.0.0.1:{server.server_port}", "k" * 32, sleep=waits.append)
    try:
        result = studio.post("/internal/crawl/catalog/artists", {"items": [{"external_id": "a"}]})
        assert result["items"][0]["result"] == "CREATED"
        assert waits == [2] and len(seen) == 2
        path, headers, body = seen[1]
        claims = jwt.decode(headers["X-Internal-Token"], "k" * 32, algorithms=["HS256"], audience="studio", issuer="crawler")
        assert claims["exp"] > 0 and body == {"items": [{"external_id": "a"}]}

        with pytest.raises(StudioRejected) as e:              # 400 은 다시 보내지 않음
            studio.post("/internal/crawl/concerts", {"items": []})
        assert e.value.status == 400 and len(seen) == 3
    finally:
        server.shutdown()


def test_studio_needs_crawler_secret():
    with pytest.raises(ValueError):
        Studio("http://studio:8085", None)
