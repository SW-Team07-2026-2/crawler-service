"""가져오기 — Scrapling Fetcher(HTTP) 위에 robots.txt 확인 · 사이트별 간격 · 원문 저장(캐시)만 더합니다.

robots.txt 가 막은 주소는 가져오지 않습니다 (우회 기능을 쓰지 않음 · stealthy_headers 끔).
예외는 공식 API 이용 정책이 따로 허락한 경로뿐입니다 — 아래 API_POLICY.
"""
import hashlib
import logging
import re
import time
import urllib.robotparser
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from scrapling.fetchers import Fetcher
from sqlalchemy import select

from . import db as dbm

log = logging.getLogger(__name__)

# MusicBrainz 의 robots.txt 는 검색 로봇용으로 /ws 를 막지만, API 이용 정책(doc/MusicBrainz_API/Rate_Limiting)이
# 앱의 사용을 허락합니다 — 조건: IP 당 평균 초당 1건 · 연락처가 든 User-Agent · 정해진 시각에 몰아서 부르지 않기.
API_POLICY = {("musicbrainz.org", "/ws/2/"): 1.5}   # 초당 1건보다 넉넉히 (1.1초 간격에서도 503 이 났음)
# 잠깐 오류(400 · 5xx)를 주다가 곧 돌아오는 사이트 — 오류면 이만큼(초) 쉬었다가 다시 (11/19 KOPIS: 03:43 목록 · 10:17 상세 8건이 400 등,
# 같은 주소를 다시 부르면 200). 그래도 안 되면 그 항목만 실패 → 다음 실행에
RETRY_LATER = {"www.kopis.or.kr": (30, 60)}
_SECRET_PARAMS = re.compile(r"(service|token|key)=[^&\s]+", re.I)   # 공백에서 멈춤 — 주소 끝의 키 뒤 상태 · 본문까지 지우던 것 (11/19)


class Blocked(Exception):
    """robots.txt 가 막은 주소"""


def redact(url: str) -> str:
    """주소 안의 키(KOPIS service= 등)를 지운 값 — 저장 · 로그용"""
    return _SECRET_PARAMS.sub(r"\1=***", url)


class Http:
    def __init__(self, sessions, user_agent: str, default_interval: float = 1.0, fetch=None, sleep=time.sleep, fetch_bytes=None):
        self.sessions = sessions
        self.user_agent = user_agent
        self.default_interval = default_interval
        self._fetch = fetch or self._scrapling
        self._fetch_bytes = fetch_bytes or self._scrapling_bytes
        self._sleep = sleep
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last: dict[str, float] = {}

    def get(self, source: str, url: str, *, headers: dict | None = None, max_age: timedelta | None = None) -> str:
        """본문(텍스트). max_age 안에 같은 주소를 가져온 적 있으면 저장한 원문을 돌려줍니다."""
        safe = redact(url)
        url_hash = hashlib.sha256(safe.encode()).hexdigest()
        with self.sessions() as s:
            cached = s.scalar(select(dbm.RawPage).where(dbm.RawPage.url_hash == url_hash))
            if cached and max_age and cached.fetched_at > dbm.now() - max_age:
                return cached.body

        parts = urlsplit(url)
        interval = self._policy_interval(parts)
        if interval is None:
            rp = self._robots_for(parts)
            if not rp.can_fetch(self.user_agent, parts.geturl()):
                raise Blocked(safe)
            interval = max(self.default_interval, rp.crawl_delay(self.user_agent) or 0)   # robots.txt 의 Crawl-delay 를 따름
        self._wait(parts.netloc, interval)

        status, body = self._later(parts.hostname, lambda: self._get_with_retry(url, headers or {}))
        if status >= 400:   # 본문 앞부분도 남김 — 11/19 KOPIS 400 은 본문이 없어 원인을 알 수 없었음
            raise RuntimeError(f"GET {safe} → {status} {redact(' '.join(body.split()))[:200]}".rstrip())
        with self.sessions() as s:
            page = s.scalar(select(dbm.RawPage).where(dbm.RawPage.url_hash == url_hash))
            if page is None:
                page = dbm.RawPage(source=source, url=safe[:1024], url_hash=url_hash)
                s.add(page)
            page.status, page.fetched_at, page.body = status, dbm.now(), body
            page.content_hash = hashlib.sha256(body.encode()).hexdigest()
            s.commit()
        return body

    def get_bytes(self, source: str, url: str, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        """이미지 같은 바이너리 — robots.txt · 간격은 get 과 같고, 원문은 저장하지 않습니다 (크기 상한)."""
        parts = urlsplit(url)
        rp = self._robots_for(parts)
        if not rp.can_fetch(self.user_agent, parts.geturl()):
            raise Blocked(redact(url))
        self._wait(parts.netloc, max(self.default_interval, rp.crawl_delay(self.user_agent) or 0))
        status, body = self._later(parts.hostname, lambda: self._fetch_bytes(url, {"User-Agent": self.user_agent}))
        if status >= 400:
            raise RuntimeError(f"GET {redact(url)} → {status}")
        if len(body) > max_bytes:
            raise RuntimeError(f"GET {redact(url)} → {len(body)} bytes (상한 {max_bytes})")
        log.debug("fetched %s %s bytes from %s", source, len(body), redact(url))
        return body

    def allow(self, url: str, wait: bool = True):
        """다른 도구(Scrapling MCP · webtools.PageReader)로 가져올 주소 — robots.txt 가 막으면 Blocked, 아니면 사이트 간격만큼 기다림"""
        parts = urlsplit(url)
        rp = self._robots_for(parts)
        if not rp.can_fetch(self.user_agent, parts.geturl()):
            raise Blocked(redact(url))
        if wait:
            self._wait(parts.netloc, max(self.default_interval, rp.crawl_delay(self.user_agent) or 0))

    def _later(self, host, call):
        """call() → (상태, 본문). RETRY_LATER 사이트면 오류일 때 쉬었다가 다시"""
        status, body = call()
        for wait in RETRY_LATER.get(host, ()):
            if status < 400:
                break
            log.info("retry_later host=%s status=%s wait=%ss", host, status, wait)
            self._sleep(wait)
            status, body = call()
        return status, body

    def _get_with_retry(self, url, headers, tries: int = 5):   # 2 · 4 · 8 · 16초 쉬며 (느려도 괜찮음 · 11/17)
        for attempt in range(tries):
            status, body = self._fetch(url, {"User-Agent": self.user_agent, **headers})
            if status not in (429, 503) or attempt == tries - 1:
                return status, body
            self._sleep(2 ** (attempt + 1))        # 쉬었다가 (MusicBrainz 는 초과하면 503)
        return status, body

    @staticmethod
    def _policy_interval(parts):
        for (host, prefix), interval in API_POLICY.items():
            if parts.netloc.endswith(host) and parts.path.startswith(prefix):
                return interval
        return None

    def _robots_for(self, parts) -> urllib.robotparser.RobotFileParser:
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            status, body = self._fetch(base + "/robots.txt", {"User-Agent": self.user_agent})
            rp.parse(body.splitlines() if status == 200 else [])   # 없으면(404) 전부 허용 · 5xx 도 빈 규칙
            self._robots[base] = rp
        return rp

    def _wait(self, host: str, interval: float):
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < interval:
                self._sleep(interval - gap)
        self._last[host] = time.monotonic()

    @staticmethod
    def _scrapling_bytes(url: str, headers: dict) -> tuple[int, bytes]:
        r = Fetcher.get(url, headers=headers, stealthy_headers=False, timeout=30)
        return r.status, bytes(r.body) if isinstance(r.body, (bytes, bytearray)) else str(r.body).encode()

    @staticmethod
    def _scrapling(url: str, headers: dict) -> tuple[int, str]:
        r = Fetcher.get(url, headers=headers, stealthy_headers=False, timeout=30)
        body = r.body.decode("utf-8", "replace") if isinstance(r.body, (bytes, bytearray)) else str(r.body)
        return r.status, body
