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
- 보낸 것은 전부 Studio 에서 관리자 승인을 기다립니다. 같은 아티스트면 Gemini 장르를 다시 묻지 않고, 결과가 그대로면 다시 보내지 않습니다.
"""
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select

from . import db as dbm
from . import images as imgs
from .keys import content_hash, norm
from .llm import LlmSkip
from .sources import discogs, gimbab, indistreet, kopis, musicbrainz
from .studio import StudioRejected

log = logging.getLogger(__name__)

SEND = [  # (type, 경로, 한 요청 최대 건수) — 계약 2장 순서
    ("ARTIST", "/internal/crawl/catalog/artists", 500),
    ("ALBUM", "/internal/crawl/catalog/albums", 500),
    ("CONCERT", "/internal/crawl/concerts", 100),
]
_NONE = {"", "NONE", "UNKNOWN"}
_SKIP_FOR_LLM = {"poster", "styurls"}  # 이미지는 주소 대신 이미지로 넣음
CONCERT_VERSION = 2                    # 공연 정리 방식(프롬프트 · 입력)이 바뀌면 올림 → 같은 원문도 다시 정리
KOPIS_HOSTS = {"www.kopis.or.kr", "kopis.or.kr"}   # Studio 가 승인할 때 옮겨 오는 곳 (app.image.import-hosts 와 같음)
MAX_IMAGE_URLS = 20                    # Concert imageUrls 상한
KOPIS_DOWN = 5                         # 공연 상세가 (다시 불러도) 이만큼 연달아 실패하면 KOPIS 가 내려간 것으로 보고 남은 공연은 다음 실행에
MAX_CAST_LOOKUPS = 20                  # 팀을 못 찾은 출연진을 MusicBrainz 에서 볼 최대 수 (공연 한 건)
RECENT = timedelta(days=180)           # 이보다 최근 앨범은 트랙 · 레이블을 7일마다 다시 봄 (그 전은 90일)
WEB_EVERY = timedelta(days=7)          # 아티스트마다 웹 검색은 7일에 한 번
WEB_ARTISTS_PER_RUN = 30               # 한 번 실행에 웹 검색할 아티스트 수 — 나머지는 다음 새벽에 (실행이 너무 길어지지 않게)
TICKET_SEARCHES = 8                    # 한 번 실행에 예매 오픈 찾기에 쓸 검색 크레딧 — 나머지는 아티스트 조사에
SEARCH_NAMES = 3                       # 김밥레코즈 검색어로 쓸 이름 수 (한글 이름 먼저)


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
        self._web_left = WEB_ARTISTS_PER_RUN

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
        for mt20id in kopis.list_ids(self.http, key, start, end):
            try:
                detail = kopis.detail(self.http, key, mt20id)
            except Exception as e:  # 한 건 실패로 실행 전체를 멈추지 않음
                self._fail("concert_fetch", mt20id, e)
                misses += 1
                if misses >= KOPIS_DOWN:   # 다시 불러도 계속 실패 — 남은 공연을 하나씩 기다리지 않음 (실행이 다음 일정까지 늘어지지 않게)
                    self.stats["concert_kopis_down"] += 1
                    break
                continue
            misses = 0
            if not detail:
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
                if item is not None and item.input_hash == input_hash:
                    self.stats["concert_same_input"] += 1
                    if item.status != "SKIPPED":
                        names += json.loads(item.payload_json)["concert"]["artistNames"]
                    continue
            if getattr(self.llm, "exhausted", False):   # 하루 한도를 다 씀 · 서버가 계속 응답 못 함 — 이미지를 받지 않고 다음 실행에
                self.stats[f"concert_llm_{getattr(self.llm, 'stop_reason', 'quota')}"] += 1
                continue
            facts = {"detail": {k: v for k, v in detail.items() if k not in _SKIP_FOR_LLM}, "venue": venue,
                     "knownArtists": _mentioned(known, detail)}
            try:
                c = self.llm.concert(facts, self._concert_images(mt20id, detail))
            except LlmSkip as e:
                self.stats[f"concert_llm_{e}"] += 1
                continue
            indie = c.get("relevance") == "INDIE"
            if indie and c.get("unmatchedCast") and not c.get("artistNames"):
                # 포스터 · 글로 팀을 하나도 확인하지 못했을 때만 MusicBrainz 멤버 관계로 (확인한 팀이 있으면 남은 이름은 대개 그 팀 멤버 · 세션)
                c["artistNames"], c["unmatchedCast"] = self._teams_for(c["unmatchedCast"])
            concert = to_concert(detail, c, venue)
            payload = {"source": "kopis", "external_id": mt20id, "concert": concert}
            with self.sessions() as s:
                self._upsert(s, "CONCERT", mt20id, payload, input_hash,
                             origin=concert.get("sourceUrl") or f"kopis:{mt20id}",
                             status="READY" if indie else "SKIPPED", note=None if indie else c.get("relevance"))
                s.commit()
            self.stats["concert_indie" if indie else "concert_skipped"] += 1
            if indie:
                names += concert["artistNames"]
        self._tickets()
        return names

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

    def _teams_for(self, unmatched: list[str]) -> tuple[list[str], list[str]]:
        """팀을 확인하지 못한 출연진(멤버 실명 등) → MusicBrainz "member of band" 로 팀 이름 → (찾은 팀, 그래도 남은 이름).
        두 사람 이상이 같은 팀을 가리키거나, 그 사람의 팀이 하나뿐일 때만 팀으로 봅니다 (애매하면 남김 — 관리자가)."""
        names = list(dict.fromkeys(n.strip() for n in unmatched if n and n.strip()))
        bands: dict[str, list[str]] = {}
        for n in names[:MAX_CAST_LOOKUPS]:
            try:
                bands[n] = musicbrainz.bands_of(self.http, n)
            except Exception as e:
                self._fail("concert_cast", n, e)
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
        return list(dict.fromkeys(teams)), left

    def _tickets(self):
        """KOPIS 에 일반 예매 오픈 일시가 없는 인디 공연 → 웹 검색. 출처는 후보의 evidence 로 관리자에게.
        11/18 공연 정리와 나눔 — 검색 크레딧이 적어(Tavily 무료 월 1,000) 아직 안 한 공연 중 시작이 가까운 것부터
        TICKET_SEARCHES 안에서만 하고, 못 한 것은 다음 실행에 이어서. 원문(input_hash)이 같으면 한 번만 찾습니다 (ticket://<공연 id> 표시)."""
        if self.searcher is None:
            return
        with self.sessions() as s:
            done = {p.url: p.body for p in s.scalars(select(dbm.RawPage).where(dbm.RawPage.source == "ticket"))}
            todo = []
            for i in s.scalars(select(dbm.Item).where(dbm.Item.type == "CONCERT", dbm.Item.status.in_(("READY", "SENT")))):
                c = json.loads(i.payload_json)["concert"]
                starts = c.get("startsAt")
                if starts and datetime.fromisoformat(starts) < datetime.now(timezone.utc):
                    continue                                                 # 이미 시작한 공연은 찾지 않음
                if c.get("bookingOpenAt") is None and done.get(f"ticket://{i.key}") != i.input_hash:
                    todo.append((starts or "9999", i.key))
        first = getattr(self.searcher, "searches", 0)
        for n, (_, mt20id) in enumerate(sorted(todo)):
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
                mb = musicbrainz.find_artist(self.http, name.strip())
                if mb is None:
                    self.stats["artist_not_found"] += 1   # 동명이인 · 표기 차이로 애매하면 만들지 않음
                    log.info("crawl_artist_not_found name=%s", name)
                    continue
                if mb["mbid"] not in self._loaded:       # 다른 이름(한글 · 영어)으로 이미 본 아티스트
                    self._load(mb, name.strip())
            except Exception as e:
                self._fail("artist", name, e)

    def refresh_known(self):
        """전에 찾은 아티스트를 MusicBrainz id 로 다시 봄 — 시드에 없고 공연에 다시 안 나와도 신보 · 바뀐 값이 들어옵니다.
        요청은 캐시(아티스트 · 앨범 목록 7일)가 지난 것만 나갑니다."""
        with self.sessions() as s:
            mbids = s.scalars(select(dbm.ArtistLink.site_id).where(dbm.ArtistLink.site == "musicbrainz")
                              .order_by(dbm.ArtistLink.id)).all()
        for mbid in mbids:
            if mbid in self._loaded:
                continue
            try:
                self._load(musicbrainz.artist(self.http, mbid))
                self.stats["artist_refreshed"] += 1
            except Exception as e:
                self._fail("artist_refresh", mbid, e)

    def _load(self, mb: dict, asked: str | None = None):
        self._loaded.add(mb["mbid"])
        groups = musicbrainz.release_groups(self.http, mb["mbid"])
        if not groups:
            # 앨범이 하나도 없으면 만들지 않음 — 작사가 · 배우처럼 별칭만 같은 사람이 걸리는 것을 막음 (11/18 이지민 → 이하진)
            self.stats["artist_no_releases"] += 1
            log.info("crawl_artist_no_releases name=%s mbid=%s", asked or mb["name"], mb["mbid"])
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
                self._fail("discogs", mb["discogs"], e)
        elif self.settings.discogs_token:
            self.stats["discogs_no_link"] += 1          # MusicBrainz 에 Discogs 연결이 없으면 피지컬은 건드리지 않음

        source = {"mb": mb, "albums": [g["title"] for g in groups], "label": labels[0] if labels else None}
        self._discover(key, mb, names, groups, source["label"])
        # 장르는 아티스트가 같으면(id · 이름 · 별칭) 다시 묻지 않음 — 신보 하나로 장르가 흔들려 모든 앨범이 승인 대기로 돌아가는 것을 막음
        identity = content_hash({k: mb[k] for k in ("mbid", "name", "aliases")})
        with self.sessions() as s:
            found = self._found(s, key)
            item = self._item(s, "ARTIST", key)
            prev = json.loads(item.payload_json)["genres"] if item is not None else []
            if item is not None and item.input_hash == identity:
                genres, judged = prev, True
            elif self.llm is None:
                genres, judged = prev, False             # 키가 생기면 그때 물음
            else:
                asked_genres = self._genres(mb, source)  # None = 이번엔 못 물음 (거절 · 잘림) → 다음 실행에 다시
                genres, judged = (prev, False) if asked_genres is None else (asked_genres, True)
            goods, goods_evidence = _goods(found["GOODS"])
            artist = {
                "source": "crawler", "external_id": key,
                "name": mb["name"][:100], "genres": genres,
                "label": source["label"][:100] if source["label"] else None,
                "debut_year": min(years) if years else None,
                "profile_url": None,   # 공개 이미지 버킷이 정해지기 전까지 비움 (계약 3장)
                "goods": goods,        # 웹 검색으로 찾은 공식 굿즈 (가격은 관리자가) · 없으면 null = Catalog 가 건드리지 않음
            }
            if goods_evidence:
                artist["evidence"] = goods_evidence      # 출처 — 관리자 확인용 (Studio 가 Catalog 로 보낼 때 뺌)
            # 장르를 한 번도 정하지 못한 아티스트는 보내지 않고 기다림 (HOLD — 앨범도 아티스트가 간 뒤에) · 11/19 한도 소진으로 장르 없이 22팀이 갔음
            self._upsert(s, "ARTIST", key, artist, identity if judged else "not-judged",
                         origin=f"https://musicbrainz.org/artist/{mb['mbid']}", status="READY" if judged or genres else "HOLD")

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

    def _genres(self, mb: dict, source: dict) -> list[str] | None:
        try:
            return self.llm.artist_genres(mb["name"], mb["aliases"], source["albums"], source["label"])
        except LlmSkip as e:
            self.stats[f"artist_llm_{e}"] += 1
            return None

    # ---------- 전송 ----------

    def send(self):
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

    def _post(self, type_: str, path: str, items: list):
        """한 묶음 전송. 400 이면 반으로 나눠 다시 — 잘못된 한 건만 FAILED 로 남깁니다."""
        try:
            result = self.studio.post(path, {"items": [json.loads(i.payload_json) for i in items]})
        except StudioRejected as e:
            if e.status == 400 and len(items) > 1:
                mid = len(items) // 2
                self._post(type_, path, items[:mid])
                self._post(type_, path, items[mid:])
                return
            if e.status != 400:
                raise                                  # 401 · 5xx — 실행을 멈추고 다음 실행에 다시
            self._mark(items, "FAILED", error=str(e))
            self.stats[f"send_{type_.lower()}_failed"] += len(items)
            return
        by_key = {r["external_id"]: r["result"] for r in result.get("items", [])}
        self._mark(items, "SENT", results=by_key)
        for r in by_key.values():
            self.stats[f"send_{type_.lower()}_{r.lower()}"] += 1

    def _mark(self, items, status, error=None, results=None):
        with self.sessions() as s:
            for i in items:
                row = s.get(dbm.Item, i.id)
                row.status, row.error, row.updated_at = status, error, dbm.now()
                if status == "SENT":
                    row.sent_hash = row.source_hash
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
        elif item.source_hash == payload["source_hash"]:
            item.input_hash = input_hash
            return item                       # 내용 그대로 — 상태(SENT · FAILED)도 그대로
        item.payload_json = json.dumps(payload, ensure_ascii=False)
        item.input_hash, item.source_hash, item.origin = input_hash, payload["source_hash"], origin[:1024]
        item.status, item.error, item.updated_at = status, note, dbm.now()
        s.flush()
        return item

    def _fail(self, what, key, e):
        self.stats[f"{what}_error"] += 1
        log.warning("crawl_%s_failed item=%s error=%s", what, key, str(e)[:300])   # key= 는 로그 가림 필터가 비밀로 봄 (11/19)


def _same_album(title: str, titles: list[str]) -> str | None:
    """찾은 제목이 가리키는 앨범 제목 — 정규화해 같으면 그것, 아니면 한쪽이 다른 쪽을 품는 것(4자 이상)이 하나뿐일 때만
    ('정규 2집 POWER ANDRE 99' → 'POWER ANDRE 99'). 여럿이거나 없으면 None (붙이지 않음)."""
    t = norm(title)
    exact = [x for x in titles if norm(x) == t]
    if exact:
        return exact[0]
    hits = [x for x in titles if min(len(norm(x)), len(t)) >= 4 and (norm(x) in t or t in norm(x))]
    return hits[0] if len(hits) == 1 else None


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
        "description": None,
        "terms": None, "requirements": None,
        # KOPIS 주소 그대로 — Studio 가 승인할 때 우리 버킷으로 옮김 (못 옮기면 관리자가 직접 올림)
        "posterUrl": poster if _kopis_image(poster) else None,
        "imageUrls": [u for u in detail.get("styurls") or [] if _kopis_image(u)][:MAX_IMAGE_URLS],
        "sourceUrl": link,
    }
    concert["description"] = _description(detail, c, venue, concert)
    return concert


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
    lines += section("일정", [period, detail.get("dtguidance"), f"공연 시간 {detail['prfruntime']}" if detail.get("prfruntime") else None])
    lines += section("관람 안내", [f"관람 연령: {detail['prfage']}" if detail.get("prfage") else None, *(c.get("notices") or [])[:8]])
    lines += section("공연장", [place, venue.get("address"), f"{venue['seats']:,}석" if venue.get("seats") else None])
    lines += section("출연", [" · ".join(concert["artistNames"])])
    lines += section("주최 · 기획", [f"{' · '.join(roles)}: {name}" for name, roles in org.items()])
    if not lines:
        return None
    lines.append("정보 출처: 공연예술통합전산망(KOPIS)")
    return "\n".join(lines)[:20000]
