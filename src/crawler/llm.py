"""Gemini 전처리 (Anthropic Haiku -> Gemini 무료 등급, 기본 gemini-3.5-flash-lite, CRAWLER_MODEL 로 바꿈).

- 구조화 출력(response_json_schema)으로 정해진 모양, 값만 받습니다. 모든 필드를 필수로 두고 "모름" 은 "", -1, NONE, UNKNOWN 으로 받아
  코드에서 null 로 바꿉니다 (Haiku 때 스키마 그대로, 실호출로 enum, additionalProperties 받는 것 확인).
- 생각 수준(thinking_level)은 MEDIUM - 정확도 우선으로 HIGH 였으나 밤 HIGH 만 503 "high demand" 가 몇 시간 이어져
  (같은 요청이 LOW, MEDIUM 은 응답) 사용자 결정으로 내림. 생각 토큰도 max_output_tokens 에 들어가므로 넉넉히 둡니다.
- 웹 조사(research)는 우리 함수(웹 검색, 페이지 열기)를 모델이 고르는 함수 호출 반복 - 자동 함수 호출은 끄고 직접 돌립니다
  (도구 결과를 기록해 지어낸 출처를 거르기 위해, sources/websearch.py).
- 무료 등급 한도: 분당 요청 수를 넘지 않게 호출 사이를 띄우고, 429 는 API 가 알려 준 시간만큼 쉬었다가 다시.
  하루 한도(PerDay)를 다 쓰면 이번 실행의 남은 호출은 모두 건너뜁니다 (LlmSkip("quota") - 다음 실행에 이어서).
  서버 오류(500, 503, 504)는 1, 2, 4, 8분 쉬며 다시 부르고, 그래도 계속되면 20분 동안 호출을 건너뜁니다 (LlmSkip("unavailable"), 503 "high demand" 가 20분 이어져 공연 단계 전체가 멈췄음 - 다른 단계는 끝까지 돌고 남은 것은 다음 실행에).
  20분이 지나면 한 번 다시 불러 보고(1분 쉬고 한 번 더), 되면 이어서, 안 되면 또 20분 (, 새벽 503 한 번에 실행 끝까지
  Gemini 를 안 불러 아티스트 168팀을 못 물었음).
"""
import json
import logging
import re
import time

from google import genai
from google.genai import errors, types

from .enums import CONCERT_TYPES, GENRES, REGIONS
from .keys import HANGUL, norm

log = logging.getLogger(__name__)

MAX_TOKENS = 16000     # 생각 토큰 포함
GAP = 6.5              # 호출 사이 최소 간격(초) - 무료 등급 분당 10건 안팎 (공식 수치 비공개, 사용자 측정 보고 기준)
TRIES = 4              # 분당 한도(429) 때 다시 부르는 횟수
SERVER_WAITS = (60, 120, 240, 480)   # 서버 오류(500, 503, 504) 때 쉬는 시간(초) - 503 혼잡이 20분 띄엄띄엄 이어짐 (느려도 괜찮음, )
COOLDOWN = 1200        # 서버 오류가 그래도 이어지면 이만큼(초) 부르지 않음 -> 그 뒤 다시 불러 봄
TURNS = 16             # 웹 조사 한 번에 모델이 도구를 부르는 최대 차례


class LlmSkip(Exception):
    """거절, 길이 초과, 한도 소진, 요청 거절(400) - 그 항목은 이번에 건너뜀 (다음 실행에 다시)"""


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


THINKING = "MEDIUM"

ARTIST_SCHEMA = _obj({
    "relevance": {"type": "string", "enum": ["INDIE", "MAINSTREAM"]},
    "genres": {"type": "array", "items": {"type": "string", "enum": GENRES}},
    "koreanName": {"type": "string", "description": "한국에서 쓰는 한글 활동명 · 모르면 빈 문자열"},
})

PICK_SYSTEM = """공연 출연진 이름 하나가 MusicBrainz 에서 같은 이름의 한국 아티스트 여럿과 맞습니다.
공연 정보(concert: 제목 · 공연장 · 소개 · 함께 나오는 팀)와 후보(candidates: 설명(disambiguation) · 종류 · 성별 · 별칭 · 발매작)를 보고
이 공연에 나오는 사람이 어느 후보인지 고르세요.
- 공연 정보와 맞는 후보가 확실히 하나일 때만 그 id 를 돌려줍니다. 둘 이상이 그럴듯하거나 아무도 맞지 않으면 빈 문자열.
- 근거: 소개에 나온 역할(싱어송라이터 · 밴드 · 배우 등) · 성별 · 소속 · 기획사 · 공연장 규모 · 함께 나오는 팀 · 발매작.
- 그룹 멤버 · 배우 · 세션 연주자 · 작사가처럼 그 공연의 주인공과 다른 활동을 하는 후보는 공연 정보가 그 활동을 말할 때만 고릅니다."""

PICK_SCHEMA = _obj({"id": {"type": "string", "description": "고른 후보의 id · 확실하지 않으면 빈 문자열"}})

# 소개 글 - 문장마다 근거 출처 번호 (코드가 숫자, 제목, 개인정보를 출처와 대조, describe.py)
DESCRIBE_SCHEMA = _obj({"sentences": {"type": "array", "items": _obj({
    "text": {"type": "string"},
    "source": {"type": "integer", "description": "이 문장의 근거가 된 sources 의 번호(no)"},
})}})

CONCERT_SCHEMA = _obj({
    "relevance": {"type": "string", "enum": ["INDIE", "MAINSTREAM", "HIPHOP", "NOT_CONCERT"]},
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
    "performers": {"type": "array", "items": _obj({"name": {"type": "string"}, "intro": {"type": "string"}}),
                   "description": "11/26 출연진 소개 (이미지 · 글에 소개가 있는 사람 · 팀만)"},
    "program": {"type": "array", "items": {"type": "string"}, "description": "11/26 프로그램 · 곡목 · 공연 구성 (적힌 순서)"},
    "timetable": {"type": "array", "items": {"type": "string"}, "description": "11/26 날짜 · 무대별 출연 순서와 시각"},
    "events": {"type": "array", "items": {"type": "string"}, "description": "11/26 관람객 대상 부대 행사"},
    "notices": {"type": "array", "items": {"type": "string"}},
})

# 굿즈, 음반 판본 상세 설명 (detail.py) - 공식 판매 페이지의 글, 이미지에서 사실만
PRODUCT_SCHEMA = _obj({
    "isProductPage": {"type": "boolean", "description": "이 상품을 파는 · 소개하는 페이지인지"},
    "summary": {"type": "string"},
    "facts": {"type": "array", "items": {"type": "string"}},
    "care": {"type": "array", "items": {"type": "string"}},
    "detailImages": {"type": "array", "items": {"type": "integer"}, "description": "상세 설명 이미지 번호 (1부터)"},
})
PRODUCT_SYSTEM = """아티스트 공식 굿즈 또는 음반 판본 하나의 판매 · 소개 페이지(글과 이미지)를 indig 상품 상세 설명으로 정리합니다. 결과는 관리자가 확인한 뒤 올라갑니다.
입력: product(이름 · 종류 · 아티스트) · page(페이지 글) · 이미지(페이지의 큰 이미지 · "이미지 N" 글 다음에 그 이미지 · 긴 이미지는 위에서부터 잘라 이어짐).
- isProductPage: 이 페이지가 바로 이 상품(이름 · 색 · 판이 맞는 것)을 파는 · 소개하는 페이지면 true. 목록 · 홈 · 다른 상품 페이지면 false 이고 나머지는 비웁니다.
- summary: 상품을 소개하는 1~2문장. 페이지 · 이미지에 적힌 사실만 직접 씁니다. 홍보 문구 · 꾸밈말 · 가격 · 배송 · 할인 · 적립 이야기는 넣지 않습니다.
- facts: 소재 · 실측 사이즈 · 구성품 · 색 · 인쇄 방식 · 판 정보(한정 수량 · 넘버링 · 컬러반 · 수록곡 수) · 제조국처럼 확인되는 사실을 한 줄씩 최대 12. "소재: 면 100%" 처럼 항목 이름을 앞에.
- care: 세탁 · 보관 · 취급 주의를 한 줄씩 최대 5. 없으면 [].
- detailImages: 상세 설명에 쓸 이미지 번호 (상품 사진 · 실측표 · 구성 안내). 로고 · 배너 · 다른 상품 · 아이콘 · 모델 얼굴만 나온 사진은 넣지 않습니다.
- 페이지 · 이미지에 없는 값을 지어내지 않습니다. 읽히지 않는 글자는 추측하지 않습니다. 개인 정보는 쓰지 않습니다."""

# indig 가 다루는 아티스트 - 공연, 아티스트 판단이 같이 씀 (사람이 읽는 판(서비스 설명)과 이유는 docs/v2_인디_아티스트_기준.md)
INDIE_RULES = """indig 가 다루는 아티스트:
- 다룸: 스스로 또는 인디 레이블 · 작은 기획사에서 음악을 만들고 클럽 · 라이브홀 · 소극장 · 인디 페스티벌 무대에 서는 밴드 · 싱어송라이터 · 프로듀서 · 연주자.
  록 · 팝 · 포크 · 일렉트로닉 · 재즈 · R&B 등 장르는 넓게 봅니다. 유명해지거나 큰 무대에 서도 이렇게 활동하면 다룹니다.
- 다루지 않음(대중): 아이돌 그룹과 그 멤버 · 대형 기획사 소속 가수 · 방송 · 오디션 프로그램 중심 가수 · 트로트 가수 · 방송 중심 발라드 가수 · 배우 · 방송인의 음악 활동.
- 다루지 않음(힙합): 힙합 · 랩이 중심인 아티스트."""

ARTIST_SYSTEM = """indig 에서 다룰 아티스트인지와 장르 · 한글 활동명을 정합니다.
""" + INDIE_RULES + """
- relevance: 다루는 아티스트면 INDIE, 대중 쪽이면 MAINSTREAM. 아이돌 · 방송 · 오디션 중심으로 활동하는 대중 가수인 것이 확실할 때만 MAINSTREAM — 잘 모르는 팀은 INDIE
  (공연 · 발매로 들어온 작은 팀은 대개 인디). 레이블 · 유통사 이름만으로 정하지 않습니다 (큰 회사가 유통만 하거나 재즈 · 클래식 레이블을 따로 둠).
  재즈 · 클래식 연주자는 다룹니다. 같은 이름의 다른 사람과 헷갈리지 않게 앨범 · 레이블로 확인합니다. 힙합은 장르(HIPHOP)로 따로 거릅니다.
- genres: 정해진 목록에서 최대 3개, 대표적인 것부터. 이 아티스트를 확실히 알지 못하면 빈 배열 — 이름만 보고 짐작하지 않습니다.
  앨범 제목 · 레이블은 같은 이름의 다른 아티스트와 구분하는 참고용입니다.
- koreanName: 한국 음원 사이트 · 공연 소개에서 이 아티스트를 부르는 한글 활동명 (예: SE SO NEON → 새소년, ADOY → 아도이, 10cm → 십센치).
  별칭(aliases)이나 웹 검색 결과(webResults)에 이 아티스트의 한글 표기로 적힌 것 중에서 고릅니다. 영어 이름을 소리 나는 대로 적은 표기(예: 스키니브라운)도 거기 보이면 씁니다.
  별칭 · 검색 결과에 한글 표기가 없으면 "" — 직접 만들지 않습니다.
  본명(legalNames · 검색 결과의 '본명') · 예전 이름 · 다른 프로젝트 이름 · 숫자나 기호를 글자로 풀어 읽은 표기(예: 포티 포)는 쓰지 않습니다.
  다만 본명으로 활동하면(name 이 legalNames 에 있음 — Moon Sujin → 문수진) 한글 본명이 활동명입니다.
  검색 결과에는 같은 이름의 다른 사람이나 함께 적힌 다른 아티스트도 나옵니다 — 앨범 · 레이블로 이 아티스트를 가리키는 표기인지 확인합니다.
결과는 관리자가 확인한 뒤 서비스에 들어갑니다."""

DESCRIBE_RULES = """
- 한국어 합니다체(…입니다 · …했습니다)로 씁니다.
- 출처(sources)에 적힌 사실만 씁니다. 출처에 없는 내용 · 추측 · 평가하는 꾸밈말(최고의 · 독보적인 · 감성적인 · 주목받는 등)은 쓰지 않습니다.
  출처 문장을 그대로 옮기지 말고 사실만 골라 새 문장으로 씁니다.
- 이런 것은 쓰지 않습니다: 본명 · 생년월일 · 나이 · 태어난 곳 · 학교 · 가족 · 연애 · 결혼 · 병역 · 건강 · 사건 · 논란 · 소송.
- 출처가 이 아티스트(이름 · 앨범 · 레이블로 확인)에 대한 글이 아니면 쓰지 않습니다 — 같은 이름의 다른 사람 · 다른 작품이 흔합니다.
- 문장마다 근거가 된 출처 번호(source)를 붙입니다. 쓸 만한 사실이 모자라면 문장 수를 줄이고, 없으면 빈 목록을 돌려줍니다 — 관리자가 직접 씁니다.
결과는 관리자가 출처와 함께 확인한 뒤 서비스에 들어갑니다."""

BIO_SYSTEM = """indig(한국 인디 음악 플랫폼) 아티스트 상세 화면에 들어갈 짧은 소개 글을 씁니다.
- 3~4문장 · 모두 합쳐 300자 안쪽. 쓸 만한 것: 언제 · 어디서 결성(데뷔)했는지, 구성(몇 인조 · 솔로), 음악 장르, 대표 앨범 · 곡, 레이블, 수상.
- artist 는 MusicBrainz 의 값(이름 · 별칭 · 앨범 · 레이블)으로, 출처가 이 아티스트에 대한 것인지 확인하는 데 씁니다.""" + DESCRIBE_RULES

ALBUM_SYSTEM = """indig(한국 인디 음악 플랫폼) 앨범 상세 화면에 들어갈 짧은 소개 글을 씁니다.
- 2~3문장 · 모두 합쳐 200자 안쪽. 쓸 만한 것: 몇 번째 앨범인지, 언제 냈는지, 어떤 곡 · 주제를 담았는지, 수상 · 기록.
- album 은 MusicBrainz 의 값(제목 · 종류 · 발매일 · 트랙 수)입니다. 0번 출처(facts)에도 같은 값이 있습니다.
  0번 출처만으로는 쓰지 않습니다 — 다른 출처에 이 앨범 이야기가 있을 때 그 사실을 중심으로 쓰고, 날짜 · 트랙 수는 0번에서 보탭니다.""" + DESCRIBE_RULES

CONCERT_SYSTEM = """KOPIS 공연 상세(JSON)와 그 공연의 포스터 · 소개 이미지를 indig 공연 후보로 정리합니다. indig 는 한국 인디 음악(밴드 · 싱어송라이터 · 인디 레이블) 플랫폼이고, 결과는 관리자가 확인한 뒤 등록됩니다.
입력: detail(KOPIS 상세) · venue(공연시설 API 의 주소 · 좌석) · knownArtists(우리가 아는 아티스트 이름 중 이 공연 글에 보이는 것) · 이미지(포스터 다음에 소개 이미지, 긴 이미지는 위에서부터 잘라 순서대로).
- relevance: 아래 기준으로 다루는 아티스트의 음악 공연이면 INDIE. 아이돌 · 트로트 · 대형 기획사 가수 · 방송사 공연 · 클래식 · 뮤지컬은 MAINSTREAM.
  힙합 · 랩이 중심인 공연(힙합 아티스트의 단독 공연 · 힙합 페스티벌 · 힙합 파티)은 HIPHOP. 음악 공연이 아니면 NOT_CONCERT. 애매하면 INDIE.
""" + INDIE_RULES + """
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
- 11/26 소개 이미지에는 소개 글보다 자세한 내용(출연진 소개 · 곡목 · 타임테이블 · 부대 행사)이 들어 있는 경우가 많습니다. 아래 칸에 빠짐없이 옮기되, 문장은 원문을 옮기지 말고 사실만 직접 씁니다.
  홍보 문구 · 꾸밈말(최고 · 가장 · 압도적 · 감동 · 힐링 · 레전드 같은 말)은 옮기지 않습니다. summary 도 같습니다.
- performers: 이미지 · 글에 출연진(팀 · 연주자 · 게스트) 소개가 있으면 그 사람 · 팀마다 name 과 intro(1~2문장 · 경력 · 대표작 · 활동처럼 적힌 사실만). 최대 12.
  intro 는 장르 · 구성(몇 인조 · 악기) · 활동 · 대표작 · 수상 · 소속 악단처럼 확인되는 사실로 씁니다. 학교 · 학위 · 졸업은 쓰지 않습니다. 사실 없이 꾸밈말뿐이면 그 사람은 넣지 않습니다.
  name 은 artistNames 와 같은 표기로. 소개가 없는 사람은 넣지 않고, 생년월일 · 학교 · 가족 같은 개인 정보는 쓰지 않습니다. 없으면 [].
- program: 프로그램 · 곡목(셋리스트) · 공연 구성(1부 · 2부 · 인터미션 등)이 적혀 있으면 적힌 순서대로 한 줄씩 (곡 제목 · 작곡가 표기는 그대로). 최대 40. 없으면 [].
- timetable: 여러 날 · 여러 무대 공연의 날짜별 · 무대별 출연 순서와 시각이 적혀 있으면 한 줄씩 "날짜 무대 시각 팀" (예: "12/20(토) MAIN STAGE 15:00 실리카겔"). 최대 80. 없으면 [].
- events: 관람객이 참여하는 부대 행사(사인회 · 하이터치 · 포토존 · 공연장 MD 판매 · 관객 이벤트)를 한 줄씩 최대 6개. 예매처 할인 · 예매 이벤트 · 추첨 경품 응모 방법은 넣지 않습니다. 없으면 [].
- notices: 공연장에서 관람객이 알아야 할 것(입장 시작 시각 · 입장 순서 · 스탠딩/지정석 · 관람 연령 확인 방법 · 반입 금지 물품 · 촬영 규정 · 물품 보관 등)을 한 줄씩 최대 8개.
  티켓은 indig 가 직접 팔고 약관도 따로 있으므로 티켓 수령 · 배송 · 취소 · 환불 · 대리 수령 · 예매 매수 · 예매 아이디 · 할인 · 예매처 · 고객센터 · 문의처처럼 예매처에 따라 다른 내용은 넣지 않습니다.
  가격 · 날짜 · 공연장 주소 · 관람 연령 값도 따로 들어가므로 빼고, 없으면 [].
- 이미지 · 글에 없는 값을 지어내지 않습니다. 읽히지 않는 글자는 추측하지 않습니다."""


class Llm:
    def __init__(self, api_key: str | None, model: str, client=None, sleep=time.sleep, gap: float = GAP, clock=time.monotonic):
        self.client = client or genai.Client(api_key=api_key)
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0          # 생각 토큰 포함
        self.stop_reason = "quota"      # exhausted 의 이유: quota, unavailable
        self._quota = False             # 하루 한도를 다 씀 -> 이번 실행의 남은 호출은 건너뜀
        self._down_until: float | None = None   # 서버가 계속 응답 못 함 -> 이 시각(clock)까지 건너뛰고, 지나면 다시 불러 봄
        self._sleep = sleep
        self._clock = clock
        self._gap = gap
        self._last: float | None = None

    @property
    def exhausted(self) -> bool:
        """지금 부르면 안 됨 - 하루 한도(이번 실행 끝까지), 서버가 계속 응답 못 함(COOLDOWN 동안)"""
        return self._quota or (self._down_until is not None and self._clock() < self._down_until)

    def cooldown_left(self) -> float | None:
        """쉬는 중이면 남은 초 (0 = 지금 불러도 됨), 하루 한도면 None (이번 실행에는 다시 못 부름)"""
        if self._quota:
            return None
        if self._down_until is None:
            return 0.0
        return max(0.0, self._down_until - self._clock())

    def wait(self, seconds: float):
        self._sleep(seconds)

    def extract(self, system: str, content: str | list, schema: dict) -> dict:
        """content: 글 또는 [이미지 Part ..., 글 Part]"""
        r = self._generate(content, types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=THINKING), max_output_tokens=MAX_TOKENS))
        try:
            return json.loads(r.text or "")
        except json.JSONDecodeError:
            raise LlmSkip("bad_json") from None

    def research(self, system: str, content: str, tools: list, turns: int = TURNS) -> str:
        """함수 호출 반복 - tools: [(선언 {name, description, parameters(JSON 스키마)}, 함수(**인자) -> dict)].
        모델이 더 부르지 않으면 마지막 글을 돌려줍니다. 함수가 LlmSkip 을 내면 조사를 멈춥니다 (예: 검색 한도 소진)."""
        run = {d["name"]: fn for d, fn in tools}
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name=d["name"], description=d["description"], parameters_json_schema=d["parameters"]) for d, _ in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=THINKING), max_output_tokens=MAX_TOKENS)
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
        waits = SERVER_WAITS[:1] if self._down_until is not None else SERVER_WAITS   # 쉬고 난 뒤 다시 불러 볼 때는 1분만 더
        limited = busy = 0                             # 분당 한도, 서버 오류로 다시 부른 횟수
        while True:
            self._wait()
            try:
                r = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as e:
                if e.code == 429 and "PerDay" in str(e):
                    self._quota, self.stop_reason = True, "quota"
                    log.warning("llm_daily_quota_exhausted model=%s", self.model)
                    raise LlmSkip("quota") from None
                if e.code == 429 and limited < TRIES - 1:
                    self._sleep(_retry_after(e) or 15 * 2 ** limited)
                    limited += 1
                    continue
                if e.code in (500, 503, 504) and busy < len(waits):
                    self._sleep(waits[busy])
                    busy += 1
                    continue
                if e.code == 429:
                    self._quota, self.stop_reason = True, "quota"
                    raise LlmSkip("quota") from None
                if e.code in (500, 503, 504):
                    self._down_until, self.stop_reason = self._clock() + COOLDOWN, "unavailable"
                    log.warning("llm_unavailable model=%s retry_in=%ss error=%s", self.model, COOLDOWN, str(e)[:300])
                    raise LlmSkip("unavailable") from None
                if e.code == 400:                      # 그 요청만 문제 (이미지, 글) - 실행 전체를 멈추지 않음
                    log.warning("llm_rejected model=%s error=%s", self.model, str(e)[:300])
                    raise LlmSkip("rejected") from None
                raise                                  # 401, 403 등 설정 문제 - 실행을 멈추고 기록
            if self._down_until is not None:
                log.info("llm_available_again model=%s", self.model)
                self._down_until = None
            self._count(r)
            if r.prompt_feedback is not None and r.prompt_feedback.block_reason:
                raise LlmSkip("blocked")
            reason = r.candidates[0].finish_reason if r.candidates else None
            if reason != types.FinishReason.STOP:       # MAX_TOKENS, SAFETY, RECITATION ...
                raise LlmSkip((reason.name if reason else "no_candidate").lower())
            return r

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

    def artist_info(self, name: str, aliases: list[str], albums: list[str], label: str | None,
                    legal_names: list[str] = (), web: list[str] = ()) -> dict:
        """{relevance(INDIE, MAINSTREAM), genres, korean_name} - 한글 활동명은 근거에 있을 때만 (아니면 None): 한글이 들어 있고 본명이 아니며(본명으로 활동하면 본명도),
        별칭 중 하나이거나 웹 검색 결과 한 건(제목, 본문 조각)에 영어 이름과 함께 보여야 함
        (근거 없이 물으니 Royal 44 -> '로열포티포' 를 지어냄, 실제 표기는 로얄44)"""
        payload = {"name": name, "aliases": aliases, "legalNames": list(legal_names), "albums": albums[:15], "label": label}
        if web:
            payload["webResults"] = list(web)
        content = json.dumps(payload, ensure_ascii=False)
        result = self.extract(ARTIST_SYSTEM, content, ARTIST_SCHEMA)
        genres = []
        for g in result["genres"]:            # 대소문자는 보장되지 않음 (structured outputs 안내)
            g = g.upper()
            if g in GENRES and g not in genres:
                genres.append(g)
        korean = (result.get("koreanName") or "").strip()[:100]
        k = norm(korean)
        legal = {norm(n) for n in legal_names}
        if norm(name) in legal:
            legal = set()                     # 본명으로 활동 (Moon Sujin -> 문수진)
        seen = k in {norm(a) for a in aliases} or any(k in norm(w) and norm(name) in norm(w) for w in web)
        if not HANGUL.search(korean) or k in legal or not seen:
            korean = None
        relevance = "MAINSTREAM" if str(result.get("relevance", "")).upper() == "MAINSTREAM" else "INDIE"
        return {"relevance": relevance, "genres": genres[:3], "korean_name": korean}

    def describe(self, system: str, subject: dict, sources: list[dict]) -> list[dict]:
        """소개 글 - [{text, source}] 그대로 (검사는 describe.checked). subject: {artist: ...} 또는 {album: ...}, sources: [{no, site, title, text}]"""
        content = json.dumps({**subject, "sources": sources}, ensure_ascii=False)
        result = self.extract(system, content, DESCRIBE_SCHEMA)
        return [s for s in result.get("sentences") or [] if isinstance(s, dict)]

    def pick_artist(self, name: str, candidates: list[dict], concert: dict) -> str | None:
        """같은 이름의 한국 아티스트 여럿 중 이 공연에 나오는 사람 - 확실할 때만 id (아니면 None)"""
        content = json.dumps({"name": name, "concert": concert, "candidates": candidates}, ensure_ascii=False)
        picked = str(self.extract(PICK_SYSTEM, content, PICK_SCHEMA).get("id") or "").strip()
        return picked if picked in {c["id"] for c in candidates} else None

    def concert(self, facts: dict, images: list[bytes] | None = None) -> dict:
        """facts: {detail, venue, knownArtists}, images: JPEG 조각 (포스터 -> 소개 이미지 순, images.slices)"""
        text = json.dumps(facts, ensure_ascii=False)
        if not images:
            return self.extract(CONCERT_SYSTEM, text, CONCERT_SCHEMA)
        parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images]
        return self.extract(CONCERT_SYSTEM, [*parts, types.Part.from_text(text=text)], CONCERT_SCHEMA)


    def product_detail(self, product: dict, page: str, images: list[list[bytes]]) -> dict:
        """product: {name, kind, artist}, page: 페이지 글, images: 이미지마다 JPEG 조각 (images.slices) - 번호는 1부터"""
        parts = []
        for n, chunks in enumerate(images, start=1):
            parts.append(types.Part.from_text(text=f"이미지 {n}"))
            parts += [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in chunks]
        text = json.dumps({"product": product, "page": page}, ensure_ascii=False)
        return self.extract(PRODUCT_SYSTEM, [*parts, types.Part.from_text(text=text)] if parts else text, PRODUCT_SCHEMA)


def _call(run: dict, call) -> dict:
    """모델이 부른 함수 실행 - 모르는 함수, 잘못된 인자는 오류를 모델에게 돌려줌 (LlmSkip 은 위로)"""
    fn = run.get(call.name)
    if fn is None:
        return {"error": f"없는 도구: {call.name}"}
    try:
        return fn(**(call.args or {}))
    except TypeError as e:
        return {"error": f"인자가 맞지 않음: {e}"}


def _retry_after(e) -> float | None:
    """429 오류의 retryDelay ('23s') - 없으면 None"""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) + 1 if m else None
