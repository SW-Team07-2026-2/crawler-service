"""Studio 로 보내기 (docs/v2_크롤러_전송_계약.md) — 같은 compose 의 http://studio:8085/internal/crawl/**.

토큰: HS256 { iss: crawler, aud: studio, exp: 지금+5분 } · 키 crawler.shared-secret.
400 은 본문이 틀린 것이라 다시 보내지 않고, 502 · 503 · 연결 실패만 쉬었다가 다시 보냅니다 (같은 본문은 멱등).
"""
import json
import time
import urllib.error
import urllib.request

import jwt


class StudioRejected(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Studio {status}: {body[:300]}")
        self.status = status


class Studio:
    def __init__(self, base_url: str, secret: str, sleep=time.sleep, tries: int = 4):
        if not secret:
            raise ValueError("crawler.shared-secret 이 없습니다")
        self.base_url = base_url
        self.secret = secret
        self.sleep = sleep
        self.tries = tries

    def token(self) -> str:
        return jwt.encode({"iss": "crawler", "aud": "studio", "exp": int(time.time()) + 300}, self.secret, algorithm="HS256")

    def post(self, path: str, body: dict) -> dict:
        return self._call("POST", path, json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def get(self, path: str) -> dict:
        return self._call("GET", path, None)

    def _call(self, method: str, path: str, data: bytes | None) -> dict:
        for attempt in range(self.tries):
            headers = {"X-Internal-Token": self.token()}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    return json.loads(res.read())["result"]
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code not in (502, 503) or attempt == self.tries - 1:
                    raise StudioRejected(e.code, text) from None
            except urllib.error.URLError:
                if attempt == self.tries - 1:
                    raise
            self.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")
