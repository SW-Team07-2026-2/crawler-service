"""Gemini 전처리 (11/18 Anthropic Haiku → Gemini 무료 등급 · 기본 gemini-3.5-flash-lite · CRAWLER_MODEL 로 바꿈).

- 구조화 출력(response_json_schema)으로 정해진 모양 · 값만 받습니다. 모든 필드를 필수로 두고 "모름" 은 "" · -1 · NONE · UNKNOWN 으로 받아
  코드에서 null 로 바꿉니다 (Haiku 때 스키마 그대로 · 11/18 실호출로 enum · additionalProperties 받는 것 확인).
- 정확도가 먼저라(11/17 사용자 결정) 생각 수준(thinking_level)은 HIGH. 생각 토큰도 max_output_tokens 에 들어가므로 넉넉히 둡니다.
- 웹 조사(research)는 우리 함수(웹 검색 · 페이지 열기)를 모델이 고르는 함수 호출 반복 — 자동 함수 호출은 끄고 직접 돌립니다
  (도구 결과를 기록해 지어낸 출처를 거르기 위해 · sources/websearch.py).
- 무료 등급 한도: 분당 요청 수를 넘지 않게 호출 사이를 띄우고, 429 는 API 가 알려 준 시간만큼 쉬었다가 다시.
  하루 한도(PerDay)를 다 쓰면 이번 실행의 남은 호출은 모두 건너뜁니다 (LlmSkip("quota") — 다음 실행에 이어서).
  서버 오류(500 · 503 · 504)가 다시 불러도 계속되면 똑같이 남은 호출을 건너뜁니다 (LlmSkip("unavailable") ·
  11/19 503 "high demand" 가 20분 이어져 공연 단계 전체가 멈췄음 — 다른 단계는 끝까지 돌고 남은 것은 다음 실행에).
"""
import json
import logging
import re
import time

from google import genai
from google.genai import errors, types

from .enums import CONCERT_TYPES, GENRES, REGIONS

log = logging.getLogger(__name__)

MAX_TOKENS = 16000     # 생각 토큰 포함
GAP = 6.5              # 호출 사이 최소 간격(초) — 무료 등급 분당 10건 안팎 (공식 수치 비공개 · 11/18 사용자 측정 보고 기준)
TRIES = 4              # 분당 한도 · 서버 오류(500 · 503 · 504) 때 다시 부르는 횟수
TURNS = 16             # 웹 조사 한 번에 모델이 도구를 부르는 최대 차례


class LlmSkip(Exception):
    """거절 · 길이 초과 · 한도 소진 · 요청 거절(400) — 그 항목은 이번에 건너뜀 (다음 실행에 다시)"""


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


ARTIST_SCHEMA = _obj({"genres": {"type": "array", "items": {"type": "string", "enum": GENRES}}})

CONCERT_SCHEMA = _obj({
    "relevance": {"type": "string", "enum": ["INDIE", "MAINSTREAM", "NOT_CONCERT"]},
    "title": {"type": "string"},
    "type": {"type": "string", "enum": CONCERT_TYPES},
    "genre": {"type": "string", "enum": [*GENRES, "NONE"]},
    "startsAt": {"type": "string", "description": "ISO 8601 · +09:00"},
    "endsAt": {"type": "string", "description": "ISO 8601 · +09:00 · 모르면 빈 문자열"},
    "bookingOpenAt": {"type": "string", "description": "일반 예매 시작 · ISO 8601 · +09:00 · 모르면 빈 문자열"},
    "bookingNotes": {"type": "array", "items": {"type": "string"}, "description": "선예매 · 차수별 오픈 등 예매 일정 요약 (관리자 참고)"},
    "price": {"type": "integer", "description": "원 · 무료 0 · 모르면 -1"},
    "venueName": {"type": "string"},
    "region": {"type": "string", "enum": [*REGIONS, "UNKNOWN"]},
    "artistNames": {"type": "array", "items": {"type": "string"}},
    "unmatchedCast": {"type": "array", "items": {"type": "string"}, "description": "팀 이름으로 확인하지 못한 출연진 (artistNames 에는 없음)"},
    "summary": {"type": "string"},
    "notices": {"type": "array", "items": {"type": "string"}},
})

ARTIST_SYSTEM = """한국 인디 아티스트의 장르를 정해진 목록에서 최대 3개, 대표적인 것부터 고릅니다.
이 아티스트를 확실히 알지 못하면 빈 배열을 돌려줍니다 — 이름만 보고 짐작하지 않습니다. 앨범 제목 · 레이블은 같은 이름의 다른 아티스트와 구분하는 참고용입니다.
결과는 관리자가 확인한 뒤 서비스에 들어갑니다."""

CONCERT_SYSTEM = """KOPIS 공연 상세(JSON)와 그 공연의 포스터 · 소개 이미지를 indig 공연 후보로 정리합니다. indig 는 한국 인디 음악(밴드 · 싱어송라이터 · 인디 레이블) 플랫폼이고, 결과는 관리자가 확인한 뒤 등록됩니다.
입력: detail(KOPIS 상세) · venue(공연시설 API 의 주소 · 좌석) · knownArtists(우리가 아는 아티스트 이름 중 이 공연 글에 보이는 것) · 이미지(포스터 다음에 소개 이미지, 긴 이미지는 위에서부터 잘라 순서대로).
- relevance: 인디 음악 공연이면 INDIE. 아이돌 · 트로트 · 대형 기획사 가수 · 방송사 공연 · 클래식 · 뮤지컬은 MAINSTREAM. 음악 공연이 아니면 NOT_CONCERT. 애매하면 INDIE.
- type: 여러 팀이 나오는 페스티벌 · 합동 공연은 FESTIVAL, 한 팀의 단독 공연은 SOLO. genre 는 FESTIVAL 일 때만 대표 장르, SOLO 는 NONE.
- startsAt · endsAt: 기간(prfpdfrom~prfpdto)과 공연 시간 안내(dtguidance)로 첫 회 시작 · 마지막 회 끝을 한국 시간 ISO 8601(+09:00)로. 끝 시각이 없으면 공연 시간(prfruntime)으로 계산하고, 그것도 없으면 "".
- bookingOpenAt: 소개 이미지 · 글의 "티켓 오픈 · 예매 오픈 · 예매 일정" 공지에서 일반 예매가 시작되는 시각. 선예매(팬클럽 · 카드사 등)만 있으면 그 시각이 아니라 "". 연도가 없으면 공연 날짜보다 앞서는 가장 가까운 연도. 시각이 없으면 "".
- bookingNotes: 선예매 · 차수별 오픈 · 예매처별 오픈처럼 bookingOpenAt 하나로 담기지 않는 예매 일정을 날짜 · 시각과 함께 짧게 (관리자 참고용). 없으면 [].
- price: 가격 안내(pcseguidance)에서 가장 낮은 일반 좌석 가격(원). 무료면 0, 모르면 -1.
- region: 공연장 주소 · 지역(area) 기준. 판단할 수 없으면 UNKNOWN.
- artistNames: 무대에 서는 아티스트 · 팀 이름을 소개 순서대로 (첫 번째가 헤드라이너). 출연진(prfcast)이 멤버의 실명이면 제목 · 포스터 · 소개 이미지 · knownArtists 로 확인한 팀 이름으로 바꾸고, 같은 팀은 한 번만 씁니다.
  팀마다 표기는 하나만 씁니다 — 제목에 쓰인 표기를 먼저, 제목에 없으면 포스터에 크게 쓰인 표기. 다른 표기 · 설명을 붙이지 않습니다
  ("ONEWE 원위" → "원위", 제목이 "THE SOLUTIONS (솔루션스)" 면 "THE SOLUTIONS").
  확인하지 못한 이름은 넣지 않습니다. 연출 · 스태프 · 세션 · 객원 연주자 · 사회자 · "등" 은 뺍니다.
- unmatchedCast: 출연진 중 팀 이름으로 확인하지 못했고 세션 · 스태프로도 보이지 않는 이름 그대로. 짐작으로 팀을 붙이지 않습니다.
- summary: 공연을 소개하는 2~4문장 (누가 · 어떤 공연인지 · 공연의 성격이나 기획 의도). 원문을 옮겨 쓰지 말고 이미지 · 글에 적힌 사실만으로 직접 씁니다.
  포스터의 그림 · 색 · 분위기를 해석하지 않고, 가격 · 예매 · 예매처 이야기는 넣지 않습니다.
- notices: 공연장에서 관람객이 알아야 할 것(입장 시작 시각 · 입장 순서 · 스탠딩/지정석 · 관람 연령 확인 방법 · 반입 금지 물품 · 촬영 규정 · 물품 보관 등)을 한 줄씩 최대 8개.
  티켓은 indig 가 직접 팔고 약관도 따로 있으므로 티켓 수령 · 배송 · 취소 · 환불 · 대리 수령 · 예매 매수 · 예매 아이디 · 할인 · 예매처 · 고객센터 · 문의처처럼 예매처에 따라 다른 내용은 넣지 않습니다.
  가격 · 날짜 · 공연장 주소 · 관람 연령 값도 따로 들어가므로 빼고, 없으면 [].
- 이미지 · 글에 없는 값을 지어내지 않습니다. 읽히지 않는 글자는 추측하지 않습니다."""


class Llm:
    def __init__(self, api_key: str | None, model: str, client=None, sleep=time.sleep, gap: float = GAP):
        self.client = client or genai.Client(api_key=api_key)
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0          # 생각 토큰 포함
        self.exhausted = False          # 하루 한도를 다 씀 · 서버가 계속 응답 못 함 → 이번 실행의 남은 호출은 건너뜀
        self.stop_reason = "quota"      # exhausted 의 이유: quota · unavailable
        self._sleep = sleep
        self._gap = gap
        self._last: float | None = None

    def extract(self, system: str, content: str | list, schema: dict) -> dict:
        """content: 글 또는 [이미지 Part …, 글 Part]"""
        r = self._generate(content, types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level="HIGH"), max_output_tokens=MAX_TOKENS))
        try:
            return json.loads(r.text or "")
        except json.JSONDecodeError:
            raise LlmSkip("bad_json") from None

    def research(self, system: str, content: str, tools: list, turns: int = TURNS) -> str:
        """함수 호출 반복 — tools: [(선언 {name, description, parameters(JSON 스키마)}, 함수(**인자) → dict)].
        모델이 더 부르지 않으면 마지막 글을 돌려줍니다. 함수가 LlmSkip 을 내면 조사를 멈춥니다 (예: 검색 한도 소진)."""
        run = {d["name"]: fn for d, fn in tools}
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name=d["name"], description=d["description"], parameters_json_schema=d["parameters"]) for d, _ in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level="HIGH"), max_output_tokens=MAX_TOKENS)
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=content)])]
        for _ in range(turns):
            r = self._generate(contents, config)
            calls = r.function_calls or []
            if not calls:
                return r.text or ""
            contents.append(r.candidates[0].content)          # 생각 서명(thought signature)까지 그대로 돌려줘야 이어짐
            contents.append(types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                id=c.id, name=c.name, response=_call(run, c))) for c in calls]))
        raise LlmSkip("turns")

    def _generate(self, contents, config):
        if self.exhausted:
            raise LlmSkip(self.stop_reason)
        for attempt in range(TRIES):
            self._wait()
            try:
                r = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as e:
                if e.code == 429 and "PerDay" in str(e):
                    self.exhausted = True
                    log.warning("llm_daily_quota_exhausted model=%s", self.model)
                    raise LlmSkip("quota") from None
                if e.code in (429, 500, 503, 504) and attempt < TRIES - 1:
                    self._sleep(_retry_after(e) or 15 * 2 ** attempt)
                    continue
                if e.code == 429:
                    self.exhausted = True
                    raise LlmSkip("quota") from None
                if e.code in (500, 503, 504):
                    self.exhausted, self.stop_reason = True, "unavailable"
                    log.warning("llm_unavailable model=%s error=%s", self.model, str(e)[:300])
                    raise LlmSkip("unavailable") from None
                if e.code == 400:                      # 그 요청만 문제 (이미지 · 글) — 실행 전체를 멈추지 않음
                    log.warning("llm_rejected model=%s error=%s", self.model, str(e)[:300])
                    raise LlmSkip("rejected") from None
                raise                                  # 401 · 403 등 설정 문제 — 실행을 멈추고 기록
            self._count(r)
            if r.prompt_feedback is not None and r.prompt_feedback.block_reason:
                raise LlmSkip("blocked")
            reason = r.candidates[0].finish_reason if r.candidates else None
            if reason != types.FinishReason.STOP:       # MAX_TOKENS · SAFETY · RECITATION …
                raise LlmSkip((reason.name if reason else "no_candidate").lower())
            return r
        raise AssertionError("unreachable")

    def _wait(self):
        if self._last is not None:
            gap = time.monotonic() - self._last
            if gap < self._gap:
                self._sleep(self._gap - gap)
        self._last = time.monotonic()

    def _count(self, r):
        u = r.usage_metadata
        if u is None:
            return
        self.input_tokens += u.prompt_token_count or 0
        self.output_tokens += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)

    def artist_genres(self, name: str, aliases: list[str], albums: list[str], label: str | None) -> list[str]:
        content = json.dumps({"name": name, "aliases": aliases, "albums": albums[:15], "label": label}, ensure_ascii=False)
        genres = self.extract(ARTIST_SYSTEM, content, ARTIST_SCHEMA)["genres"]
        out = []
        for g in genres:                      # 대소문자는 보장되지 않음 (structured outputs 안내)
            g = g.upper()
            if g in GENRES and g not in out:
                out.append(g)
        return out[:3]

    def concert(self, facts: dict, images: list[bytes] | None = None) -> dict:
        """facts: {detail, venue, knownArtists} · images: JPEG 조각 (포스터 → 소개 이미지 순, images.slices)"""
        text = json.dumps(facts, ensure_ascii=False)
        if not images:
            return self.extract(CONCERT_SYSTEM, text, CONCERT_SCHEMA)
        parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images]
        return self.extract(CONCERT_SYSTEM, [*parts, types.Part.from_text(text=text)], CONCERT_SCHEMA)


def _call(run: dict, call) -> dict:
    """모델이 부른 함수 실행 — 모르는 함수 · 잘못된 인자는 오류를 모델에게 돌려줌 (LlmSkip 은 위로)"""
    fn = run.get(call.name)
    if fn is None:
        return {"error": f"없는 도구: {call.name}"}
    try:
        return fn(**(call.args or {}))
    except TypeError as e:
        return {"error": f"인자가 맞지 않음: {e}"}


def _retry_after(e) -> float | None:
    """429 오류의 retryDelay ('23s') — 없으면 None"""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) + 1 if m else None
