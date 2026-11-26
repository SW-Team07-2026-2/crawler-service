"""수집 → 저장 → 전처리 → 전송.

- 공연: KOPIS 대중음악 목록 · 상세 · 공연시설(주소 · 좌석) + 포스터 · 소개 이미지 → Gemini 가 읽고 후보로 정리 · 인디 여부 판단 → 인디만 Studio 로.
  예매 오픈 시각 · 공지는 소개 이미지에서 읽고(없으면 웹 검색 · 출처가 있어야 받음), 출연진이 멤버 실명이면 팀 이름으로 바꿉니다
  (팀을 하나도 확인하지 못하면 MusicBrainz 멤버 관계로 한 번 더 · 그래도 남은 이름은 unmatchedCast 로 관리자에게).
  상세 설명(마크다운)은 Gemini 소개 · 공지 + KOPIS 원문 값(일정 · 가격 · 공연장 · 주최)으로 만듭니다. 포스터 · 소개 이미지는 KOPIS 주소로 보내고
  Studio 가 승인할 때 우리 버킷으로 옮깁니다. 등록한 공연의 KOPIS 원문이 바뀌면 다시 보내 Studio 가 CHANGED 로 표시합니다.
- 아티스트 · 앨범: 시드 목록 + 인디 공연 출연진 · 인디스트릿 최근 발매 아티스트 이름 → MusicBrainz(앨범 · 트랙 · 레이블) → Gemini 장르 → Studio 로.
  전에 찾은 아티스트는 새벽마다 MusicBrainz id 로 다시 봅니다 (신보 · 바뀐 트랙 · 레이블).
- 11/18 모델 교체: Anthropic Haiku → Gemini(llm.py) · Claude 웹 검색 → Tavily + Scrapling MCP(webtools.py). 하는 일은 같습니다.
- MusicBrainz 밖에서 찾기 (11/18): 인디스트릿(신보) · 김밥레코즈(LP · CD · 카세트) · 웹 검색(신보 · 피지컬 · 공식 굿즈, 7일마다).
  찾은 것은 found 에 쌓고 출처(evidence)를 붙여 후보에 넣습니다. MusicBrainz 값이 있으면 그쪽이 이깁니다(같은 앨범은 MusicBrainz 앨범으로).
  이미지 · 트랙 태그 · 판매가 · 재고는 관리자가 넣는 값이라 null 로 보냅니다 (11/18 — Catalog 는 null 이면 저장된 값을 그대로 둠).
- 소개 글 (11/25): 아티스트 bio · 앨범 description 을 한국어 위키백과(없으면 웹 검색 1번)의 사실로 Gemini 가 쓰고 문장마다 출처와 대조합니다.
  출처가 없으면 빈칸 — 관리자가 승인할 때 씁니다. 저녁 · 점심 실행 모두 (describe).
- 보낸 것은 전부 Studio 에서 관리자 승인을 기다립니다. 같은 아티스트면 Gemini 장르를 다시 묻지 않고, 결과가 그대로면 다시 보내지 않습니다.
"""
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select

from . import db as dbm
from . import describe as desc
from . import images as imgs
from . import llm as llms
from . import notify
from .enums import ARTIST_COMPOSITIONS, ARTIST_GENDERS
from .keys import HANGUL, LATIN, content_hash, norm
from .llm import LlmSkip
from .sources import discogs, gimbab, indistreet, kopis, musicbrainz, wikipedia
from .studio import StudioRejected
from .webtools import SearchQuota

log = logging.getLogger(__name__)

SEND = [  # (type, 경로, 한 요청 최대 건수) — 계약 2장 순서
    ("ARTIST", "/internal/crawl/catalog/artists", 500),
    ("ALBUM", "/internal/crawl/catalog/albums", 500),
    ("CONCERT", "/internal/crawl/concerts", 100),
]
_NONE = {"", "NONE", "UNKNOWN"}
# 11/26 관리자 확인 표시 — 보낼 때 붙임 (저장한 값은 그대로). 아티스트 · 앨범은 evidence(for: identity · check · Studio 가 Catalog 로 보낼 때 뺌),
# 공연은 reviewNotes 에 MARK 로 시작하는 줄. 관리자 화면이 Studio 후보만 보므로 크롤러 상태(판단 전 · 보류 · 뺌)를 여기로 알림
MARK = "[크롤러] "
SKIP_WHY = {"MAINSTREAM": "대중 가수 · 공연", "HIPHOP": "힙합", "NOT_CONCERT": "공연이 아님"}
LLM_WAIT_BUDGET = 3 * 3600   # 11/26 실행마다 Gemini 쉬는 시간(503 COOLDOWN 20분)을 기다려 주는 최대 시간 — 점심(12:30) 실행이 저녁(20:00) 전에 끝나게
MISS_WHY = {"not_found": "한국 아티스트로 못 찾음 (외국 팀 · 표기 차이 · 동명이인)", "no_releases": "MusicBrainz 에 앨범이 없음"}
_SKIP_FOR_LLM = {"poster", "styurls"}  # 이미지는 주소 대신 이미지로 넣음
CONCERT_VERSION = 3                    # 공연 정리 방식(프롬프트 · 입력)이 바뀌면 올림 → 같은 원문도 다시 정리 (11/26 3: 소개 이미지의 출연진 소개 · 곡목 · 타임테이블 · 부대 행사)
CONCERT_REREAD = 60                    # 11/26 방식만 바뀐(원문은 같은) 공연을 한 번 실행에 다시 정리할 수 — 나머지는 다음 실행에 (Gemini 몫을 아티스트 판단에도 남기려고)
KOPIS_HOSTS = {"www.kopis.or.kr", "kopis.or.kr"}   # Studio 가 승인할 때 옮겨 오는 곳 (app.image.import-hosts 와 같음)
MAX_IMAGE_URLS = 20                    # Concert imageUrls 상한
KOPIS_DOWN = 5                         # 공연 상세가 (다시 불러도) 이만큼 연달아 실패하면 KOPIS 가 내려간 것으로 보고 남은 공연은 다음 실행에
MAX_CAST_LOOKUPS = 20                  # 팀을 못 찾은 출연진을 MusicBrainz 에서 볼 최대 수 (공연 한 건)
RECENT = timedelta(days=180)           # 이보다 최근 앨범은 트랙 · 레이블을 7일마다 다시 봄 (그 전은 90일)
WEB_EVERY = timedelta(days=7)          # 아티스트마다 웹 검색은 7일에 한 번
WEB_ARTISTS_PER_RUN = 30               # 한 번 실행에 웹 검색할 아티스트 수 — 나머지는 다음 새벽에 (실행이 너무 길어지지 않게)
TICKET_SEARCHES = 10                   # 한 번 실행에 예매 오픈 찾기에 쓸 검색 크레딧 (11/22 8 → 10) — 나머지는 아티스트 조사에
TICKET_SOON = timedelta(days=7)        # 시작이 이보다 가까운 공연은 예매가 이미 열렸을 때가 많아 예매 오픈 찾기에서 맨 뒤로 (11/25)
SEARCH_NAMES = 3                       # 김밥레코즈 검색어로 쓸 이름 수 (한글 이름 먼저)
MAX_TRIES = 6                          # 다시 하기(retry)에서 이만큼 실패하면 더 하지 않음 — 행은 남겨 둠 (한도로 못 한 것은 세지 않음)
ARTIST_ASK = 2                         # 아티스트마다 Gemini 에 묻는 것 — 11/23 장르 → 장르 + 한글 이름 · 11/24 + 인디 여부 (올리면 모든 아티스트를 한 번 다시 물음)
# 11/25 소개 글 (describe) — 아티스트 bio 3~4문장 · 앨범 description 2~3문장 (한글)
DESCRIBE_VERSION = 1                   # 만드는 방식(프롬프트 · 출처 · 검사)이 바뀌면 올림 → 다시 만듦 (관리자가 쓴 글은 Catalog 가 지킴 · 빈 칸만 채움)
DESCRIBE_ARTISTS = 40                  # 한 번 실행에 Gemini 로 소개 글을 쓸 아티스트 수 (11/25 실제 시험 한 팀 입력 1~2천 · 출력 6백 토큰) — 나머지는 다음 실행에 (새벽 · 점심 모두)
DESCRIBE_ALBUMS = 40                   # 같은 것 · 앨범 (출처가 없는 앨범은 Gemini 를 부르지 않아 세지 않음)
DESCRIBE_SEARCHES = 15                 # 한 번 실행에 소개 글 출처 찾기(위키백과 문서가 없을 때)에 쓸 검색 크레딧
DESCRIBE_AGAIN = timedelta(days=30)    # 출처가 없거나 못 쓴 것은 이만큼 지나 한 번 더
DESCRIBE_LIVE = ("READY", "SENT", "HOLD", "FAILED")   # 소개 글을 쓰는 후보 상태 (뺀 SKIPPED 는 쓰지 않음)


class Pipeline:
    def __init__(self, sessions, http, settings, llm=None, studio=None, searcher=None):
        self.sessions = sessions
        self.http = http
        self.settings = settings
        self.llm = llm
        self.studio = studio
        self.searcher = searcher          # 웹 검색 (sources/websearch.WebSearch) — Gemini · Tavily 키가 있을 때
        self.stats: Counter = Counter()
        self._loaded: set[str] = set()   # 이번 실행에서 본 MusicBrainz id
        self._indistreet: list[dict] = []
        self._contexts: dict[str, dict] = {}   # 11/24 출연진 이름 → 나온 공연 (같은 이름 아티스트가 여럿일 때 고르는 근거)
        self._web_left = WEB_ARTISTS_PER_RUN
        self._reread_left = CONCERT_REREAD
        self._llm_wait_left = LLM_WAIT_BUDGET   # 11/26 이번 실행에 Gemini 쉬는 시간을 더 기다려 줄 수 있는 초
        self._lineup_names: tuple | None = None   # 11/26 보낼 때 공연 라인업 → 아티스트 후보 (send 마다 새로)
        self.failures: dict[str, dict] = {}   # 11/24 알림용 — what → {count, codes: {코드: 수}, samples: [(항목, 내용)] 몇 개}
        self.reviewed: dict[tuple[str, str], str] = {}   # (CONCERT · ARTIST · ALBUM, external_id) → REJECTED · APPROVED · CHANGED (load_reviewed)
        self._articles: dict[str, dict | None] = {}     # 11/25 아티스트 키 → 한국어 위키백과 문서 (이번 실행 · 앨범 소개 글에도 씀)

    def load_reviewed(self):
        """11/24 관리자가 처리한 후보 (Studio GET /internal/crawl/reviewed) — 실행 처음에 한 번.
        거절한 공연 · 아티스트는 다시 찾지 않고(KOPIS 상세 · Gemini · MusicBrainz · 웹 검색), 관리자가 처리한 공연은 예매 오픈을 찾지 않음.
        못 받으면 전처럼 전부 합니다 (실행은 계속)."""
        if self.studio is None:
            return
        try:
            result = self.studio.get("/internal/crawl/reviewed")
        except Exception as e:
            self._fail("reviewed_fetch", "studio", e)
            return
        for type_, field in (("CONCERT", "concerts"), ("ARTIST", "artists"), ("ALBUM", "albums")):
            for row in result.get(field) or []:
                self.reviewed[(type_, row["external_id"])] = row["status"]
        self.stats["reviewed_rejected"] = sum(1 for v in self.reviewed.values() if v == "REJECTED")

    def _rejected(self, type_: str, key: str | None) -> bool:
        return self.reviewed.get((type_, key)) == "REJECTED"

    # ---------- 공연 ----------

    def concerts(self) -> list[str]:
        """KOPIS → 공연 항목. 인디 공연 출연진 이름을 돌려줍니다 (새 아티스트 찾기용)."""
        key = self.settings.kopis_key
        if not key:
            self.stats["concert_no_kopis_key"] += 1
            return []
        if self.llm is None:
            self.stats["concert_no_llm"] += 1
            return []
        start, end = kopis.window(self.settings.concert_days)
        known = self._known_names()
        names: list[str] = []
        misses = 0
        retry = self._retries("CONCERT")    # 지난번에 못 끝낸 공연 → 맨 앞에 (11/23 아웃박스)
        ids = kopis.list_ids(self.http, key, start, end)
        self._drop_unlisted(set(ids))
        for mt20id in [i for i in ids if i in retry] + [i for i in ids if i not in retry]:
            if self._rejected("CONCERT", mt20id):      # 11/24 관리자가 거절한 공연 — 상세 · 정리(Gemini) · 출연진 찾기를 하지 않음
                self.stats["concert_rejected_skip"] += 1
                if mt20id in retry:
                    self._retried("CONCERT", mt20id)
                continue
            try:
                detail = kopis.detail(self.http, key, mt20id)
            except Exception as e:  # 한 건 실패로 실행 전체를 멈추지 않음
                self._fail("concert_fetch", mt20id, e)
                self._retry("CONCERT", mt20id, "detail", e)
                misses += 1
                if misses >= KOPIS_DOWN:   # 다시 불러도 계속 실패 — 남은 공연을 하나씩 기다리지 않음 (실행이 다음 일정까지 늘어지지 않게)
                    self.stats["concert_kopis_down"] += 1
                    break
                continue
            misses = 0
            if not detail:
                if mt20id in retry:
                    self._retried("CONCERT", mt20id)
                continue
            try:
                fac = kopis.facility(self.http, key, detail.get("mt10id"))
            except Exception as e:     # 주소 · 좌석 없이 계속
                self._fail("concert_facility", mt20id, e)
                fac = {}
            venue = _venue(detail, fac)
            with self.sessions() as s:
                item = self._item(s, "CONCERT", mt20id)
                input_hash = content_hash({"v": CONCERT_VERSION, "detail": detail, "venue": venue})
                # 원문이 같으면 다시 정리하지 않음 — 정리(Gemini) · 출연진→팀 조회가 실패해 남은 공연은 원문이 같아도 다시
                same = item is not None and item.input_hash == input_hash and retry.get(mt20id) not in ("llm", "cast")
                if not same and item is not None and mt20id not in retry and item.input_hash in {
                        content_hash({"v": v, "detail": detail, "venue": venue}) for v in range(1, CONCERT_VERSION)}:
                    # 11/26 원문은 그대로고 정리 방식만 바뀜 — 뺀 공연은 다시 보지 않고, 나머지는 실행마다 CONCERT_REREAD 건까지
                    if item.status == "SKIPPED" or self._reread_left <= 0:
                        same = True
                        if item.status != "SKIPPED":
                            self.stats["concert_reread_deferred"] += 1
                    else:
                        self._reread_left -= 1
                        self.stats["concert_reread"] += 1
                if same:
                    self.stats["concert_same_input"] += 1
                    if item.status != "SKIPPED":
                        payload = json.loads(item.payload_json)
                        notes = fit_kopis_dates(detail, payload["concert"])   # 이 검사 전에 정리한 것도 Gemini 없이 고침
                        if notes:
                            payload["concert"]["reviewNotes"] = notes
                            self._upsert(s, "CONCERT", mt20id, payload, input_hash, origin=item.origin)
                            s.commit()
                            self.stats["concert_dates_fixed"] += 1
                        names += payload["concert"]["artistNames"]
                        self._remember_context(payload["concert"])
            if same:
                if mt20id in retry:
                    self._retried("CONCERT", mt20id)         # 상세를 못 받았던 공연 — 이번엔 받음
                continue
            if getattr(self.llm, "exhausted", False):   # 하루 한도를 다 씀 · 서버가 계속 응답 못 함 — 이미지를 받지 않고 다음 실행에
                reason = getattr(self.llm, "stop_reason", "quota")
                self.stats[f"concert_llm_{reason}"] += 1
                self._retry("CONCERT", mt20id, "llm", reason, count=False)   # 한도 탓 — 실패 횟수로 세지 않음
                continue
            facts = {"detail": {k: v for k, v in detail.items() if k not in _SKIP_FOR_LLM}, "venue": venue,
                     "knownArtists": _mentioned(known, detail)}
            try:
                c = self.llm.concert(facts, self._concert_images(mt20id, detail))
            except LlmSkip as e:
                self.stats[f"concert_llm_{e}"] += 1
                self._retry("CONCERT", mt20id, "llm", e, count=str(e) not in ("quota", "unavailable"))
                continue
            indie = c.get("relevance") == "INDIE"
            cast_failed = False
            if indie and c.get("unmatchedCast") and not c.get("artistNames"):
                # 포스터 · 글로 팀을 하나도 확인하지 못했을 때만 MusicBrainz 멤버 관계로 (확인한 팀이 있으면 남은 이름은 대개 그 팀 멤버 · 세션)
                c["artistNames"], c["unmatchedCast"], cast_failed = self._teams_for(c["unmatchedCast"])
            concert = to_concert(detail, c, venue)
            if concert["reviewNotes"]:
                self.stats["concert_dates_fixed"] += 1
            payload = {"source": "kopis", "external_id": mt20id, "concert": concert}
            with self.sessions() as s:
                self._upsert(s, "CONCERT", mt20id, payload, input_hash,
                             origin=concert.get("sourceUrl") or f"kopis:{mt20id}",
                             status="READY" if indie else "SKIPPED", note=None if indie else c.get("relevance"))
                s.commit()
            if cast_failed:
                self._retry("CONCERT", mt20id, "cast", "MusicBrainz 출연진 → 팀 조회 실패")   # 원문이 같아도 다음에 다시 정리
            elif mt20id in retry:
                self._retried("CONCERT", mt20id)
            self.stats["concert_indie" if indie else "concert_skipped"] += 1
            if indie:
                names += concert["artistNames"]
                self._remember_context(concert)
        self._tickets()
        return names

    def _remember_context(self, concert: dict):
        context = {"title": concert.get("title"), "venueName": concert.get("venueName"), "startsAt": concert.get("startsAt"),
                   "lineup": concert.get("artistNames"), "description": (concert.get("description") or "")[:1200]}
        for n in concert.get("artistNames") or []:
            self._contexts.setdefault(norm(n), context)

    def _pick(self, name: str, candidates: list[dict]) -> str | None:
        """11/24 같은 이름의 한국 아티스트가 여럿 (정우: NCT · 인디 싱어송라이터 · 배우 · TREASURE) — 이 이름이 나온 공연을 보고 Gemini 가 고름.
        나온 공연을 모르면(시드) · Gemini 가 못 고르면 None = 만들지 않음. Gemini 한도 · 503 이면 LlmSkip → 다음 실행에 다시 (artists 의 retry)."""
        self.stats["artist_same_name"] += 1
        context = self._contexts.get(norm(name))
        if context is None or self.llm is None:
            self.stats["artist_same_name_unresolved"] += 1
            log.info("crawl_artist_same_name name=%s candidates=%s context=none", name, [c["id"] for c in candidates])
            return None
        if getattr(self.llm, "exhausted", False):
            raise LlmSkip(getattr(self.llm, "stop_reason", "quota"))
        options = [{"id": c["id"], "name": c.get("name"), "disambiguation": c.get("disambiguation"), "type": c.get("type"),
                    "gender": c.get("gender"), "aliases": [a.get("name") for a in c.get("aliases", [])][:6],
                    "releases": [g["title"] for g in musicbrainz.release_groups(self.http, c["id"])][-8:]} for c in candidates]
        chosen = self.llm.pick_artist(name, options, context)
        self.stats["artist_same_name_picked" if chosen else "artist_same_name_unresolved"] += 1
        log.info("crawl_artist_same_name name=%s candidates=%s chosen=%s", name, [c["id"] for c in candidates], chosen)
        return chosen

    def _known_names(self) -> list[tuple[str, str]]:
        """우리가 아는 아티스트 (정규화한 이름 · 별칭 → 대표 이름) — 공연 글에 보이는 것만 Gemini 에 힌트로"""
        with self.sessions() as s:
            keys = dict(s.execute(select(dbm.ArtistKey.key, dbm.ArtistKey.name)).all())
            links = s.execute(select(dbm.ArtistLink.site_id, dbm.ArtistLink.key).where(dbm.ArtistLink.site == "name")).all()
        out = {norm(name): name for name in keys.values()}
        out.update({site_id: keys[k] for site_id, k in links if k in keys and site_id not in out})
        return [(n, name) for n, name in out.items() if len(n) >= 2]

    def _concert_images(self, mt20id: str, detail: dict) -> list[bytes]:
        """포스터 → 소개 이미지 순으로 받아 Gemini 가 읽을 조각으로 (images.slices) — 못 받은 것은 빼고 계속 (출연진 · 예매 공지가 이미지에만 있는 경우가 많음)"""
        out: list[bytes] = []
        for url in [detail.get("poster"), *(detail.get("styurls") or [])]:
            if not _kopis_image(url) or len(out) >= imgs.MAX_SLICES:   # KOPIS 에 올라온 이미지만
                continue
            try:
                out += imgs.slices(self.http.get_bytes("kopis", url), limit=imgs.MAX_SLICES - len(out))
            except Exception as e:
                self._fail("concert_image", f"{mt20id} {url}", e)
        self.stats["concert_image_slices"] += len(out)
        return out

    def _teams_for(self, unmatched: list[str]) -> tuple[list[str], list[str], bool]:
        """팀을 확인하지 못한 출연진(멤버 실명 등) → MusicBrainz "member of band" 로 팀 이름 → (찾은 팀, 그래도 남은 이름, 조회 실패 여부).
        두 사람 이상이 같은 팀을 가리키거나, 그 사람의 팀이 하나뿐일 때만 팀으로 봅니다 (애매하면 남김 — 관리자가).
        조회가 하나라도 실패하면(서버 오류 등) 공연을 다시 하기에 남깁니다 (11/23 — 원문이 같으면 다시 안 보던 것)."""
        names = list(dict.fromkeys(n.strip() for n in unmatched if n and n.strip()))
        bands: dict[str, list[str]] = {}
        failed = False
        for n in names[:MAX_CAST_LOOKUPS]:
            try:
                bands[n] = musicbrainz.bands_of(self.http, n)
            except Exception as e:
                self._fail("concert_cast", n, e)
                failed = True
        support = Counter(b for found in bands.values() for b in set(found))
        teams, left = [], []
        for n in names:
            found = bands.get(n) or []
            shared = [b for b in found if support[b] >= 2]
            pick = shared if shared else (found if len(found) == 1 else [])
            if len(pick) == 1:
                teams.append(pick[0])
                self.stats["concert_cast_to_team"] += 1
            else:
                left.append(n)
        return list(dict.fromkeys(teams)), left, failed

    def _tickets(self):
        """KOPIS 에 일반 예매 오픈 일시가 없는 인디 공연 → 웹 검색. 출처는 후보의 evidence 로 관리자에게.
        11/18 공연 정리와 나눔 — 검색 크레딧이 적어(Tavily 무료 월 1,000) 아직 안 한 공연 중 시작이 가까운 것부터
        TICKET_SEARCHES 안에서만 하고, 못 한 것은 다음 실행에 이어서. 원문(input_hash)이 같으면 한 번만 찾습니다 (ticket://<공연 id> 표시).
        11/25 시작이 7일(TICKET_SOON) 안인 공연은 맨 뒤로 — 예매가 이미 열렸을 때가 많은데 가까운 것부터라 크레딧이 거기에 먼저 갔음."""
        if self.searcher is None:
            return
        with self.sessions() as s:
            done = {p.url: p.body for p in s.scalars(select(dbm.RawPage).where(dbm.RawPage.source == "ticket"))}
            todo = []
            for i in s.scalars(select(dbm.Item).where(dbm.Item.type == "CONCERT", dbm.Item.status.in_(("READY", "SENT")))):
                if ("CONCERT", i.key) in self.reviewed:   # 11/24 관리자가 처리한 공연 — 거절 · 등록(예매 오픈은 관리자가 넣음)
                    continue
                c = json.loads(i.payload_json)["concert"]
                starts = c.get("startsAt")
                if starts and datetime.fromisoformat(starts) < datetime.now(timezone.utc):
                    continue                                                 # 이미 시작한 공연은 찾지 않음
                if c.get("bookingOpenAt") is None and done.get(f"ticket://{i.key}") != i.input_hash:
                    soon = bool(starts) and datetime.fromisoformat(starts) < datetime.now(timezone.utc) + TICKET_SOON
                    todo.append((soon, starts or "9999", i.key))
        first = getattr(self.searcher, "searches", 0)
        for n, (_, _, mt20id) in enumerate(sorted(todo)):
            if getattr(self.searcher, "exhausted", False) or getattr(self.searcher, "searches", 0) - first >= TICKET_SEARCHES:
                self.stats["concert_ticket_deferred"] += len(todo) - n      # 다음 실행에
                return
            with self.sessions() as s:
                item = self._item(s, "CONCERT", mt20id)
                payload, input_hash, origin = json.loads(item.payload_json), item.input_hash, item.origin
            concert = payload["concert"]
            try:
                found = self.searcher.ticket_open({k: concert[k] for k in ("title", "artistNames", "venueName", "startsAt")})
            except LlmSkip as e:
                self.stats[f"concert_ticket_llm_{e}"] += 1
                if str(e) in ("quota", "unavailable", "search_quota"):
                    continue                                                 # 표시하지 않음 → 다음 실행에 다시
                found = None
            except Exception as e:
                self._fail("concert_ticket", mt20id, e)                     # 다음 실행에 다시
                continue
            with self.sessions() as s:
                if found is None:
                    self.stats["concert_ticket_not_found"] += 1
                else:
                    concert["bookingOpenAt"], evidence = found
                    concert["evidence"] = [{"for": "bookingOpenAt", **e} for e in evidence]
                    self._upsert(s, "CONCERT", mt20id, payload, input_hash, origin=origin)   # 바뀐 후보 → 다시 보냄
                    self.stats["concert_ticket_found"] += 1
                marker = f"ticket://{mt20id}"
                marker_hash = hashlib.sha256(marker.encode()).hexdigest()
                page = s.scalar(select(dbm.RawPage).where(dbm.RawPage.url_hash == marker_hash)) or \
                    dbm.RawPage(source="ticket", url=marker, url_hash=marker_hash)
                page.status, page.fetched_at, page.body = 200, dbm.now(), input_hash
                page.content_hash = hashlib.sha256(input_hash.encode()).hexdigest()
                s.merge(page)
                s.commit()

    # ---------- 아티스트 · 앨범 ----------

    def indistreet(self) -> list[str]:
        """인디스트릿 최근 발매 40개 — 이번 실행에서 아티스트마다 맞춰 보고, 아티스트 이름을 돌려줍니다 (새 아티스트 찾기용)"""
        try:
            self._indistreet = indistreet.latest(self.http)
        except Exception as e:
            self._fail("indistreet", indistreet.URL, e)
            return []
        self.stats["indistreet_albums"] += len(self._indistreet)
        return [names[0] for a in self._indistreet for names in a["artists"]]

    def artists(self, names: list[str]):
        seen = set()
        for name in names:
            if not name.strip() or norm(name) in seen:
                continue
            seen.add(norm(name))
            try:
                mb = musicbrainz.find_artist(self.http, name.strip(), pick=lambda found, n=name.strip(): self._pick(n, found))
                if mb is None:
                    self.stats["artist_not_found"] += 1   # 동명이인 · 표기 차이로 애매하면 만들지 않음
                    log.info("crawl_artist_not_found name=%s", name)
                    self._miss(name.strip(), "not_found")
                elif mb["mbid"] not in self._loaded:     # 다른 이름(한글 · 영어)으로 이미 본 아티스트
                    self._load(mb, name.strip())
            except Exception as e:                       # 서버 오류 등 — 못 찾음(miss)과 달리 실행 끝에 한 번 더 · 남으면 다음 실행 맨 앞에
                self._fail("artist", name, e)
                self._retry("NAME", norm(name)[:200], name.strip(), e)
                continue
            self._retried("NAME", norm(name)[:200])      # 끝남 (찾음 · 못 찾음) — 남아 있던 다시 하기는 지움

    # ---------- 다시 하기 (11/23 아웃박스) ----------

    def retry_names(self) -> list[str]:
        """지난번에 찾다가 실패한 이름 — 새벽 실행이 새 이름보다 먼저 찾음 (runner)"""
        return list(self._retries("NAME").values())

    def retry(self, names: bool = True):
        """실행 끝 — 못 끝낸 것(이번 실행 · 앞선 실행)을 한 번 더. 그래도 남으면 다음 실행 맨 앞에.
        공연은 Gemini 가 하루 한도에 걸려 있으면 하지 않음 (다음 실행에). 판단을 못 한 아티스트도 다시 물음 (_reask · 점심 실행도)."""
        todo = self.retry_names() if names else []
        if todo:
            self.stats["retry_pass_names"] += len(todo)
            self.artists(todo)
        if self._retries("CONCERT") and not getattr(self.llm, "exhausted", False):
            self.stats["retry_pass_concerts"] += 1
            self.concerts()
        self._reask()
        with self.sessions() as s:
            tries = s.scalars(select(dbm.Retry.tries)).all()
        self.stats["retry_left"] = sum(1 for t in tries if t < MAX_TRIES)
        if any(t >= MAX_TRIES for t in tries):
            self.stats["retry_gave_up"] = sum(1 for t in tries if t >= MAX_TRIES)

    def _reask(self):
        """Gemini 가 응답하지 않아 판단(인디 여부 · 장르 · 한글 이름)을 못 한 아티스트를 MusicBrainz id 로 다시 불러 물음.
        11/24 · 11/25 새벽 503 으로 168팀이 판단 없이 남았는데, 점심 실행은 공연만 해서 다음 새벽까지 그대로였음 (11/25).
        MusicBrainz 는 캐시(7일)라 요청이 거의 나가지 않음 · Gemini 가 쉬는 중이면 실행 몫 안에서 기다렸다 이어서 (11/26 · `_llm_usable`) · 하루 한도 · 몫을 넘으면 남은 팀은 다음 실행에."""
        if self.llm is None:
            return
        with self.sessions() as s:
            keys = set(s.scalars(select(dbm.Item.key).where(dbm.Item.type == "ARTIST", dbm.Item.input_hash == "not-judged")))
            links = s.execute(select(dbm.ArtistLink.site_id, dbm.ArtistLink.key).where(
                dbm.ArtistLink.site == "musicbrainz", dbm.ArtistLink.key.in_(keys)).order_by(dbm.ArtistLink.id)).all() if keys else []
        for n, (mbid, _) in enumerate(links):
            if not self._llm_usable():
                self.stats["artist_reask_deferred"] += len(links) - n
                break
            try:
                self._load(musicbrainz.artist(self.http, mbid))
                self.stats["artist_reask"] += 1
            except Exception as e:
                self._fail("artist_reask", mbid, e)
        with self.sessions() as s:
            left = s.scalars(select(dbm.Item.key).where(dbm.Item.type == "ARTIST", dbm.Item.input_hash == "not-judged")).all()
        if left:
            self.stats["artist_not_judged_left"] = len(left)

    def _llm_usable(self) -> bool:
        """Gemini 를 지금 부를 수 있음 — 쉬는 중(503 COOLDOWN)이면 실행 몫(LLM_WAIT_BUDGET) 안에서 기다렸다가 다시.
        11/26 점심 실행이 쉬는 중이라 판단 15팀(156팀 미룸) · 소개 글 0 으로 끝남 → 다시 묻기 · 소개 글만 기다림 (공연은 그대로 다음 실행에).
        하루 한도면 False (이번 실행에는 다시 못 부름)"""
        if not getattr(self.llm, "exhausted", False):
            return True
        left = self.llm.cooldown_left() if hasattr(self.llm, "cooldown_left") else None
        if left is None or left > self._llm_wait_left:
            return False
        self._llm_wait_left -= left
        self.stats["llm_cooldown_waited_s"] += int(left)
        log.info("llm_cooldown_wait seconds=%s budget_left=%s", int(left), int(self._llm_wait_left))
        self.llm.wait(left)
        return not self.llm.exhausted

    def _retries(self, kind: str) -> dict[str, str]:
        """{key: value} — 실패 횟수가 MAX_TRIES 보다 적은 것만, 먼저 실패한 순"""
        with self.sessions() as s:
            rows = s.execute(select(dbm.Retry.key, dbm.Retry.value).where(dbm.Retry.kind == kind, dbm.Retry.tries < MAX_TRIES)
                             .order_by(dbm.Retry.id)).all()
        return dict(rows)

    def _retry(self, kind: str, key: str, value: str, error, count: bool = True):
        with self.sessions() as s:
            dbm.queue_retry(s, kind, key, value, str(error), count)
            s.commit()
        self.stats[f"retry_{kind.lower()}_queued"] += 1

    def _retried(self, kind: str, key: str):
        with self.sessions() as s:
            done = dbm.drop_retry(s, kind, key)
            s.commit()
        if done:
            self.stats[f"retry_{kind.lower()}_done"] += 1

    def _drop_unlisted(self, listed: set[str]):
        """KOPIS 목록에서 빠진 공연(끝남 · 기간 밖)은 더 하지 않음"""
        with self.sessions() as s:
            s.execute(delete(dbm.Retry).where(dbm.Retry.kind == "CONCERT", dbm.Retry.key.not_in(listed)))
            s.commit()

    def _miss(self, name: str, reason: str):
        """만들지 못한 이름을 남겨 30일 동안 새 이름 칸에서 뺌 (runner._new_names)"""
        with self.sessions() as s:
            dbm.remember_miss(s, name, reason)
            s.commit()

    def refresh_known(self):
        """전에 찾은 아티스트를 MusicBrainz id 로 다시 봄 — 시드에 없고 공연에 다시 안 나와도 신보 · 바뀐 값이 들어옵니다.
        요청은 캐시(아티스트 · 앨범 목록 7일)가 지난 것만 나갑니다."""
        with self.sessions() as s:
            links = s.execute(select(dbm.ArtistLink.site_id, dbm.ArtistLink.key).where(dbm.ArtistLink.site == "musicbrainz")
                              .order_by(dbm.ArtistLink.id)).all()
        for mbid, key in links:
            if mbid in self._loaded:
                continue
            if self._rejected("ARTIST", key):            # 11/24 관리자가 거절한 아티스트
                self.stats["artist_rejected_skip"] += 1
                continue
            try:
                self._load(musicbrainz.artist(self.http, mbid))
                self.stats["artist_refreshed"] += 1
            except Exception as e:
                self._fail("artist_refresh", mbid, e)

    def _load(self, mb: dict, asked: str | None = None):
        self._loaded.add(mb["mbid"])
        if self.reviewed:
            with self.sessions() as s:
                known = s.scalar(select(dbm.ArtistLink.key).where(dbm.ArtistLink.site == "musicbrainz", dbm.ArtistLink.site_id == mb["mbid"]))
            if self._rejected("ARTIST", known):          # 11/24 관리자가 거절한 아티스트 — 앨범 · 판단(Gemini) · 웹 검색을 하지 않음
                self.stats["artist_rejected_skip"] += 1
                return
        groups = musicbrainz.release_groups(self.http, mb["mbid"])
        if not groups:
            # 앨범이 하나도 없으면 만들지 않음 — 작사가 · 배우처럼 별칭만 같은 사람이 걸리는 것을 막음 (11/18 이지민 → 이하진)
            self.stats["artist_no_releases"] += 1
            log.info("crawl_artist_no_releases name=%s mbid=%s", asked or mb["name"], mb["mbid"])
            if asked:
                self._miss(asked, "no_releases")
            return
        names = [n for n in dict.fromkeys([mb["name"], *mb["aliases"], asked]) if n]
        with self.sessions() as s:
            key = dbm.artist_key_for(s, mb["name"], {"musicbrainz": mb["mbid"]}, names[1:])
            s.commit()

        recent = (date.today() - RECENT).isoformat()
        releases = {g["rgid"]: musicbrainz.first_release(self.http, g["rgid"], days=7 if (g["released_at"] or "9999") >= recent else 90)
                    for g in groups}
        labels = [releases[g["rgid"]]["label"] for g in reversed(groups) if releases[g["rgid"]]["label"]]
        years = [int(g["released_at"][:4]) for g in groups if g["released_at"]]
        physical = None
        if self.settings.discogs_token and mb.get("discogs"):
            try:
                physical = discogs.physical_by_title(self.http, self.settings.discogs_token, mb["discogs"])
            except Exception as e:   # 피지컬만 건너뜀 (None = Catalog 가 건드리지 않음) — 11/19 지워진 Discogs 아티스트(404)로 아티스트 전체가 빠졌음
                if notify.error_code(e) == "HTTP 404":   # 11/25 지워진 Discogs 아티스트(안치환)는 연결이 없는 것과 같음 — 실행마다 오류로 알리지 않음
                    self.stats["discogs_gone"] += 1
                else:
                    self._fail("discogs", mb["discogs"], e)
        elif self.settings.discogs_token:
            self.stats["discogs_no_link"] += 1          # MusicBrainz 에 Discogs 연결이 없으면 피지컬은 건드리지 않음

        source = {"mb": mb, "albums": [g["title"] for g in groups], "label": labels[0] if labels else None}
        # 인디 여부 · 장르 · 한글 이름은 아티스트가 같으면(id · 이름 · 별칭) 다시 묻지 않음 — 신보 하나로 장르가 흔들려 모든 앨범이 승인 대기로 돌아가는 것을 막음
        identity = content_hash({**{k: mb[k] for k in ("mbid", "name", "aliases")}, "ask": ARTIST_ASK})
        with self.sessions() as s:
            item = self._item(s, "ARTIST", key)
            prev = json.loads(item.payload_json) if item is not None else {"genres": [], "name": None}
            asked_before = item is not None and item.input_hash == identity
            prev_skip = item.error if item is not None and item.status == "SKIPPED" else None   # 전에 뺀 이유 (MAINSTREAM · HIPHOP)
        if asked_before:
            genres, name, judged, skip = prev["genres"], prev["name"], True, prev_skip
        elif self.llm is None:
            genres, name, judged, skip = prev["genres"], prev["name"], False, prev_skip   # 키가 생기면 그때 물음
        else:
            info = self._ask(mb, source)                 # None = 이번엔 못 물음 (거절 · 잘림 · 검색 한도) → 다음 실행에 다시
            if info is None:
                genres, name, judged, skip = prev["genres"], prev["name"], False, prev_skip
            else:
                genres, name, judged = info["genres"], _display_name(mb["name"], info["korean_name"]), True
                skip = _skip_reason(info["relevance"], genres)
        if skip and mb["mbid"] in self.settings.include_ids:   # 11/24 사용자가 넣기로 한 팀 (seeds/include.txt · QWER) — 판단과 상관없이 다룸
            self.stats["artist_included"] += 1
            skip = None
        if skip:
            self.stats[f"artist_skipped_{skip.lower()}"] += 1
        else:
            self._discover(key, mb, names, groups, source["label"])   # 뺀 아티스트는 신보 · 굿즈 · 웹 검색을 하지 않음
        with self.sessions() as s:
            found = self._found(s, key)
            name = name or mb["name"]
            if name != mb["name"]:
                self.stats["artist_korean_name"] += 1
            goods, goods_evidence = _goods(found["GOODS"])
            artist = {
                "source": "crawler", "external_id": key,
                "name": name[:100], "genres": genres,
                "label": source["label"][:100] if source["label"] else None,
                "debut_year": min(years) if years else None,
                "profile_url": None,   # 공개 이미지 버킷이 정해지기 전까지 비움 (계약 3장)
                # 11/22 성별 · 구성 — 모르면 null (Catalog 가 저장된 값을 그대로 두고 관리자가 승인 때 채움) · 목록 밖 값은 승인 때 400 이라 여기서 거름
                "gender": mb.get("gender") if mb.get("gender") in ARTIST_GENDERS else None,
                "composition": mb.get("composition") if mb.get("composition") in ARTIST_COMPOSITIONS else None,
                "goods": goods,        # 웹 검색으로 찾은 공식 굿즈 (가격은 관리자가) · 없으면 null = Catalog 가 건드리지 않음
            }
            if goods_evidence:
                artist["evidence"] = goods_evidence      # 출처 — 관리자 확인용 (Studio 가 Catalog 로 보낼 때 뺌)
            artist = self._with_description(s, artist, "ARTIST", key)   # 11/25 만들어 둔 소개 글 (describe)
            # 장르를 한 번도 정하지 못한 아티스트는 보내지 않고 기다림 (HOLD — 앨범도 아티스트가 간 뒤에) · 11/19 한도 소진으로 장르 없이 22팀이 갔음
            # 11/24 indig 가 다루지 않는 아티스트(대중 · 힙합)는 SKIPPED — 보내지 않고 앨범도 만들지 않음 (공연은 send 에서 라인업으로 한 번 더)
            self._upsert(s, "ARTIST", key, artist, identity if judged else "not-judged",
                         origin=f"https://musicbrainz.org/artist/{mb['mbid']}",
                         status="SKIPPED" if skip else "READY" if judged or genres else "HOLD", note=skip)
            if skip:
                s.commit()
                return

            albums = []   # (앨범, 원본 해시, 원래 주소, Discogs 판본, 출처)
            for g in groups:
                rel = releases[g["rgid"]]
                albums.append(({
                    "source": "crawler", "external_id": dbm.album_key_for(s, key, g["type"], g["title"], g["rgid"]),
                    "artist_ref": {"source": "crawler", "external_id": key},
                    "title": g["title"][:200], "type": g["type"], "genres": genres,
                    "released_at": g["released_at"], "thumbnail_url": None,
                    # 트랙은 전부 담음 (빠진 번호는 Catalog 가 지움) · 장르 · 템포 · 무드 · 감정 · 스타일은 관리자가 (null = 그대로)
                    "tracks": [{**t, "genres": None, "tempo": None, "moods": None, "emotions": None, "styles": None}
                               for t in rel["tracks"]] or None,
                }, content_hash(rel), f"https://musicbrainz.org/release-group/{g['rgid']}",
                    physical.get(norm(g["title"])) if physical is not None else None, []))
            for f in found["RELEASE"]:                   # MusicBrainz 에 없는 신보 (웹 검색 · 인디스트릿)
                d = f["data"]
                if _same_album(d["title"], [a[0]["title"] for a in albums]) is not None:
                    continue
                if not d.get("type"):
                    self.stats["found_release_no_type"] += 1   # 종류(정규 · EP · 싱글)를 모르면 앨범을 만들 수 없음
                    continue
                albums.append(({
                    "source": "crawler", "external_id": dbm.album_key_for(s, key, d["type"], d["title"]),
                    "artist_ref": {"source": "crawler", "external_id": key},
                    "title": d["title"][:200], "type": d["type"], "genres": genres,
                    "released_at": d.get("released_at"), "thumbnail_url": None, "tracks": None,
                }, content_hash(d), f["evidence"][0]["url"], None, [{"for": "album", **e} for e in f["evidence"]]))
                self.stats["album_from_found"] += 1

            per_album: dict[str, list] = {}
            for f in found["PHYSICAL"]:
                title = _same_album(f["data"]["album_title"], [a[0]["title"] for a in albums])
                if title is None:
                    self.stats["physical_no_album"] += 1      # 어느 앨범인지 하나로 정해지지 않으면 붙이지 않음
                    continue
                per_album.setdefault(title, []).append(f)
            for album, input_hash, origin, from_discogs, evidence in albums:
                album["physical_albums"], physical_evidence = _physicals(from_discogs, per_album.get(album["title"], []))
                if evidence or physical_evidence:
                    album["evidence"] = evidence + physical_evidence
                album = self._with_description(s, album, "ALBUM", album["external_id"])
                self._upsert(s, "ALBUM", album["external_id"], album, input_hash, origin=origin)
            s.commit()
        self.stats["artist_ok"] += 1
        self.stats["album_ok"] += len(albums)

    def _discover(self, key: str, mb: dict, names: list[str], groups: list[dict], label: str | None):
        """MusicBrainz 밖에서 이 아티스트의 신보 · 피지컬 · 굿즈를 찾아 found 에 쌓음 (인디스트릿 → 김밥레코즈 → 웹 검색)"""
        norms = {norm(n) for n in names if norm(n)}
        with self.sessions() as s:
            for a in self._indistreet:
                if any(norm(n) in norms for artist in a["artists"] for n in artist):
                    self.stats["found_indistreet"] += dbm.remember(
                        s, key, "RELEASE", norm(a["title"]), "indistreet",
                        {"title": a["title"], "type": a["type"], "released_at": a["released_at"]},
                        [{"site": "indistreet", "url": a["url"], "title": a["title"], "quote": None}])
            s.commit()

        for name in _search_names(names):
            try:
                products = gimbab.search(self.http, name)
            except Exception as e:
                self._fail("gimbab", name, e)
                continue
            with self.sessions() as s:
                for p in products:
                    if not any(norm(n) in norms for n in p["artist_names"]):
                        continue                         # 검색어가 다른 팀 이름 · 설명에 걸린 것
                    self.stats["found_gimbab"] += dbm.remember(
                        s, key, "PHYSICAL", f"gimbab:{p['id']}", "gimbab",
                        {"external_id": f"gimbab-{p['id']}", "album_title": p["title"], "format": p["format"],
                         "name": p["format"] if p["detail"].upper() == p["format"] else f"{p['format']} · {p['detail']}",
                         "released_at": None, "pre_order": p["pre_order"],
                         "vinyl_color": "COLOR" if p["colored"] else "UNKNOWN", "reference_price": None},
                        [{"site": "gimbab", "url": p["url"], "title": f"{p['title']} ({p['detail']})", "quote": None}])
                s.commit()

        self._web(key, mb, groups, label)

    def _web(self, key: str, mb: dict, groups: list[dict], label: str | None):
        """웹 검색 — 아티스트마다 7일에 한 번 (실행마다 WEB_ARTISTS_PER_RUN 팀까지). 키가 없으면 건너뜀."""
        if self.searcher is None:
            self.stats["web_search_off"] += 1
            return
        marker = f"websearch://{key}"
        marker_hash = hashlib.sha256(marker.encode()).hexdigest()
        with self.sessions() as s:
            done = s.scalar(select(dbm.RawPage).where(dbm.RawPage.url_hash == marker_hash))
            if done is not None and done.fetched_at > dbm.now() - WEB_EVERY:
                return
        if self._web_left <= 0 or getattr(self.searcher, "exhausted", False):   # 실행당 팀 수 · 검색 한도 · Gemini 하루 한도
            self.stats["web_search_deferred"] += 1       # 다음 실행에
            return
        self._web_left -= 1
        artist = {"name": mb["name"], "aliases": mb["aliases"], "label": label,
                  "known_albums": [{"title": g["title"], "type": g["type"], "released_at": g["released_at"]} for g in groups[-20:]]}
        try:
            releases = self.searcher.releases(artist)
            goods = self.searcher.goods(artist)
        except LlmSkip as e:
            self.stats[f"web_search_{e}"] += 1
            return
        except Exception as e:
            self._fail("web_search", key, e)
            return
        with self.sessions() as s:
            for r in releases:
                ev = [{"site": "websearch", **e} for e in r["evidence"]]
                self.stats["found_web_release"] += dbm.remember(
                    s, key, "RELEASE", norm(r["title"]), "websearch",
                    {"title": r["title"], "type": r["type"], "released_at": r["released_at"]}, ev)
                for f in r["formats"]:
                    match = f"web:{norm(r['title'])}:{f['format']}:{norm(f['name'])}"
                    self.stats["found_web_physical"] += dbm.remember(
                        s, key, "PHYSICAL", match, "websearch",
                        {"external_id": "web-" + hashlib.sha256(f"{key}:{match}".encode()).hexdigest()[:20],
                         "album_title": r["title"], "format": f["format"], "name": f["name"] or f["format"],
                         "released_at": f["released_at"], "pre_order": f["pre_order"], "vinyl_color": f["vinyl_color"],
                         "reference_price": f["reference_price"]},
                        [{"site": "websearch", **e} for e in f["evidence"]])
            for g in goods:
                self.stats["found_web_goods"] += dbm.remember(
                    s, key, "GOODS", norm(g["name"]), "websearch",
                    {"external_id": "web-" + hashlib.sha256(f"{key}:goods:{norm(g['name'])}".encode()).hexdigest()[:20],
                     **{k: g[k] for k in ("name", "category", "sizes", "purchase_url", "reference_price")}},
                    [{"site": "websearch", **e} for e in g["evidence"]])
            page = done or dbm.RawPage(source="websearch", url=marker, url_hash=marker_hash)
            body = json.dumps({"releases": releases, "goods": goods}, ensure_ascii=False)
            page.status, page.fetched_at, page.body = 200, dbm.now(), body
            page.content_hash = hashlib.sha256(body.encode()).hexdigest()
            s.merge(page)
            s.commit()
        self.stats["web_search_artist"] += 1

    @staticmethod
    def _found(s, key: str) -> dict[str, list[dict]]:
        out = {"RELEASE": [], "PHYSICAL": [], "GOODS": []}
        for f in s.scalars(select(dbm.Found).where(dbm.Found.artist_key == key).order_by(dbm.Found.id)):
            out[f.kind].append({"site": f.site, "data": json.loads(f.data_json), "evidence": json.loads(f.evidence_json)})
        return out

    def _ask(self, mb: dict, source: dict) -> dict | None:
        legal = mb.get("legal_names", [])
        web = []
        # 11/24 영어 이름인데 쓸 수 있는 한글 별칭이 없으면 한글 표기를 웹 검색 조각에서 찾음 — 근거 없이 물으면 지어냄 (llm.artist_info)
        # 본명은 본명으로 활동할 때만 씀 (MusicBrainz 이름이 본명 — Moon Sujin → 문수진)
        usable = [a for a in mb["aliases"] if HANGUL.search(a) and (a not in legal or mb["name"] in legal)]
        if self.searcher is not None and not getattr(self.llm, "exhausted", False) and LATIN.search(mb["name"]) and not usable:
            try:
                web = self.searcher.mentions(mb["name"])
                self.stats["artist_name_search"] += 1
            except SearchQuota:
                self.stats["artist_name_search_deferred"] += 1     # 검색 한도 — 장르와 함께 다음 실행에 (None)
                return None
            except Exception as e:
                self._fail("artist_name_search", mb["mbid"], e)
                return None
        try:
            return self.llm.artist_info(mb["name"], mb["aliases"], source["albums"], source["label"], legal, web)
        except LlmSkip as e:
            self.stats[f"artist_llm_{e}"] += 1
            return None

    # ---------- 소개 글 (11/25) ----------

    def describe(self):
        """아티스트 bio · 앨범 description (한글) — 새벽 · 점심 실행 모두 (전송 전 · 11/25 사용자).
        출처: 한국어 위키백과(MusicBrainz 의 Wikidata 연결 → kowiki) → 없으면 웹 검색 1번 (아티스트 · 피지컬 판본이 있는 앨범).
        Gemini 가 출처 사실로 쓰고, 문장마다 출처와 대조해 거릅니다 (describe.checked). 출처가 없거나 남은 문장이 모자라면 null —
        후보에 빈칸으로 가서 관리자가 승인할 때 씁니다. 한 번 쓴 글은 다시 쓰지 않음 (DESCRIBE_VERSION) · Gemini · 검색 한도면 다음 실행에."""
        if self.llm is None:
            return
        left = {"ARTIST": DESCRIBE_ARTISTS, "ALBUM": DESCRIBE_ALBUMS, "search": DESCRIBE_SEARCHES}
        with self.sessions() as s:
            done = {(d.kind, d.key): d for d in s.scalars(select(dbm.Description))}
            items = s.scalars(select(dbm.Item).where(dbm.Item.type.in_(("ARTIST", "ALBUM")), dbm.Item.status.in_(DESCRIBE_LIVE))
                              .order_by(dbm.Item.id)).all()
            mbids: dict[str, str] = {}
            for site_id, key in s.execute(select(dbm.ArtistLink.site_id, dbm.ArtistLink.key)
                                          .where(dbm.ArtistLink.site == "musicbrainz").order_by(dbm.ArtistLink.id)):
                mbids.setdefault(key, site_id)
        artists = {i.key: json.loads(i.payload_json) for i in items if i.type == "ARTIST"}
        for type_ in ("ARTIST", "ALBUM"):
            for item in items:
                if item.type != type_ or not _due(done.get((type_, item.key))):
                    continue
                payload = json.loads(item.payload_json)
                owner = item.key if type_ == "ARTIST" else payload["artist_ref"]["external_id"]
                if owner not in artists:
                    continue                             # 아티스트를 뺐거나(SKIPPED) 아직 없음
                if left[type_] <= 0 or not self._llm_usable():
                    self.stats[f"describe_{type_.lower()}_deferred"] += 1
                    continue
                try:
                    if type_ == "ARTIST":
                        result = self._bio(payload, mbids.get(item.key), left)
                    else:
                        result = self._album_description(payload, artists[owner], owner, mbids.get(owner), left)
                except LlmSkip as e:
                    self.stats[f"describe_llm_{e}"] += 1
                    continue
                except SearchQuota:
                    self.stats[f"describe_{type_.lower()}_deferred"] += 1
                    continue
                except Exception as e:
                    self._fail("describe", item.key, e)
                    continue
                if result is None:                       # 검색 몫을 다 써서 출처를 못 찾음 — 다음 실행에
                    self.stats[f"describe_{type_.lower()}_deferred"] += 1
                    continue
                text, evidence, reason = result
                self.stats[f"describe_{type_.lower()}_{'written' if text else reason}"] += 1
                self._save_description(type_, item.key, text, evidence, reason)

    def _bio(self, artist: dict, mbid: str | None, left: dict):
        """→ (글 또는 None, 출처, null 인 까닭) · 검색 몫이 없어 못 찾았으면 None"""
        mb = musicbrainz.artist(self.http, mbid) if mbid else None
        article = self._article(artist["external_id"], mb)
        if article is not None:
            sources = [{"no": 1, "site": "wikipedia", "url": article["url"], "title": article["title"],
                        "text": wikipedia.readable(article["text"])}]
        else:
            sources = self._search_sources(f"{artist['name']} 음악 소개", left)
            if sources is None:
                return None
        if not sources:
            return None, [], "no_source"
        albums = [g["title"] for g in musicbrainz.release_groups(self.http, mbid)][-15:] if mbid else []
        subject = {"artist": {"name": artist["name"], "musicbrainzName": mb["name"] if mb else None,
                              "aliases": (mb or {}).get("aliases", [])[:10], "label": artist.get("label"), "albums": albums}}
        left["ARTIST"] -= 1
        raw = self.llm.describe(llms.BIO_SYSTEM, subject, [{k: x[k] for k in ("no", "site", "title", "text")} for x in sources])
        banned = set((mb or {}).get("legal_names", [])) | desc.legal_names(x["text"] for x in sources)
        text, used = desc.checked(raw, sources, max_sentences=4, max_chars=300, min_sentences=2, banned_names=banned)
        return text, _evidence("bio", sources, used if text else None), None if text else "too_thin"

    def _album_description(self, album: dict, artist: dict, artist_key: str, mbid: str | None, left: dict):
        """앨범 — 아티스트 위키백과 문서에서 이 앨범을 《제목》 으로 적은 문장 → 없으면 피지컬 판본이 있는 앨범만 웹 검색 1번.
        0번 출처는 MusicBrainz 값(종류 · 발매일 · 트랙) — 그것만으로는 쓰지 않음 (11/25 사용자: 못 찾으면 빈칸)"""
        tracks = album.get("tracks") or []
        facts = (f"아티스트 {artist['name']} · 제목 {album['title']} · 종류 {album['type']} · 발매일 {album.get('released_at') or '모름'}"
                 + (f" · 트랙 {len(tracks)}곡: " + " / ".join(t["title"] for t in tracks[:30]) if tracks else ""))
        sources = [{"no": 0, "site": "musicbrainz", "url": None, "title": "facts", "text": facts}]
        article = self._article(artist_key, musicbrainz.artist(self.http, mbid) if mbid and artist_key not in self._articles else None)
        lines = wikipedia.sentences_about(article["text"], [album["title"]]) if article else []
        if lines:
            sources.append({"no": 1, "site": "wikipedia", "url": article["url"], "title": article["title"], "text": "\n".join(lines)})
        elif album.get("physical_albums"):
            found = self._search_sources(f"{artist['name']} {album['title']} 앨범 소개", left)
            if found is None:
                return None
            sources += found
        if len(sources) == 1:
            return None, [], "no_source"
        subject = {"album": {"artist": artist["name"], "title": album["title"], "type": album["type"],
                             "releasedAt": album.get("released_at"), "trackCount": len(tracks)}}
        left["ALBUM"] -= 1
        raw = self.llm.describe(llms.ALBUM_SYSTEM, subject, [{k: x[k] for k in ("no", "site", "title", "text")} for x in sources])
        # 11/25 실제 시험: 출처 문장이 한두 개면 Gemini 가 한 문장으로 씀 (한로로 《자몽살구클럽》) — 출처가 있는 한 문장도 받음
        text, used = desc.checked(raw, sources, max_sentences=3, max_chars=200, min_sentences=1,
                                  banned_names=desc.legal_names(x["text"] for x in sources), facts_no=0)
        return text, _evidence("description", sources[1:], (used - {0}) if text else None), None if text else "too_thin"

    def _article(self, artist_key: str, mb: dict | None) -> dict | None:
        """아티스트의 한국어 위키백과 문서 — 이번 실행 안에서는 한 번만 (앨범마다 다시 부르지 않음)"""
        if artist_key not in self._articles:
            self._articles[artist_key] = wikipedia.article_for(self.http, (mb or {}).get("wikidata"))
        return self._articles[artist_key]

    def _search_sources(self, query: str, left: dict) -> list[dict] | None:
        """웹 검색 1번 → 출처 [{no, site, url, title, text}] (1번부터) · 검색을 못 하면(키 없음) [] · 이번 몫을 다 썼으면 None"""
        if self.searcher is None:
            return []
        if left["search"] <= 0 or getattr(self.searcher, "exhausted", False):
            return None
        left["search"] -= 1
        self.stats["describe_search"] += 1
        return [{"no": n, "site": "websearch", "url": p["url"], "title": p["title"], "text": p["text"]}
                for n, p in enumerate(self.searcher.read_results(query), start=1)]

    def _save_description(self, type_: str, key: str, text: str | None, evidence: list[dict], reason: str | None):
        """저장하고 후보에 바로 넣음 — 바뀌었으면 READY (이번 실행에 보냄 · 장르를 기다리는 HOLD 는 그대로)"""
        with self.sessions() as s:
            row = s.scalar(select(dbm.Description).where(dbm.Description.kind == type_, dbm.Description.key == key))
            if row is None:
                row = dbm.Description(kind=type_, key=key)
                s.add(row)
            row.text, row.evidence_json, row.reason = text, json.dumps(evidence, ensure_ascii=False), reason
            row.version, row.checked_at = DESCRIBE_VERSION, dbm.now()
            s.flush()
            item = self._item(s, type_, key)
            payload = self._with_description(s, json.loads(item.payload_json), type_, key)
            self._upsert(s, type_, key, payload, item.input_hash, origin=item.origin,
                         status="HOLD" if item.status == "HOLD" else "READY")
            s.commit()

    @staticmethod
    def _with_description(s, payload: dict, type_: str, key: str) -> dict:
        """후보에 저장된 소개 글 · 출처를 넣은 새 값 (아티스트 bio · 앨범 description) — 글이 없으면 칸을 빼서 null (관리자가 씀)"""
        field = "bio" if type_ == "ARTIST" else "description"
        row = s.scalar(select(dbm.Description).where(dbm.Description.kind == type_, dbm.Description.key == key))
        evidence = [e for e in payload.get("evidence") or [] if e.get("for") != field]
        out = {k: v for k, v in payload.items() if k not in (field, "evidence")}
        if row is not None and row.text:
            out[field] = row.text
        if row is not None:
            evidence += json.loads(row.evidence_json)
        if evidence:
            out["evidence"] = evidence
        return out

    # ---------- 전송 ----------

    def send(self):
        self._skip_concerts_of_skipped_artists()
        self._lineup_names = None
        if self.studio is None:
            self.stats["send_dry_run"] += 1
            return
        for type_, path, limit in SEND:
            with self.sessions() as s:
                items = s.scalars(select(dbm.Item).where(dbm.Item.type == type_, dbm.Item.status == "READY")
                                  .order_by(dbm.Item.id)).all()
                if type_ == "ALBUM":   # 아티스트가 Studio 에 들어간 앨범만
                    sent = set(s.scalars(select(dbm.Item.key).where(dbm.Item.type == "ARTIST", dbm.Item.status == "SENT")))
                    items = [i for i in items if json.loads(i.payload_json)["artist_ref"]["external_id"] in sent]
            for i in range(0, len(items), limit):
                self._post(type_, path, items[i:i + limit])
        self._resend_marks()

    def _resend_marks(self):
        """11/26 이미 보낸 후보의 확인 표시가 바뀌면 다시 보냄 — 판단이 끝남 · 크롤러가 뺌(SKIPPED) · 보류(HOLD).
        Studio 대기열은 크롤러가 나중에 뺀 것을 모름 (11/26 비와이 · 주니엘 · 공연 4건이 대기로 남아 있었음) → 관리자 화면이 표시를 보고 거절.
        상태(SENT · SKIPPED · HOLD)는 그대로 · Studio 가 거절한 것(REVIEWED)은 보내지 않음."""
        for type_, path, limit in SEND:
            with self.sessions() as s:
                rows = s.scalars(select(dbm.Item).where(
                    dbm.Item.type == type_, dbm.Item.status.in_(("SENT", "SKIPPED", "HOLD")), dbm.Item.sent_hash.is_not(None),
                    or_(dbm.Item.studio_result.is_(None), dbm.Item.studio_result != "REVIEWED")).order_by(dbm.Item.id)).all()
            todo = [r for r in rows if self._marked(r)["source_hash"] != r.sent_hash]   # 표시 · 라인업 연결이 바뀐 것만
            self.stats[f"send_{type_.lower()}_marks"] += len(todo)
            for i in range(0, len(todo), limit):
                self._post(type_, path, todo[i:i + limit], keep_status=True)

    def _lineup(self, names: list[str]) -> list[dict]:
        """11/26 공연 라인업 이름마다 어느 아티스트 후보인지(externalId) 또는 왜 없는지(missing) — 관리자 화면이 이름 대신 id 로 맞추게.
        영어 · 한글 이름이 달라도(BewhY · 비와이) 크롤러가 찾은 이름 연결로 같은 팀. 수집하지 못한 팀은 관리자가 라인업에 넣을 수 없음."""
        if self._lineup_names is None:
            with self.sessions() as s:
                links = dict(s.execute(select(dbm.ArtistLink.site_id, dbm.ArtistLink.key).where(dbm.ArtistLink.site == "name")).all())
                miss = dict(s.execute(select(dbm.ArtistMiss.name_norm, dbm.ArtistMiss.reason)).all())
            self._lineup_names = (links, miss)
        links, miss = self._lineup_names
        out = []
        for n in names:
            key = links.get(norm(n))
            out.append({"name": n, "externalId": key} if key
                       else {"name": n, "missing": MISS_WHY.get(miss.get(norm(n)), "아직 찾지 않음 (다음 실행에)")})
        return out

    def _marked(self, item) -> dict:
        """보낼 값 — 저장한 값에 관리자 확인 표시를 붙이고 원본 해시를 다시 셈 (Studio 는 해시가 같으면 그대로 둠)"""
        payload = {k: v for k, v in json.loads(item.payload_json).items() if k != "source_hash"}
        notes = []
        if item.status == "SKIPPED":
            why = item.error or ""
            notes.append(f"크롤러가 뺌 ({SKIP_WHY.get(why.removeprefix('LINEUP_'), why)}"
                         f"{' · 라인업 대부분이 뺀 팀' if why.startswith('LINEUP_') else ''}) — 승인하지 말고 거절")
        elif item.status == "HOLD":
            notes.append("크롤러가 보류 중 (장르를 정하지 못함) — 판단이 끝나 다시 올 때까지 승인하지 말 것")
        if item.type == "ARTIST" and item.input_hash == "not-judged" and item.status != "SKIPPED":
            notes.append("판단 전 — 인디 여부 · 장르 · 한글 이름을 지금 기준으로 아직 묻지 못함 (장르 · 이름은 이전 값 · 다음 실행에 다시 물음)")
        if item.type == "CONCERT":
            c = dict(payload["concert"])
            if "reviewNotes" in c or notes:   # 11/22 전 공연은 키가 없음 — 표시가 없으면 그대로 (해시가 같아 다시 가지 않게)
                c["reviewNotes"] = [n for n in c.get("reviewNotes") or [] if not n.startswith(MARK)] + [MARK + n for n in notes]
            c["lineup"] = self._lineup(c.get("artistNames") or [])
            payload["concert"] = c
        else:
            evidence = [e for e in payload.get("evidence") or [] if e.get("for") not in ("identity", "check")]
            if item.origin and "musicbrainz.org/" in item.origin:   # 동명이인 확인용 — 어느 MusicBrainz 아티스트 · 앨범에서 왔는지
                evidence.insert(0, {"for": "identity", "site": "MusicBrainz", "url": item.origin,
                                    "title": payload.get("name") or payload.get("title")})
            evidence += [{"for": "check", "note": n} for n in notes]
            if evidence:
                payload["evidence"] = evidence
        payload["source_hash"] = content_hash(payload)
        return payload

    def _skip_concerts_of_skipped_artists(self):
        """11/24 라인업에서 확인된 팀(우리가 아는 아티스트) 중 절반 넘게가 뺀 아티스트(대중 · 힙합)면 공연도 뺌 — 공연 판단(Gemini)이 놓친 것.
        보내기 전(READY)과 이미 보낸 것(SENT) 모두. 이미 보낸 공연은 Studio 대기열에 남으므로 로그를 보고 관리자가 거절합니다."""
        with self.sessions() as s:
            links = dict(s.execute(select(dbm.ArtistLink.site_id, dbm.ArtistLink.key).where(dbm.ArtistLink.site == "name")).all())
            artists = {i.key: (i.status, i.error) for i in s.scalars(select(dbm.Item).where(dbm.Item.type == "ARTIST"))}
            for item in s.scalars(select(dbm.Item).where(dbm.Item.type == "CONCERT", dbm.Item.status.in_(("READY", "SENT")))):
                keys = dict.fromkeys(links[norm(n)] for n in json.loads(item.payload_json)["concert"]["artistNames"]
                                     if links.get(norm(n)) in artists)
                skipped = [artists[k][1] for k in keys if artists[k][0] == "SKIPPED"]
                if not keys or len(skipped) * 2 <= len(keys):
                    continue
                reason = f"LINEUP_{Counter(skipped).most_common(1)[0][0]}"
                if item.status == "SENT":
                    self.stats["concert_skipped_after_sent"] += 1
                    log.info("crawl_concert_skipped_after_sent external_id=%s reason=%s", item.key, reason)
                item.status, item.error, item.updated_at = "SKIPPED", reason, dbm.now()
                self.stats["concert_skipped_lineup"] += 1
            s.commit()

    def _post(self, type_: str, path: str, items: list, keep_status: bool = False):
        """한 묶음 전송. 400 이면 반으로 나눠 다시 — 잘못된 한 건만 FAILED 로 남깁니다 (표시만 다시 보낸 것은 상태를 바꾸지 않음)."""
        payloads = [self._marked(i) for i in items]
        try:
            result = self.studio.post(path, {"items": payloads})
        except StudioRejected as e:
            if e.status == 400 and len(items) > 1:
                mid = len(items) // 2
                self._post(type_, path, items[:mid], keep_status)
                self._post(type_, path, items[mid:], keep_status)
                return
            if e.status != 400:
                raise                                  # 401 · 5xx — 실행을 멈추고 다음 실행에 다시
            if not keep_status:
                self._mark(items, "FAILED", error=str(e))
            self.stats[f"send_{type_.lower()}_failed"] += len(items)
            return
        by_key = {r["external_id"]: r["result"] for r in result.get("items", [])}
        self._mark(items, None if keep_status else "SENT", results=by_key,
                   hashes={i.id: p["source_hash"] for i, p in zip(items, payloads)})
        for r in by_key.values():
            self.stats[f"send_{type_.lower()}_{r.lower()}"] += 1

    def _mark(self, items, status, error=None, results=None, hashes=None):
        """status 가 None 이면 보낸 기록(보낸 해시 · Studio 결과)만 남김"""
        with self.sessions() as s:
            for i in items:
                row = s.get(dbm.Item, i.id)
                if status is not None:
                    row.status, row.error, row.updated_at = status, error, dbm.now()
                if status in ("SENT", None):
                    row.sent_hash = (hashes or {}).get(row.id, row.source_hash)   # 보낸 값(표시 포함)의 해시
                    row.studio_result = (results or {}).get(row.key)
            s.commit()

    # ---------- 공통 ----------

    @staticmethod
    def _item(s, type_, key):
        return s.scalar(select(dbm.Item).where(dbm.Item.type == type_, dbm.Item.key == key))

    def _upsert(self, s, type_, key, payload, input_hash, origin, status="READY", note=None):
        payload = {k: v for k, v in payload.items() if k != "source_hash"}
        payload["source_hash"] = content_hash(payload)
        item = self._item(s, type_, key)
        if item is None:
            item = dbm.Item(type=type_, key=key)
            s.add(item)
        elif item.source_hash == payload["source_hash"] and (item.status == "SKIPPED") == (status == "SKIPPED"):
            item.input_hash = input_hash
            return item                       # 내용 그대로 — 상태(SENT · FAILED)도 그대로 (빼기 · 다시 넣기는 내용이 같아도 바꿈 · 11/24)
        item.payload_json = json.dumps(payload, ensure_ascii=False)
        item.input_hash, item.source_hash, item.origin = input_hash, payload["source_hash"], origin[:1024]
        item.status, item.error, item.updated_at = status, note, dbm.now()
        s.flush()
        return item

    def _fail(self, what, key, e):
        self.stats[f"{what}_error"] += 1
        f = self.failures.setdefault(what, {"count": 0, "codes": Counter(), "samples": []})
        f["count"] += 1
        f["codes"][notify.error_code(e)] += 1
        if len(f["samples"]) < notify.SAMPLES:
            f["samples"].append((str(key)[:120], str(e)[:300]))
        log.warning("crawl_%s_failed item=%s error=%s", what, key, str(e)[:300])   # key= 는 로그 가림 필터가 비밀로 봄 (11/19)


def _due(row) -> bool:
    """소개 글을 (다시) 만들 차례 — 처음 · 만드는 방식이 바뀜 · 출처가 없었거나 못 쓴 지 30일"""
    return (row is None or row.version != DESCRIBE_VERSION
            or (row.text is None and row.checked_at < dbm.now() - DESCRIBE_AGAIN))


def _evidence(field: str, sources: list[dict], used: set[int] | None) -> list[dict]:
    """소개 글 출처 — 쓴 출처만 (used) · 못 썼으면(None) 찾은 출처 전부 (관리자가 쓸 때 참고) · 글 앞부분을 quote 로"""
    return [{"for": field, "site": x["site"], "url": x["url"], "title": x["title"], "quote": " ".join(x["text"].split())[:300]}
            for x in sources if used is None or x["no"] in used]


def _skip_reason(relevance: str, genres: list[str]) -> str | None:
    """11/24 indig 가 다루지 않는 아티스트 — 대중 쪽(Gemini 판단) · 대표 장르(첫 번째)가 힙합 (사용자 결정 · docs/v2_인디_아티스트_기준.md)"""
    if relevance == "MAINSTREAM":
        return "MAINSTREAM"
    if genres and genres[0] == "HIPHOP":
        return "HIPHOP"
    return None


def _display_name(mb_name: str, korean: str | None) -> str:
    """11/23 보내는 이름은 한글 — MusicBrainz 이름에 영어가 섞여 있으면 Gemini 가 고른 한글 활동명 (모르면 MusicBrainz 이름 그대로).
    이미 한글뿐인 이름은 바꾸지 않음 (박성진 → 성진 같은 다른 표기로 흔들리지 않게)"""
    return korean if korean and LATIN.search(mb_name) else mb_name


def _same_album(title: str, titles: list[str]) -> str | None:
    """찾은 제목이 가리키는 앨범 제목 — 정규화해 같으면 그것, 아니면 한쪽이 다른 쪽을 품는 것(4자 이상)이 하나뿐일 때만
    ('정규 2집 POWER ANDRE 99' → 'POWER ANDRE 99'). 그래도 없으면 '정규 3집' · 'EP' 같은 앞말과 괄호 뒤를 떼고 같은 것이
    하나뿐일 때만 (11/25 김밥레코즈 '3집 헤븐' → '헤븐 (Heaven)' · 'EP 동물' → '동물'). 여럿이거나 없으면 None (붙이지 않음)."""
    t = norm(title)
    exact = [x for x in titles if norm(x) == t]
    if exact:
        return exact[0]
    hits = [x for x in titles if min(len(norm(x)), len(t)) >= 4 and (norm(x) in t or t in norm(x))]
    if len(hits) == 1:
        return hits[0]
    forms = _title_forms(title)
    same = list(dict.fromkeys(x for x in titles if _title_forms(x) & forms))
    return same[0] if len(same) == 1 else None


_EDITION_PREFIX = re.compile(r"^(?:(?:정규|미니|싱글|EP)(?=[\s\d])\s*)?(?:\d+\s*집\s*)?(?:(?:정규|미니|싱글|EP)(?=\s)\s*)?[:\-]?\s*", re.I)
_EDITION_SUFFIX = re.compile(r"\s*(?:GSI Edition|\d+(?:st|nd|rd|th) Anniversary Edition)$", re.I)
_VERSION = re.compile(r"remix|remake|live|acoustic|inst|remaster|version|demo|sped|리믹스|리메이크|라이브|어쿠스틱|버전", re.I)


def _title_forms(title: str) -> set[str]:
    """같은 앨범인지 볼 제목 꼴 — 그대로 · 앞말('정규 3집' · 'EP 2집' · '1집 -') · 판 이름('GSI Edition' · '10th Anniversary Edition')을 뗀 것 ·
    괄호 앞 (괄호가 다른 판(Acoustic · Remix · Live …)을 말하면 빼지 않음 — '5.0 (Acoustic)' 은 '5.0' 이 아님)"""
    core = _EDITION_SUFFIX.sub("", _EDITION_PREFIX.sub("", title)).strip() or title
    forms = {title, core}
    paren = core.find("(")
    if paren > 0 and not _VERSION.search(core[paren:]):
        forms.add(core[:paren])
    return {norm(f) for f in forms if norm(f)}


def _physicals(from_discogs: list | None, found: list[dict]) -> tuple[list | None, list[dict]]:
    """앨범의 피지컬 판본 — 같은 형식(LP · CD · 카세트)은 한 곳 것만: Discogs > 김밥레코즈 > 웹 검색 (같은 판이 둘로 올라가지 않게).
    판매가 · 재고는 null (관리자가 넣음 · 새 옵션은 판매가가 있어야 Catalog 가 받음). 아무것도 없으면 None = Catalog 가 건드리지 않음."""
    if from_discogs is None and not found:
        return None, []
    out, evidence = list(from_discogs or []), []
    taken = {p["format"] for p in out}
    for site in ("gimbab", "websearch"):
        added = set()
        for f in found:
            d = f["data"]
            if f["site"] != site or d["format"] in taken:
                continue
            lp = d["format"] == "LP"
            out.append({"external_id": d["external_id"], "format": d["format"], "name": d["name"][:255],
                        "released_at": d.get("released_at"),
                        "options": [{"name": "기본", "vinyl_color": ("COLOR" if d.get("vinyl_color") == "COLOR" else "BLACK") if lp else None,
                                     "size": None, "price": None, "qty": None}]})
            notes = [n for n in (
                "예약 판매" if d.get("pre_order") else None,
                f"참고 가격 {d['reference_price']:,}원 (출처 페이지 가격 · 판매가는 관리자가 정함)" if d.get("reference_price") else None,
                "LP 색 모름 — 검정으로 둠" if lp and d.get("vinyl_color") not in ("COLOR", "BLACK") else None) if n]
            evidence += [{"for": f"physical:{d['external_id']}", **e, **({"note": " · ".join(notes)} if notes and i == 0 else {})}
                         for i, e in enumerate(f["evidence"])]
            added.add(d["format"])
        taken |= added
    return (out or None), evidence


def _goods(found: list[dict]) -> tuple[list | None, list[dict]]:
    """찾은 굿즈 → Catalog 굿즈 모양 (옵션은 사이즈 · 없으면 FREE · 판매가 null → 관리자가 넣어야 승인)"""
    goods, evidence = [], []
    for f in found:
        d = f["data"]
        goods.append({"external_id": d["external_id"], "name": d["name"][:255], "category": d["category"],
                      "thumbnail_url": None, "purchase_url": d.get("purchase_url"),
                      "options": [{"name": size, "vinyl_color": None, "size": size, "price": None, "qty": None}
                                  for size in d.get("sizes") or ["FREE"]]})
        note = f"참고 가격 {d['reference_price']:,}원 (출처 페이지 가격 · 판매가는 관리자가 정함)" if d.get("reference_price") else None
        evidence += [{"for": f"goods:{d['external_id']}", **e, **({"note": note} if note and i == 0 else {})}
                     for i, e in enumerate(f["evidence"])]
    return (goods or None), evidence


def _search_names(names: list[str]) -> list[str]:
    """검색어로 쓸 이름 — 정규화해 다른 것만, 한글 이름 먼저 (가게 상품명이 한글 위주)"""
    seen, out = set(), []
    for n in sorted(names, key=lambda x: not re.search(r"[가-힣]", x)):
        if norm(n) and norm(n) not in seen:
            seen.add(norm(n))
            out.append(n)
    return out[:SEARCH_NAMES]


def to_concert(detail: dict, c: dict, venue: dict | None = None) -> dict:
    """Gemini 결과 + KOPIS 원문 → Studio 공연 후보 (계약 4장 키). 모름 값은 null, 원문에 있는 값으로 비는 것을 채움."""
    venue = venue or {}
    relates = [r for r in detail.get("relates") or [] if isinstance(r, dict)]
    link = next((r.get("relateurl") for r in relates if r.get("relateurl")), None)
    price = c.get("price")
    poster = detail.get("poster")
    concert = {
        "title": (_opt(c.get("title")) or detail.get("prfnm") or "")[:200],
        "type": c.get("type") if c.get("type") in ("SOLO", "FESTIVAL") else "SOLO",
        "genre": _opt(c.get("genre")) if c.get("type") == "FESTIVAL" else None,
        "startsAt": _when(c.get("startsAt")),
        "endsAt": _when(c.get("endsAt")),
        "bookingOpenAt": _when(c.get("bookingOpenAt")),
        "price": price if isinstance(price, int) and price >= 0 else None,
        "venueName": _opt(c.get("venueName")) or detail.get("fcltynm"),
        "venueAddress": venue.get("address"),
        "region": _opt(c.get("region")),
        "artistNames": _unique(c.get("artistNames")),       # 같은 팀(표기만 다름)은 처음 것 하나
        "unmatchedCast": [n.strip() for n in c.get("unmatchedCast") or [] if n and n.strip()],   # 관리자 참고 (팀을 확인하지 못한 출연진)
        "bookingNotes": [n for n in c.get("bookingNotes") or [] if n],                          # 관리자 참고 (선예매 등)
        "reviewNotes": [],                                   # 관리자 참고 — 크롤러가 고친 값 (비어 있지 않으면 검수 필요)
        "description": None,
        "terms": None, "requirements": None,
        # KOPIS 주소 그대로 — Studio 가 승인할 때 우리 버킷으로 옮김 (못 옮기면 관리자가 직접 올림)
        "posterUrl": poster if _kopis_image(poster) else None,
        "imageUrls": [u for u in detail.get("styurls") or [] if _kopis_image(u)][:MAX_IMAGE_URLS],
        "sourceUrl": link,
    }
    concert["reviewNotes"] = fit_kopis_dates(detail, concert)
    concert["description"] = _description(detail, c, venue, concert)
    return concert


def fit_kopis_dates(detail: dict, concert: dict) -> list[str]:
    """시작 · 끝을 KOPIS 원문에 맞춤 — 시작 날짜는 공연 기간(prfpdfrom~prfpdto) 안, 시간 안내(dtguidance)의 시각이 하나뿐이면 그 시각.
    고쳤으면 concert 를 바꾸고 관리자용 '검수 필요' 글을 돌려줌. 11/22 투어 포스터의 다른 회차 날짜 · 시각을 고른 7건 (사뮈 12/19 → 12/11 등)"""
    try:
        first = datetime.strptime(detail.get("prfpdfrom") or "", "%Y.%m.%d").date()
        last = datetime.strptime(detail.get("prfpdto") or "", "%Y.%m.%d").date()
        starts = datetime.fromisoformat(concert["startsAt"])
    except (TypeError, ValueError):
        return []                                      # 원문 기간이나 시작이 없으면 맞출 기준이 없음
    fixed = starts
    if not first <= fixed.date() <= last:
        fixed = fixed.replace(year=first.year, month=first.month, day=first.day)
    times = set(re.findall(r"\b(\d{1,2}):(\d{2})\b", detail.get("dtguidance") or ""))
    if len(times) == 1:
        h, m = map(int, times.pop())
        fixed = fixed.replace(hour=h, minute=m)
    if fixed == starts:
        return []
    note = f"검수 필요 — 시작 {starts:%Y-%m-%d %H:%M} → {fixed:%Y-%m-%d %H:%M}"
    concert["startsAt"] = fixed.isoformat()
    if concert.get("endsAt"):
        ends = datetime.fromisoformat(concert["endsAt"])
        if first == last:   # 하루 공연 — 길이는 그대로, 길이가 이상하면(하루 이상 · 0 이하) 끝 날짜만 공연 날짜로 (신촌블루스 11/28~11/29)
            span = ends - starts
            new_end = fixed + span if timedelta(0) < span < timedelta(days=1) else ends.replace(year=first.year, month=first.month, day=first.day)
        elif not first <= ends.date() <= last:
            new_end = ends.replace(year=last.year, month=last.month, day=last.day)
        else:
            new_end = ends
        if new_end <= fixed:                               # 맞출 수 없으면 비움 — 관리자가 넣음 (론 브랜튼 12/19~12/26 → 하루 공연)
            concert["endsAt"] = None
            note += f" · 끝 {ends:%Y-%m-%d %H:%M} → 비움"
        elif new_end != ends:
            concert["endsAt"] = new_end.isoformat()
            note += f" · 끝 {ends:%Y-%m-%d %H:%M} → {new_end:%Y-%m-%d %H:%M}"
    period = " ~ ".join(dict.fromkeys(x for x in (detail.get("prfpdfrom"), detail.get("prfpdto")) if x))
    return [f"{note} (KOPIS 기간 {period} · 시간 안내 {detail.get('dtguidance') or '없음'} 에 맞춰 크롤러가 고침)"]


def _opt(v):
    return None if v is None or v in _NONE else v


def _unique(names) -> list[str]:
    out: dict[str, str] = {}
    for n in names or []:
        if isinstance(n, str) and norm(n) and norm(n) not in out:
            out[norm(n)] = n.strip()
    return list(out.values())


def _when(v):
    try:
        return datetime.fromisoformat(v).isoformat() if _opt(v) else None
    except ValueError:
        return None


def _kopis_image(url) -> bool:
    return isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and urlsplit(url).hostname in KOPIS_HOSTS


def _venue(detail: dict, fac: dict) -> dict:
    """공연시설 API → 주소 · 그 공연장(관) 이름 · 좌석 수. 좌석은 그 관의 값만 (관이 하나인 시설은 시설 값) · 0 은 모름."""
    hall = kopis.hall(fac, detail.get("mt13id"))
    if not hall and len(fac.get("mt13s") or []) == 1 and isinstance(fac["mt13s"][0], dict):
        hall = fac["mt13s"][0]
    seats = (hall.get("seatscale") or "").strip()
    return {"name": fac.get("fcltynm") or detail.get("fcltynm"), "hall": hall.get("prfplcnm") or None,
            "address": (fac.get("adres") or "").strip() or None,
            "seats": int(seats) if seats.isdigit() and int(seats) > 0 else None}


def _mentioned(known: list[tuple[str, str]], detail: dict) -> list[str]:
    """아는 아티스트 중 제목 · 출연진 · 소개 글에 이름이 보이는 것 (최대 30)"""
    text = norm(" ".join(str(detail.get(k) or "") for k in ("prfnm", "prfcast", "sty")))
    return list(dict.fromkeys(name for n, name in known if n in text))[:30]


def _lines(values, limit: int) -> list[str]:
    """Gemini 가 준 줄 목록 — 빈 줄 · 같은 줄을 빼고 한 줄로 (마크다운 목록이 깨지지 않게)"""
    out = [" ".join(str(v).split()) for v in values or [] if isinstance(v, str)]
    return [v for v in dict.fromkeys(out) if v][:limit]


def _performer(p: dict) -> str | None:
    """출연진 소개 한 줄 — 개인 정보 문장(생년 · 학교 · 가족 등 · describe.PRIVATE)은 뺌 · 소개가 남지 않으면 넣지 않음"""
    name = " ".join(str(p.get("name") or "").split())
    sentences = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s*", " ".join(str(p.get("intro") or "").split()))
    intro = " ".join(s for s in sentences if s and not desc.PRIVATE.search(s)).strip()
    return f"**{name}** — {intro}" if name and intro else None


_AGE_WORDS = re.compile(r"관람\s*연령|관람\s*가능|관람가|관람|연령|이상|이하|전체|만|세|가능|본|공연|입장|입니다|합니다|됩니다|은|는|이|가|[\d\s:·.,()\-~!]")


def _only_age(note: str) -> bool:
    """관람 연령만 다시 적은 줄 ('만 7세 이상 관람 가능' · '전체 관람가') — 관람 연령 줄이 따로 있어 뺌. 확인 방법 등 다른 말이 있으면 남김"""
    return not _AGE_WORDS.sub("", note).strip()


def _description(detail: dict, c: dict, venue: dict, concert: dict) -> str | None:
    """상세 설명 (마크다운) — 소개 · 관람 공지는 Gemini 가 이미지 · 글에서 정리, 일정 · 연령 · 공연장 · 출연 · 주최는 KOPIS 원문 값 그대로.
    가격 · 예매 오픈은 따로 넣는 값이고, 예매처마다 다른 안내(수령 · 환불 · 고객센터)는 indig 가 직접 팔므로 넣지 않습니다."""
    def section(title, rows):
        rows = [r for r in rows if r]
        return [f"## {title}", *[f"- {r}" for r in rows], ""] if rows else []

    period = " ~ ".join(dict.fromkeys(x for x in (detail.get("prfpdfrom"), detail.get("prfpdto")) if x))
    org: dict[str, list[str]] = {}
    for k, role in (("entrpsnmH", "주최"), ("entrpsnmS", "주관"), ("entrpsnmA", "기획"), ("entrpsnmP", "제작")):
        if (detail.get(k) or "").strip():
            org.setdefault(detail[k].strip(), []).append(role)
    place = " ".join(x for x in (venue.get("name") or concert.get("venueName"),
                                 f"({venue['hall']})" if venue.get("hall") and venue["hall"] != venue.get("name") else None) if x)
    lines = []
    summary = (_opt(c.get("summary")) or "").strip()
    if summary:
        lines += [summary, ""]
    lines += section("출연진 소개", [_performer(p) for p in (c.get("performers") or [])[:12] if isinstance(p, dict)])
    lines += section("프로그램", _lines(c.get("program"), 40))
    lines += section("타임테이블", _lines(c.get("timetable"), 80))
    lines += section("일정", [period, detail.get("dtguidance"), f"공연 시간 {detail['prfruntime']}" if detail.get("prfruntime") else None])
    age = (detail.get("prfage") or "").strip()
    lines += section("관람 안내", [f"관람 연령: {age}" if age else None,
                                   *[n for n in _lines(c.get("notices"), 8) if not (age and _only_age(n))]])
    lines += section("부대 행사", _lines(c.get("events"), 6))
    lines += section("공연장", [place, venue.get("address"), f"{venue['seats']:,}석" if venue.get("seats") else None])
    lines += section("출연", [" · ".join(concert["artistNames"])])
    lines += section("주최 · 기획", [f"{' · '.join(roles)}: {name}" for name, roles in org.items()])
    if not lines:
        return None
    lines.append("정보 출처: 공연예술통합전산망(KOPIS)")
    return "\n".join(lines)[:20000]
