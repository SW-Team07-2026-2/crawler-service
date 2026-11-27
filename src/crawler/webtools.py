"""웹 조사 도구 두 개 (, Anthropic 웹 검색, 웹 페치를 대신함) - 모델이 함수 호출로 부릅니다 (sources/websearch.py).

- Tavily: 웹 검색 API. basic 검색 1번 = 1크레딧 (무료 월 1,000, 분당 100건). 주소, 제목, 본문 조각, 게시일을 돌려줍니다.
- PageReader: Scrapling MCP 서버(stdio, 크롤러 프로세스의 자식으로만, 포트를 열지 않음)의 make_request 하나만 씁니다.
  MCP 를 그대로 모델에게 주지 않고 감쌉니다 (계획 10.6 에서 그대로 붙이지 않은 이유 1~4):
  1 우회 끔 - impersonate 없음, stealthy_headers False, 우리 User-Agent (브라우저, Cloudflare 통과 도구는 안 씀)
  2 robots.txt, 사이트 간격 - http.Http.allow (리다이렉트로 도착한 주소도 robots 확인)
  3 안쪽 주소 막기 - 공인 IP 로만 풀리는 주소, 리다이렉트는 safe(사설 IP 금지). 어떤 주소를 열지는 websearch 가 검색 결과로 한 번 더 제한
  4 JS 실행, 브라우저 없음 (KT VM, PC 메모리)
"""
import asyncio
import ipaddress
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .http import Blocked

TAVILY_URL = "https://api.tavily.com/search"
# 검색 결과에서 뺄 사이트 - Spotify 만 (사용자 결정, 쓰지 않음). 사용자 지시: 나머지는 다 찾음 (AI 수집 금지 사이트의 검색 조각도 씀)
EXCLUDE_DOMAINS = ["spotify.com", "open.spotify.com"]


RUN_BUDGET = 40   # 실행 한 번에 쓸 검색 크레딧 - 15 -> 20, 20 -> 40 (하루 몫은 runner.search_budget 이 달 단위로 나눔)
MONTHLY_SEARCHES = 1000   # Tavily 무료 월 크레딧 (매달 1일 다시 채워짐)
SEARCH_RESERVE = 100      # 기록 밖 사용 몫 (손 시험, 로컬 - Tavily 161, 우리 기록 143)


class SearchQuota(Exception):
    """이번 실행의 검색 한도(RUN_BUDGET)를 다 씀, Tavily 크레딧 소진(432, 433) - 남은 검색은 다음 실행에"""


class Tavily:
    def __init__(self, api_key: str, post=None, max_results: int = 5, budget: int | None = RUN_BUDGET):
        self.api_key = api_key
        self.max_results = max_results
        self.budget = budget
        self.searches = 0
        self.exhausted = False
        self._post = post or self._http_post

    def search(self, query: str) -> list[dict]:
        """[{url, title, content(본문 조각), published_date}]"""
        if self.exhausted or (self.budget is not None and self.searches >= self.budget):
            self.exhausted = True
            raise SearchQuota("budget")
        status, data = self._post({"query": query[:400], "search_depth": "basic", "max_results": self.max_results,
                                   "country": "south korea", "include_published_date": True, "exclude_domains": EXCLUDE_DOMAINS})
        if status in (432, 433):
            self.exhausted = True
            raise SearchQuota(f"Tavily {status}")
        if status != 200:
            raise RuntimeError(f"Tavily {status}: {str(data)[:200]}")
        self.searches += 1
        return [{"url": r["url"], "title": r.get("title") or "", "content": r.get("content") or "",
                 "published_date": r.get("published_date")} for r in data.get("results") or []
                if r.get("url") and not _excluded(r["url"])]          # Tavily 가 빼지 못한 하위 도메인까지 한 번 더

    def _http_post(self, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(TAVILY_URL, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode("utf-8", "replace")[:300]}


def _excluded(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in EXCLUDE_DOMAINS)


class PageReader:
    def __init__(self, http, user_agent: str, call=None, resolve=socket.getaddrinfo):
        self.http = http
        self.user_agent = user_agent
        self.fetches = 0
        self._call = call or self._mcp_call         # (도구 이름, 인자) -> {status, content: [글 조각], url}
        self._resolve = resolve
        self._loop = self._thread = self._session = self._closing = self._served = None

    def read(self, url: str) -> tuple[str, str]:
        """(도착 주소, 본문 마크다운). robots, 안쪽 주소, 오류면 Blocked / RuntimeError"""
        self._public(url)
        self.http.allow(url)
        res = self._call("make_request", {
            "url": url, "impersonate": None, "stealthy_headers": False, "headers": {"User-Agent": self.user_agent},
            "follow_redirects": "safe", "max_redirects": 5, "retries": 2, "timeout": 30,
            "extraction_type": "markdown", "main_content_only": True})
        final = res.get("url") or url
        if final != url:
            self._public(final)
            self.http.allow(final, wait=False)
        if (res.get("status") or 0) >= 400:
            raise RuntimeError(f"GET {final} → {res.get('status')}")
        self.fetches += 1
        return final, "\n\n".join(res.get("content") or [])

    def check_public(self, url: str):
        """페이지 안에서 찾은 주소(상세 이미지)도 같은 검사 - 공개 주소가 아니면 Blocked"""
        self._public(url)

    def _public(self, url: str):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Blocked(url)
        try:
            addrs = {info[4][0] for info in self._resolve(parts.hostname, None)}
        except OSError:
            raise Blocked(url) from None
        if not addrs or any(not ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs):
            raise Blocked(url)                       # 사설, 루프백, 링크 로컬 (같은 망의 studio, mysql, 클러스터 주소)

    # Scrapling MCP (stdio)

    def _mcp_call(self, name: str, args: dict) -> dict:
        if self._loop is None:
            self._start()
        res = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop).result(120)
        if getattr(res, "is_error", False) or getattr(res, "isError", False):
            raise RuntimeError(" ".join(getattr(c, "text", "") for c in res.content)[:300])
        data = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if data is None:
            data = json.loads(res.content[0].text)
        return data

    def _start(self):
        """MCP 세션은 한 작업 안에서 열고 닫아야 해서(anyio) 전용 스레드의 이벤트 루프에서 계속 열어 둡니다 - 실행이 끝나면 close()"""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="scrapling-mcp", daemon=True)
        self._thread.start()
        ready = asyncio.run_coroutine_threadsafe(self._open_ready(), self._loop)
        self._served = ready.result(120)

    async def _open_ready(self):
        started = asyncio.get_running_loop().create_future()
        served = asyncio.ensure_future(self._serve(started))
        await started
        return served

    async def _serve(self, started):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._closing = asyncio.Event()
        params = StdioServerParameters(command=str(Path(sys.executable).parent / "scrapling-mcp"), args=[])
        try:
            with open(os.devnull, "w") as quiet:                 # 서버가 요청마다 찍는 줄은 버림 (우리 쪽에서 기록)
                async with stdio_client(params, errlog=quiet) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        started.set_result(True)
                        await self._closing.wait()
        except Exception as e:
            if not started.done():
                started.set_exception(e)
            raise

    async def _shutdown(self):
        self._closing.set()
        await asyncio.wait_for(self._served, 30)

    def close(self):
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(40)
        except Exception:  # 닫는 중 오류는 무시 (자식 프로세스는 stdio 가 닫히면 끝남)
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        self._loop = self._thread = self._session = self._closing = self._served = None
