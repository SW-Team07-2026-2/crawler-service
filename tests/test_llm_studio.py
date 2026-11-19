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


def test_artist_genres_request_and_cleanup():
    llm = llm_with(response('{"genres": ["shoegaze", "INDIE_ROCK", "SHOEGAZE", "NOPE", "EMO", "PUNK"]}'))
    assert llm.artist_genres("Silica Gel", ["실리카겔"], ["Kyo181"], "Magic Strawberry Sound") == ["SHOEGAZE", "INDIE_ROCK", "EMO"]
    req = llm.client.models.requests[0]
    assert req["model"] == "gemini-3.5-flash-lite"
    cfg = req["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema["properties"]["genres"]["items"]["enum"] == GENRES
    assert cfg.thinking_config.thinking_level.value == "HIGH" and cfg.max_output_tokens >= 16000   # 생각 토큰까지 들어갈 자리
    assert llm.input_tokens == 100 and llm.output_tokens == 20 + 30                                # 생각 토큰 포함


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
    assert llm.artist_genres("A", [], [], None) == []
    assert sleeps == [24.0]                                       # API 가 알려 준 23초 + 1
    with pytest.raises(LlmSkip, match="quota"):
        llm.artist_genres("B", [], [], None)
    assert llm.exhausted
    with pytest.raises(LlmSkip, match="quota"):                   # 남은 호출은 API 를 부르지 않음
        llm.artist_genres("C", [], [], None)
    assert len(llm.client.models.requests) == 3


def test_server_error_that_keeps_coming_stops_the_rest_of_the_run_not_the_stage():
    """11/19 503 "high demand" 가 20분 이어져 공연 단계 전체가 멈췄음 → 이유 unavailable 로 남은 호출만 건너뜀"""
    busy = lambda: errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}})
    sleeps = []
    llm = llm_with(*[busy() for _ in range(4)], sleeps=sleeps)
    with pytest.raises(LlmSkip, match="unavailable"):
        llm.concert({"detail": {"mt20id": "PF1"}})
    assert sleeps == [15, 30, 60] and llm.exhausted and llm.stop_reason == "unavailable"
    with pytest.raises(LlmSkip, match="unavailable"):              # 남은 호출은 API 를 부르지 않음
        llm.artist_genres("A", [], [], None)
    assert len(llm.client.models.requests) == 4


def test_bad_request_skips_only_that_item_but_auth_errors_stop_the_run():
    with pytest.raises(LlmSkip, match="rejected"):
        llm_with(errors.ClientError(400, {"error": {"code": 400, "message": "bad image"}})).concert({"mt20id": "PF1"})
    with pytest.raises(errors.ClientError):
        llm_with(errors.ClientError(403, {"error": {"code": 403, "message": "free tier is not available"}})).concert({"mt20id": "PF1"})


def test_calls_are_spaced_for_the_minute_limit():
    sleeps = []
    llm = Llm(None, "m", client=fake_client(response('{"genres": []}'), response('{"genres": []}')), sleep=sleeps.append, gap=60)
    llm.artist_genres("A", [], [], None)
    llm.artist_genres("B", [], [], None)
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
