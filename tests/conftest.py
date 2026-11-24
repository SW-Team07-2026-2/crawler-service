import copy
import json
from types import SimpleNamespace

import pytest

from crawler import db as dbm
from crawler.config import Settings
from crawler.http import Http

UA = "indig-crawler/test ( https://indig.co.kr )"


@pytest.fixture
def sessions(tmp_path):
    return dbm.connect(f"sqlite:///{tmp_path / 'crawler.db'}")


def settings(**over) -> Settings:
    base = dict(studio_url="http://studio:8085", database_url="sqlite://", crawler_secret="s" * 32,
                gemini_api_key=None, tavily_api_key=None, kopis_key=None, discogs_token=None, model="gemini-3.5-flash-lite",
                seed_file=None, user_agent=UA, concert_days=90)
    base.update(over)
    return Settings(**base)


class FakeWeb:
    """주소 조각 → (상태, 본문). 먼저 넣은 규칙이 이깁니다. 부른 주소는 calls 에. 이미지 같은 바이트는 add_bytes."""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.byte_routes: list[tuple[str, bytes]] = []
        self.calls: list[str] = []

    def add_bytes(self, part, body: bytes):
        self.byte_routes.insert(0, (part, body))

    def get_bytes(self, url, headers):
        self.calls.append(url)
        assert headers["User-Agent"] == UA
        return next(((200, body) for part, body in self.byte_routes if part in url), (404, b""))

    def add(self, part, body, status=200):
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
        self.routes.insert(0, (part, status, body))

    def __call__(self, url, headers):
        self.calls.append(url)
        assert headers["User-Agent"] == UA
        for part, status, body in self.routes:
            if part in url:
                return status, body
        return 404, ""


def make_http(sessions, web) -> Http:
    return Http(sessions, UA, fetch=web, sleep=lambda s: None, fetch_bytes=getattr(web, "get_bytes", None))


class FakeLlm:
    def __init__(self, concerts=None, genres=None, names=None, mainstream=()):
        self.concerts = concerts or {}      # mt20id → 결과
        self.genres = genres or {}          # 이름 → 장르
        self.names = names or {}            # 이름 → 한글 활동명
        self.web: dict[str, list[str]] = {} # 이름 → 한글 이름 근거로 넘긴 웹 검색 조각
        self.mainstream = set(mainstream)   # 대중 쪽으로 볼 이름 (나머지는 INDIE)
        self.calls: list[str] = []
        self.facts: dict[str, dict] = {}    # mt20id → Haiku 에 넘긴 값
        self.images: dict[str, int] = {}    # mt20id → 이미지 조각 수
        self.input_tokens = self.output_tokens = 0

    def concert(self, facts, images=None):
        mt20id = facts["detail"]["mt20id"]
        self.calls.append(f"concert:{mt20id}")
        self.facts[mt20id], self.images[mt20id] = facts, len(images or [])
        return copy.deepcopy(self.concerts[mt20id])     # 실제 Gemini 처럼 부를 때마다 새 결과 (파이프라인이 고쳐 써도 다음 호출에 안 남게)

    def artist_info(self, name, aliases, albums, label, legal_names=(), web=()):
        self.calls.append(f"artist:{name}")
        self.web[name] = list(web)
        return {"relevance": "MAINSTREAM" if name in self.mainstream else "INDIE",
                "genres": self.genres.get(name, []), "korean_name": self.names.get(name)}


class FakeStudio:
    def __init__(self, reject=None, reviewed=None):
        self.posts: list[tuple[str, dict]] = []
        self.reject = reject                 # 이 external_id 가 묶음에 있으면 400
        self.reviewed = reviewed             # GET /internal/crawl/reviewed 응답 · "down" 이면 실패
        self.gets: list[str] = []

    def get(self, path):
        self.gets.append(path)
        if self.reviewed == "down":
            raise ConnectionError("studio down")
        return self.reviewed or {"concerts": [], "artists": [], "albums": []}

    def post(self, path, body):
        from crawler.studio import StudioRejected
        ids = [i["external_id"] for i in body["items"]]
        if self.reject and self.reject in ids:
            raise StudioRejected(400, '{"code":"INVALID_REQUEST"}')
        self.posts.append((path, body))
        return {"items": [{"external_id": x, "id": n + 1, "result": "CREATED"} for n, x in enumerate(ids)]}


def response(text=None, finish="STOP", calls=(), block=None):
    """Gemini 응답 (SDK 의 실제 타입) — calls: [(함수 이름, 인자)]"""
    from google.genai import types
    parts = [types.Part(function_call=types.FunctionCall(id=f"call-{i}", name=n, args=a)) for i, (n, a) in enumerate(calls)]
    if text is not None:
        parts.insert(0, types.Part(text=text))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=20,
                                                                  thoughts_token_count=30),
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=block) if block else None)


class FakeModels:
    """client.models.generate_content — replies 를 차례로 돌려줌 (예외면 던짐). 받은 요청은 requests 에 (contents 는 그때 모양으로)"""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents) if isinstance(contents, list) else contents,
                              "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def fake_client(*replies):
    return SimpleNamespace(models=FakeModels(replies))
