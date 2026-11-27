import json
from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select, update

from crawler import db as dbm
from crawler import runner
from crawler.pipeline import Pipeline, fit_kopis_dates, fit_sessions, kopis_sessions, to_concert
from crawler.webtools import SearchQuota

from conftest import FakeLlm, FakeStudio, FakeWeb, make_http, settings
from test_sources import KOPIS_DETAIL

ARTISTS = "/internal/crawl/catalog/artists"
ALBUMS = "/internal/crawl/catalog/albums"
CONCERTS = "/internal/crawl/concerts"


def web():
    w = FakeWeb([("/robots.txt", 200, "")])
    w.add("pblprfr?service=KEY", "<dbs><db><mt20id>PF100</mt20id></db><db><mt20id>PF200</mt20id></db></dbs>")
    w.add("pblprfr/PF100?", KOPIS_DETAIL)
    w.add("pblprfr/PF200?", "<dbs><db><mt20id>PF200</mt20id><prfnm>아이돌 콘서트</prfnm></db></dbs>")
    for name, mbid, rg, title in [("검정치마", "bs", "rgA", "201"), ("실리카겔", "sg", "rgB", "Kyo181")]:
        w.add(quote(name), {"artists": [{"id": mbid, "name": name if mbid == "bs" else "Silica Gel", "score": 100,
                                         "country": "KR", "aliases": [{"name": name}]}]})
        w.add(f"artist/{mbid}?", {"id": mbid, "name": name if mbid == "bs" else "Silica Gel", "aliases": [{"name": name}]})
        w.add(f"release-group?artist={mbid}", {"release-groups": [
            {"id": rg, "title": title, "primary-type": "Album", "first-release-date": "2011-05-10"}]})
        w.add(f"release?release-group={rg}", {"releases": [
            {"date": "2011-05-10", "media": [{"tracks": [{"title": "Antifreeze"}, {"title": "Dientes"}]}],
             "label-info": [{"label": {"name": "BESPOK"}}]}]})
    return w


INDIE = {"relevance": "INDIE", "title": "실리카겔 단독 공연", "type": "SOLO", "genre": "NONE",
         "startsAt": "2026-12-12T18:00:00+09:00", "endsAt": "", "bookingOpenAt": "", "bookingNotes": [], "price": 55000,
         "venueName": "무신사 개러지", "region": "SEOUL", "artistNames": ["실리카겔"], "unmatchedCast": [],
         "summary": "실리카겔의 단독 공연.", "notices": []}
MAINSTREAM = {**INDIE, "relevance": "MAINSTREAM", "title": "아이돌 콘서트", "artistNames": ["아이돌"]}


def pipeline(sessions, w, studio, llm=None):
    llm = llm or FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM},
                         genres={"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC", "INDIE_ROCK"]})
    return Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=studio)


def items(sessions, type_):
    with sessions() as s:
        return {i.key: i for i in s.scalars(select(dbm.Item).where(dbm.Item.type == type_))}


def test_dawn_collects_preprocesses_and_sends_in_contract_order(sessions):
    w, studio = web(), FakeStudio()
    p = pipeline(sessions, w, studio)
    out = runner.run("dawn", sessions, p, ["검정치마"])
    assert out["status"] == "DONE"

    assert [path for path, _ in studio.posts] == [ARTISTS, ALBUMS, CONCERTS]
    artists = {a["name"]: a for a in studio.posts[0][1]["items"]}
    assert set(artists) == {"검정치마", "Silica Gel"}           # 실리카겔은 인디 공연 출연진에서 찾음
    bs = artists["검정치마"]
    assert bs["source"] == "crawler" and bs["external_id"].startswith("a-")
    assert bs["genres"] == ["INDIE_ROCK"] and bs["label"] == "BESPOK" and bs["debut_year"] == 2011
    assert bs["goods"] is None and bs["profile_url"] is None and len(bs["source_hash"]) == 64
    assert bs["gender"] is None and bs["composition"] is None    # MusicBrainz 에 종류, 멤버가 없으면 null (관리자가 승인 때)

    albums = {a["title"]: a for a in studio.posts[1][1]["items"]}
    assert albums["201"]["artist_ref"] == {"source": "crawler", "external_id": bs["external_id"]}
    assert "artist_id" not in albums["201"]
    assert [t["track_no"] for t in albums["201"]["tracks"]] == [1, 2]
    assert albums["201"]["physical_albums"] is None              # Discogs 토큰 없음 -> 건드리지 않음

    concerts = studio.posts[2][1]["items"]
    assert [c["external_id"] for c in concerts] == ["PF100"]       # 아이돌 공연(MAINSTREAM)은 보내지 않음
    c = concerts[0]["concert"]
    assert c["title"] == "실리카겔 단독 공연" and c["price"] == 55000
    assert c["endsAt"] is None and c["genre"] is None and c["sourceUrl"] == "https://ticket.example/1"
    assert concerts[0]["source"] == "kopis"

    assert items(sessions, "CONCERT")["PF200"].status == "SKIPPED"
    assert all(i.status == "SENT" and i.studio_result == "CREATED" for i in items(sessions, "ARTIST").values())
    with sessions() as s:
        row = s.scalar(select(dbm.CrawlRun))
    assert row.status == "DONE" and json.loads(row.stats_json)["concert_indie"] == 1


def test_second_run_sends_nothing_and_skips_llm_then_only_changed_album_goes(sessions):
    w = web()
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"검정치마": ["INDIE_ROCK"]})
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), llm), ["검정치마"])
    llm_calls = len(llm.calls)

    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert studio.posts == [] and len(llm.calls) == llm_calls

    # MusicBrainz 의 트랙이 바뀜 (캐시가 지난 뒤) -> 그 앨범만 다시 감
    w.add("release?release-group=rgA", {"releases": [
        {"date": "2011-05-10", "media": [{"tracks": [{"title": "Antifreeze"}, {"title": "Dientes"}, {"title": "Bonus"}]}],
         "label-info": [{"label": {"name": "BESPOK"}}]}]})
    with sessions() as s:
        s.execute(update(dbm.RawPage).values(fetched_at=datetime(2000, 1, 1)))
        s.commit()
    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert [(path, [i["title"] for i in body["items"]]) for path, body in studio.posts] == [(ALBUMS, ["201"])]
    assert len(studio.posts[0][1]["items"][0]["tracks"]) == 3
    assert len(llm.calls) == llm_calls                              # 아티스트 원본(이름, 앨범 목록, 레이블)은 그대로


def test_english_name_goes_as_the_korean_name_and_artists_judged_before_are_asked_once_more(sessions):
    """영어 이름(Silica Gel)은 Gemini 가 고른 한글 활동명으로, 한글뿐인 이름(검정치마)은 그대로.
    한글 이름을 묻기 전에 장르만 정한 아티스트는 다음 실행에 한 번 다시 물음 (ARTIST_ASK), 못 물으면 전에 보낸 이름 그대로"""
    from crawler.llm import LlmSkip

    def set_old_input():
        with sessions() as s:
            s.execute(update(dbm.Item).where(dbm.Item.type == "ARTIST").values(input_hash="genres-only"))
            s.commit()

    w = web()
    genres = {"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC"]}
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres)),
               ["검정치마"])
    set_old_input()

    studio = FakeStudio()
    llm = FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres, names={"Silica Gel": "실리카겔", "검정치마": "검은 치마"})
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert sorted(c for c in llm.calls if c.startswith("artist:")) == ["artist:Silica Gel", "artist:검정치마"]
    assert [(path, [i["name"] for i in body["items"]]) for path, body in studio.posts] == [(ARTISTS, ["실리카겔"])]
    assert out["stats"]["artist_korean_name"] == 1                  # 앨범은 장르가 같아 다시 가지 않음

    class NoQuota(FakeLlm):
        def artist_info(self, *args, **kwargs):
            raise LlmSkip("quota")

    set_old_input()
    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio, NoQuota({"PF100": INDIE, "PF200": MAINSTREAM})), ["검정치마"])
    # 영어 이름으로 되돌아가지 않음, 판단 전 표시만 붙어 다시 감 (관리자 화면이 구별하게)
    assert [(path, sorted(i["name"] for i in body["items"])) for path, body in studio.posts] == [(ARTISTS, ["검정치마", "실리카겔"])]
    assert all([e["note"][:4] for e in i["evidence"] if e["for"] == "check"] == ["판단 전"] for i in studio.posts[0][1]["items"])
    assert {json.loads(i.payload_json)["name"] for i in items(sessions, "ARTIST").values()} == {"검정치마", "실리카겔"}
    assert {i.status for i in items(sessions, "ARTIST").values()} == {"SENT"}


def test_english_name_without_a_hangul_alias_is_looked_up_once_in_web_search(sessions):
    """한글 별칭이 없는 영어 이름만 검색 1번 (조각을 Gemini 에 근거로) - 한글 이름(검정치마), 한글 별칭이 있는 팀은 검색 안 함.
    검색 한도면 장르와 함께 다음 실행에 (영어 이름으로 보내지 않음)"""
    def english_only(w):
        w.add("artist/sg?", {"id": "sg", "name": "Silica Gel", "aliases": [{"name": "Silica Gel"}]})
        return w

    genres = {"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC"]}
    w = english_only(web())
    quota = FakeSearcher(mentions="quota")
    studio = FakeStudio()
    llm = FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres, names={"Silica Gel": "실리카겔"})
    out = runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm,
                                                studio=studio, searcher=quota), ["검정치마"])
    assert ("mentions", "Silica Gel") in quota.calls and ("mentions", "검정치마") not in quota.calls
    # 실행 끝 다시 묻기()에서 한 번 더 - 검색 한도라 또 다음 실행으로
    assert "artist:Silica Gel" not in llm.calls and out["stats"]["artist_name_search_deferred"] == 2
    assert {json.loads(i.payload_json)["name"]: i.status for i in items(sessions, "ARTIST").values()} == {
        "검정치마": "SENT", "Silica Gel": "HOLD"}                       # 장르, 이름을 못 정해 기다림
    assert [i["name"] for path, body in studio.posts if path == ARTISTS for i in body["items"]] == ["검정치마"]

    found = FakeSearcher(mentions={"Silica Gel": ["실리카겔 (Silica Gel) | 밴드 실리카겔의 정규 2집"]})
    studio = FakeStudio()
    out = runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, english_only(w)), settings(kopis_key="KEY"),
                                                llm=llm, studio=studio, searcher=found), ["검정치마"])
    assert [c for c in found.calls if c[0] == "mentions"] == [("mentions", "Silica Gel")]
    assert llm.web["Silica Gel"] == ["실리카겔 (Silica Gel) | 밴드 실리카겔의 정규 2집"] and llm.web["검정치마"] == []
    assert [i["name"] for path, body in studio.posts if path == ARTISTS for i in body["items"]] == ["실리카겔"]
    assert out["stats"]["artist_name_search"] == 1


def test_mainstream_and_hiphop_artists_are_skipped_without_albums_web_search_or_their_concert(sessions):
    """indig 가 다루지 않는 아티스트 - 대중 쪽(Gemini), 대표 장르가 힙합 -> 보내지 않고 앨범, 웹 검색도 없음.
    라인업 팀 절반 넘게가 뺀 팀이면 공연(PF100, 실리카겔 단독)도 보내지 않음"""
    w, studio, search = web(), FakeStudio(), FakeSearcher()
    llm = FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres={"검정치마": ["INDIE_ROCK"], "Silica Gel": ["HIPHOP", "RNB_SOUL"]},
                  mainstream={"검정치마"})
    out = runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm,
                                                studio=studio, searcher=search), ["검정치마"])
    assert out["status"] == "DONE" and studio.posts == []
    assert {json.loads(i.payload_json)["name"]: (i.status, i.error) for i in items(sessions, "ARTIST").values()} == {
        "검정치마": ("SKIPPED", "MAINSTREAM"), "Silica Gel": ("SKIPPED", "HIPHOP")}
    assert items(sessions, "ALBUM") == {} and not [c for c in search.calls if c[0] in ("releases", "goods")]
    assert (items(sessions, "CONCERT")["PF100"].status, items(sessions, "CONCERT")["PF100"].error) == ("SKIPPED", "LINEUP_HIPHOP")
    st = out["stats"]
    assert (st["artist_skipped_mainstream"], st["artist_skipped_hiphop"], st["concert_skipped_lineup"]) == (1, 1, 1)


def test_artist_sent_before_and_now_judged_mainstream_is_skipped_once_and_stays_skipped(sessions):
    """이미 보낸 팀을 다시 물어(ARTIST_ASK) 대중 쪽이면 내용이 같아도 SKIPPED, 그 팀 공연도 (Studio 대기열은 관리자가 거절).
    Studio 에는 '크롤러가 뺌' 표시만 한 번 보냄 (상태는 SKIPPED 그대로). 한 번 정하면 다시 묻지 않고 SKIPPED 그대로"""
    w = web()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), ["검정치마"])
    with sessions() as s:
        s.execute(update(dbm.Item).where(dbm.Item.type == "ARTIST").values(input_hash="genres-only"))
        s.commit()
    genres = {"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC", "INDIE_ROCK"]}
    studio = FakeStudio()
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres,
                                                                          mainstream={"Silica Gel"})), ["검정치마"])
    assert [(path, [i["external_id"] for i in body["items"]]) for path, body in studio.posts] == [
        (ARTISTS, [items(sessions, "ARTIST")[k].key for k in items(sessions, "ARTIST") if items(sessions, "ARTIST")[k].status == "SKIPPED"]),
        (CONCERTS, ["PF100"])]
    artist = studio.posts[0][1]["items"][0]
    assert [e["note"] for e in artist["evidence"] if e["for"] == "check"] == ["크롤러가 뺌 (대중 가수 · 공연) — 승인하지 말고 거절"]
    assert [e["for"] for e in artist["evidence"]][0] == "identity"
    assert studio.posts[1][1]["items"][0]["concert"]["reviewNotes"][-1] == \
        "[크롤러] 크롤러가 뺌 (대중 가수 · 공연 · 라인업 대부분이 뺀 팀) — 승인하지 말고 거절"
    artists = {json.loads(i.payload_json)["name"]: i.status for i in items(sessions, "ARTIST").values()}
    assert artists == {"검정치마": "SENT", "Silica Gel": "SKIPPED"}
    assert items(sessions, "CONCERT")["PF100"].status == "SKIPPED" and out["stats"]["concert_skipped_after_sent"] == 1

    llm = FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres)          # 이번엔 INDIE 라고 해도 다시 묻지 않음
    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert "artist:Silica Gel" not in llm.calls
    assert studio.posts == []                                         # 표시는 한 번만
    assert {json.loads(i.payload_json)["name"]: i.status for i in items(sessions, "ARTIST").values()}["Silica Gel"] == "SKIPPED"


def test_known_artist_is_refreshed_without_seed_or_concert_and_genres_stay(sessions):
    """공연에서 찾은 실리카겔은 시드에 없음 - 그래도 다음 새벽에 신보가 들어오고, 장르는 다시 묻지 않음 (모든 앨범이 다시 가지 않게)"""
    w = web()
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"Silica Gel": ["PSYCHEDELIC"]})
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), llm), ["검정치마"])
    llm_calls = len(llm.calls)

    new = (date.today() - timedelta(days=20)).isoformat()
    w.add("release-group?artist=sg", {"release-groups": [
        {"id": "rgB", "title": "Kyo181", "primary-type": "Album", "first-release-date": "2011-05-10"},
        {"id": "rgC", "title": "Ballad of You", "primary-type": "Album", "first-release-date": new}]})
    w.add("release?release-group=rgC", {"releases": [{"date": new, "media": [{"tracks": [{"title": "Ballad"}]}],
                                                      "label-info": [{"label": {"name": "CAM"}}]}]})
    with sessions() as s:                                            # 앨범 목록 캐시(7일)가 지남
        s.execute(update(dbm.RawPage).values(fetched_at=dbm.now() - timedelta(days=8)))
        s.commit()
    studio = FakeStudio()
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), [])
    assert out["stats"]["artist_refreshed"] == 2
    sent = {path: [i["external_id"] for i in body["items"]] for path, body in studio.posts}
    assert sent[ALBUMS] == ["b-rgC"]                                 # 새 앨범만 - 옛 앨범, 장르는 그대로
    assert [a["label"] for a in studio.posts[0][1]["items"]] == ["CAM"]   # 아티스트는 레이블이 바뀌어서만 다시 감
    assert studio.posts[0][1]["items"][0]["genres"] == ["PSYCHEDELIC"] and len(llm.calls) == llm_calls

    # 최근 앨범(rgC)은 7일 지나면 트랙, 레이블을 다시 보고, 2011년 앨범(rgB)은 90일까지 캐시
    w.calls.clear()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), llm), [])
    assert not any("release?release-group=rg" in c for c in w.calls)   # 방금 받아서 둘 다 캐시
    with sessions() as s:
        s.execute(update(dbm.RawPage).values(fetched_at=dbm.now() - timedelta(days=8)))
        s.commit()
    w.calls.clear()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), llm), [])
    assert any("release?release-group=rgC" in c for c in w.calls)
    assert not any("release?release-group=rgB" in c for c in w.calls)


def test_gender_and_composition_follow_musicbrainz_lineup_and_resend_only_the_artist(sessions):
    """성별, 구성 - 처음엔 MusicBrainz 에 멤버가 없어 null, 멤버가 생기면 아티스트만 다시 감 (해시가 바뀜, 장르는 다시 묻지 않음)"""
    w = web()
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"검정치마": ["INDIE_ROCK"]})
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), llm), ["검정치마"])
    llm_calls, before = len(llm.calls), items(sessions, "ARTIST")
    bs_key = next(k for k, i in before.items() if json.loads(i.payload_json)["name"] == "검정치마")

    w.add("artist/bs?", {"id": "bs", "name": "검정치마", "aliases": [{"name": "검정치마"}], "type": "Group", "relations": [
        {"type": "member of band", "direction": "backward", "ended": False, "artist": {"id": "jo", "name": "조휴일"}},
        {"type": "member of band", "direction": "backward", "ended": True, "artist": {"id": "ex", "name": "전 멤버"}}]})
    w.add("artist/jo?fmt=json", {"id": "jo", "type": "Person", "gender": "Male"})
    with sessions() as s:                                            # 아티스트 캐시(7일)가 지남
        s.execute(update(dbm.RawPage).values(fetched_at=dbm.now() - timedelta(days=8)))
        s.commit()
    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert [path for path, _ in studio.posts] == [ARTISTS]          # 앨범은 그대로
    bs = studio.posts[0][1]["items"][0]
    assert (bs["name"], bs["gender"], bs["composition"]) == ("검정치마", "MALE", "SOLO")   # 한 명만 남은 팀 -> 솔로
    assert bs["genres"] == ["INDIE_ROCK"] and len(llm.calls) == llm_calls
    assert bs["source_hash"] != before[bs_key].source_hash


def test_alias_only_person_without_releases_is_not_created(sessions):
    """실측: '이지민' 이 별칭인 작사가, 배우(앨범 0개)가 걸림 - 앨범이 없으면 아티스트를 만들지 않음"""
    w = web()
    w.add(quote("이지민"), {"artists": [{"id": "lj", "name": "이하진", "score": 100, "country": "KR",
                                         "aliases": [{"name": "이지민"}]}]})
    w.add("artist/lj?", {"id": "lj", "name": "이하진", "aliases": [{"name": "이지민"}]})
    w.add("release-group?artist=lj", {"release-groups": []})
    p = pipeline(sessions, w, FakeStudio())
    p.artists(["이지민"])
    assert p.stats["artist_no_releases"] == 1 and items(sessions, "ARTIST") == {}
    with sessions() as s:
        assert dbm.lookup_artist_key(s, "이지민") is None              # 다음에 다시 찾을 수 있게 연결도 남기지 않음


def test_missed_names_step_aside_for_30_days_so_later_names_get_their_turn(sessions, monkeypatch):
    """실측: 새 이름 칸(50)을 매번 같은 실패 이름 50개가 차지해 뒤의 483개를 한 번도 못 보고 39팀에서 멈췄음"""
    w = web()
    w.add(quote("없는팀"), {"artists": []})
    w.add(quote("이지민"), {"artists": [{"id": "lj", "name": "이하진", "score": 100, "country": "KR",
                                         "aliases": [{"name": "이지민"}]}]})
    w.add("artist/lj?", {"id": "lj", "name": "이하진", "aliases": [{"name": "이지민"}]})
    w.add("release-group?artist=lj", {"release-groups": []})
    p = pipeline(sessions, w, FakeStudio())
    p.artists(["없는팀", "이지민"])
    assert p.stats["artist_not_found"] == 1 and p.stats["artist_no_releases"] == 1

    monkeypatch.setattr(runner, "MAX_NEW_ARTISTS", 1)
    assert runner._new_names(sessions, ["없는팀", "이지민", "실리카겔"]) == ["실리카겔"]   # 칸이 하나여도 안 본 이름 차례

    with sessions() as s:
        s.execute(update(dbm.ArtistMiss).where(dbm.ArtistMiss.name == "없는팀")
                  .values(checked_at=dbm.now() - timedelta(days=31)))
        s.commit()
    assert runner._new_names(sessions, ["없는팀", "이지민", "실리카겔"]) == ["없는팀"]     # 30일이 지나면 다시 찾음


class Flaky:
    """처음 fails 번은 part 가 든 주소에 500 - 그다음부터는 web 그대로 (외부 서버가 잠깐 안 되는 경우)"""

    def __init__(self, web, part, fails=1):
        self.web, self.part, self.left = web, part, fails

    def __call__(self, url, headers):
        if self.part in url and self.left > 0:
            self.left -= 1
            self.web.calls.append(url)
            return 500, "boom"
        return self.web(url, headers)


HYUKOH = [(quote("혁오"), {"artists": [{"id": "hy", "name": "혁오", "score": 100, "country": "KR", "aliases": []}]}),
          ("artist/hy?", {"id": "hy", "name": "혁오", "aliases": []}),
          ("release-group?artist=hy", {"release-groups": [{"id": "rgH", "title": "22", "primary-type": "EP",
                                                          "first-release-date": "2014-05-29"}]}),
          ("release?release-group=rgH", {"releases": [{"date": "2014-05-29", "media": [{"tracks": [{"title": "Hooka"}]}]}]})]


def retries(sessions):
    with sessions() as s:
        return {(r.kind, r.key): r.tries for r in s.scalars(select(dbm.Retry))}


def test_name_that_failed_on_a_server_error_is_tried_again_at_the_end_of_the_run(sessions):
    """아웃박스 - MusicBrainz 가 잠깐 오류면 못 찾음(miss)으로 두지 않고 같은 실행 끝에 한 번 더"""
    w = web()
    for part, body in HYUKOH:
        w.add(part, body)
    p = Pipeline(sessions, make_http(sessions, Flaky(w, quote("혁오"))), settings(kopis_key="KEY"),
                 llm=FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}), studio=FakeStudio())
    out = runner.run("dawn", sessions, p, ["혁오"])
    assert out["stats"]["artist_error"] == 1 and out["stats"]["retry_name_queued"] == 1 and out["stats"]["retry_name_done"] == 1
    assert "혁오" in {json.loads(i.payload_json)["name"] for i in items(sessions, "ARTIST").values()}
    assert retries(sessions) == {} and out["stats"]["retry_left"] == 0
    with sessions() as s:
        assert s.scalar(select(dbm.ArtistMiss)) is None                 # 오류는 못 찾음이 아님 (30일 건너뛰지 않음)


def test_name_still_failing_is_kept_and_looked_up_first_in_the_next_run(sessions):
    w = web()
    w.add(quote("혁오"), "boom", status=500)
    p = pipeline(sessions, w, FakeStudio())
    p.artists(["혁오"])
    assert retries(sessions) == {("NAME", "혁오"): 1}

    for part, body in HYUKOH:                                           # 다음 실행 - 서버가 돌아옴
        w.add(part, body)
    w.calls.clear()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), [])
    searches = [u for u in w.calls if "artist?query=" in u]
    assert quote("혁오") in searches[0]                                 # 공연 출연진(실리카겔)보다 먼저
    assert retries(sessions) == {}


def test_concert_whose_cast_lookup_failed_is_cleaned_up_again_even_if_the_source_is_the_same(sessions):
    """원문이 같으면 다시 정리하지 않는데, 출연진 -> 팀 조회가 실패한 공연은 영영 다시 안 보던 것 """
    w = web()
    for person, pid in [("박솔", "p1"), ("강경태", "p2")]:
        w.add(quote(person), {"artists": [{"id": pid, "name": person, "score": 100, "country": "KR", "aliases": []}]})
        w.add(f"artist/{pid}?inc=artist-rels", {"relations": [{"type": "member of band", "direction": "forward", "artist": {"name": "Band A"}}]})
    llm = FakeLlm(concerts={"PF100": {**INDIE, "artistNames": [], "unmatchedCast": ["박솔", "강경태"]}, "PF200": MAINSTREAM})
    flaky = Flaky(w, "artist/p1?inc=artist-rels", fails=2)                  # 실행 안에서 두 번(처음, 실행 끝) 다 실패
    runner.run("lunch", sessions, Pipeline(sessions, make_http(sessions, flaky), settings(kopis_key="KEY"), llm=llm,
                                           studio=FakeStudio()), [])
    assert retries(sessions) == {("CONCERT", "PF100"): 2}
    assert json.loads(items(sessions, "CONCERT")["PF100"].payload_json)["concert"]["artistNames"] == ["Band A"]   # 강경태만으로

    studio = FakeStudio()
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])   # 원문 그대로, 조회는 이제 됨
    assert llm.calls.count("concert:PF100") == 3                          # 첫 실행 2번(처음, 끝) + 다음 실행 1번
    assert retries(sessions) == {}


def test_concert_skipped_by_the_gemini_daily_limit_waits_without_counting_as_a_failure(sessions):
    class Exhausted(FakeLlm):
        exhausted, stop_reason = True, "quota"
    runner.run("lunch", sessions, pipeline(sessions, web(), FakeStudio(), Exhausted()), [])
    assert retries(sessions) == {("CONCERT", "PF100"): 0, ("CONCERT", "PF200"): 0}   # 한도 탓 - 실패로 세지 않음
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, web(), FakeStudio(), llm), [])
    assert sorted(llm.calls) == ["concert:PF100", "concert:PF200"] and retries(sessions) == {}


def test_search_budget_spreads_the_month_over_the_days_left(sessions):
    """검색 한도 - 이번 달 남은 크레딧(1,000 - 예비 100 - 쓴 것)을 남은 날수로 나눈 것이 하루 몫, 실행 횟수와 무관"""
    now = datetime(2026, 11, 23, 13, 0, tzinfo=runner.KST)                 # ~ = 8일
    utc = lambda d: d.astimezone(runner.timezone.utc).replace(tzinfo=None)

    def run(when, n):
        with sessions() as s:
            s.add(dbm.CrawlRun(kind="dawn", status="DONE", started_at=utc(when), stats_json=json.dumps({"web_search_requests": n})))
            s.commit()

    assert runner.search_budget(sessions, now) == 40                     # 900 // 8 = 112 -> 실행당 40
    run(datetime(2026, 10, 31, 12, 0, tzinfo=runner.KST), 500)            # 지난달 - 세지 않음
    run(datetime(2026, 11, 20, 3, 30, tzinfo=runner.KST), 143)            # 이번 달 앞선 날 -> 하루 (900 - 143) // 8 = 94
    run(datetime(2026, 11, 23, 3, 30, tzinfo=runner.KST), 40)
    assert runner.search_budget(sessions, now) == 40                     # 오늘 남은 54 -> 실행당 40
    run(datetime(2026, 11, 23, 11, 0, tzinfo=runner.KST), 40)
    assert runner.search_budget(sessions, now) == 14                     # 94 - 80
    run(datetime(2026, 11, 23, 12, 0, tzinfo=runner.KST), 14)
    assert runner.search_budget(sessions, now) == 0
    assert runner.search_budget(sessions, datetime(2026, 11, 24, 3, 30, tzinfo=runner.KST)) == 40   # 다음 날 (900-237)//7 = 94


def test_discogs_only_through_musicbrainz_link(sessions):
    w = web()
    w.add("artist/bs?", {"id": "bs", "name": "검정치마", "aliases": [],
                         "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/9"}}]})
    w.add("api.discogs.com/artists/9", {"id": 9, "name": "The Black Skirts"})
    w.add("database/search?artist=The%20Black%20Skirts", {"results": [{"id": 5, "format": ["CD"]}]})
    w.add("api.discogs.com/releases/5", {"id": 5, "title": "201", "artists": [{"id": 9}], "released": "2011-05-10",
                                         "formats": [{"name": "CD"}]})
    p = Pipeline(sessions, make_http(sessions, w), settings(discogs_token="T"), llm=None, studio=FakeStudio())
    p.artists(["검정치마", "실리카겔"])
    albums = {json.loads(i.payload_json)["title"]: json.loads(i.payload_json) for i in items(sessions, "ALBUM").values()}
    assert [x["external_id"] for x in albums["201"]["physical_albums"]] == ["discogs-5"]
    assert albums["Kyo181"]["physical_albums"] is None                # 실리카겔은 MusicBrainz 에 Discogs 연결 없음 -> 건드리지 않음
    assert p.stats["discogs_no_link"] == 1
    assert not any("database/search?artist=Silica" in c for c in w.calls)   # 이름으로 찾지 않음


def test_bad_item_is_isolated_and_not_resent(sessions):
    w = web()
    studio = FakeStudio(reject="b-rgA")
    runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    albums = items(sessions, "ALBUM")
    assert albums["b-rgA"].status == "FAILED" and "400" in albums["b-rgA"].error
    assert albums["b-rgB"].status == "SENT"
    assert [i["external_id"] for i in studio.posts[1][1]["items"]] == ["b-rgB"]

    studio2 = FakeStudio()
    runner.run("lunch", sessions, pipeline(sessions, w, studio2), [])
    assert studio2.posts == []                                      # 고치기 전에는 다시 보내지 않음


def test_lunch_is_concerts_only_and_dry_run_keeps_items_ready(sessions):
    w = web()
    p = pipeline(sessions, w, studio=None)
    out = runner.run("lunch", sessions, p, ["검정치마"])
    assert out["stats"]["send_dry_run"] == 1
    assert items(sessions, "ARTIST") == {}                          # 점심에는 아티스트를 찾지 않음
    assert items(sessions, "CONCERT")["PF100"].status == "READY"
    assert not any("musicbrainz" in c for c in w.calls)


def test_running_run_blocks_the_next(sessions):
    with sessions() as s:
        s.add(dbm.CrawlRun(kind="dawn", status="RUNNING", started_at=dbm.now()))
        s.commit()
    studio = FakeStudio()
    assert "skipped" in runner.run("lunch", sessions, pipeline(sessions, web(), studio), [])
    assert studio.posts == []


def test_missing_keys_degrade_instead_of_crashing(sessions):
    p = Pipeline(sessions, make_http(sessions, web()), settings(), llm=None, studio=FakeStudio())
    out = runner.run("dawn", sessions, p, ["검정치마"])
    assert out["status"] == "DONE"
    assert out["stats"]["concert_no_kopis_key"] == 1
    assert out["stats"]["artist_new_names"] == 0                       # 공연, 인디스트릿에서 새 이름 없음 (연달아 돌릴 때 멈추는 기준)
    a = json.loads(next(iter(items(sessions, "ARTIST").values())).payload_json)
    assert a["genres"] == []                                        # 키가 없으면 장르는 비우고 관리자가


def test_to_concert_turns_unknowns_into_null():
    c = to_concert({"prfnm": "원래 제목", "fcltynm": "원래 공연장", "poster": "http://evil.example/p.png"},
                   {**INDIE, "title": "", "venueName": "", "type": "FESTIVAL", "genre": "SHOEGAZE",
                    "startsAt": "12월 12일", "price": -1, "region": "UNKNOWN", "artistNames": [" A ", "", "B"]})
    assert c["title"] == "원래 제목" and c["venueName"] == "원래 공연장"
    assert c["genre"] == "SHOEGAZE" and c["startsAt"] is None and c["price"] is None and c["region"] is None
    assert c["artistNames"] == ["A", "B"] and c["posterUrl"] is None and c["imageUrls"] == []   # KOPIS 밖 이미지는 넣지 않음
    assert c["venueAddress"] is None and c["description"].startswith("실리카겔의 단독 공연.")


FACILITY = """<dbs><db><fcltynm>무신사 개러지</fcltynm><mt10id>FC100</mt10id><mt13cnt>2</mt13cnt><seatscale>900</seatscale>
<adres>서울특별시 마포구 와우산로 66</adres><mt13s>
<mt13><prfplcnm>공연장</prfplcnm><mt13id>FC100-01</mt13id><seatscale>424</seatscale></mt13>
<mt13><prfplcnm>소극장</prfplcnm><mt13id>FC100-02</mt13id><seatscale>100</seatscale></mt13></mt13s></db></dbs>"""


def png(width, height) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def expire_kopis(sessions):
    with sessions() as s:
        s.execute(update(dbm.RawPage).values(fetched_at=datetime(2000, 1, 1)))
        s.commit()


def test_concert_reads_poster_and_intro_images_and_builds_description(sessions):
    w, studio = web(), FakeStudio()
    w.add("prfplc/FC100", FACILITY)
    w.add_bytes("pfmPoster/PF_PF100", png(800, 1100))
    w.add_bytes("pfmIntroImage/PF_PF100_1", png(1200, 4000))      # 세로로 긴 소개 이미지 -> 1000px 로 줄여 3조각
    llm = FakeLlm(concerts={"PF100": {**INDIE, "bookingOpenAt": "2026-11-20T20:00:00+09:00",
                                      "bookingNotes": ["팬클럽 선예매 11.19(목) 20:00"], "notices": ["공연 시작 30분 전 입장"]},
                            "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])

    assert llm.images["PF100"] == 1 + 3
    assert not any("elsewhere.example" in u for u in w.calls)     # KOPIS 에 올라온 이미지만 받음
    facts = llm.facts["PF100"]
    assert "poster" not in facts["detail"] and "styurls" not in facts["detail"]
    assert facts["venue"] == {"name": "무신사 개러지", "hall": "공연장", "address": "서울특별시 마포구 와우산로 66", "seats": 424}

    [item] = studio.posts[0][1]["items"]
    c = item["concert"]
    assert c["posterUrl"] == "http://www.kopis.or.kr/upload/pfmPoster/PF_PF100.gif"
    assert c["imageUrls"] == ["http://www.kopis.or.kr/upload/pfmIntroImage/PF_PF100_1.jpg"]
    assert c["bookingOpenAt"] == "2026-11-20T20:00:00+09:00" and c["venueAddress"] == "서울특별시 마포구 와우산로 66"
    assert c["bookingNotes"] == ["팬클럽 선예매 11.19(목) 20:00"] and c["price"] == 55000   # 선예매는 관리자 참고 칸
    assert c["description"] == "\n".join([
        "실리카겔의 단독 공연.", "",
        "## 일정", "- 2026.12.12 ~ 2026.12.13", "- 토요일(18:00), 일요일(17:00)", "",
        "## 관람 안내", "- 관람 연령: 만 12세 이상", "- 공연 시작 30분 전 입장", "",
        "## 공연장", "- 무신사 개러지 (공연장)", "- 서울특별시 마포구 와우산로 66", "- 424석", "",
        "## 출연", "- 실리카겔", "",
        "## 주최 · 기획", "- 주최 · 기획: (주)파스텔뮤직", "",
        "정보 출처: 공연예술통합전산망(KOPIS)"])                    # 가격, 예매처 안내는 넣지 않음 (indig 가 직접 판매)


def test_description_keeps_what_intro_images_say_beyond_the_summary(sessions):
    """소개 이미지의 출연진 소개, 곡목, 타임테이블, 부대 행사 -> 상세 설명. 관람 연령만 다시 적은 줄, 개인 정보 문장은 뺌"""
    w, studio = web(), FakeStudio()
    llm = FakeLlm(concerts={"PF100": {**INDIE,
        "performers": [{"name": "실리카겔", "intro": "2013년 결성한 4인조 밴드다. 멤버 모두 같은 대학을 졸업했다."},
                       {"name": "게스트", "intro": ""}],
        "program": ["1부 · 정규 2집 수록곡", " 2부 ·  신곡 ", "2부 · 신곡"],
        "timetable": ["12/12(토) MAIN 18:00 실리카겔"],
        "events": ["공연 후 사인회 (앨범 구매자)"],
        "notices": ["만 12세 이상 관람 가능", "입장 시 신분증으로 연령 확인", "공연 시작 30분 전 입장"]},
        "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    [item] = studio.posts[0][1]["items"]
    d = item["concert"]["description"]
    assert d.startswith("\n".join(["실리카겔의 단독 공연.", "",
        "## 출연진 소개", "- **실리카겔** — 2013년 결성한 4인조 밴드다.", "",       # 학교 문장, 소개 없는 게스트는 뺌
        "## 프로그램", "- 1부 · 정규 2집 수록곡", "- 2부 · 신곡", "",
        "## 타임테이블", "- 12/12(토) MAIN 18:00 실리카겔", "",
        "## 일정"]))
    assert "\n".join(["## 관람 안내", "- 관람 연령: 만 12세 이상", "- 입장 시 신분증으로 연령 확인", "- 공연 시작 30분 전 입장", "",
                      "## 부대 행사", "- 공연 후 사인회 (앨범 구매자)", "", "## 공연장"]) in d


def test_only_age_lines_are_dropped_and_lines_with_more_are_kept():
    from crawler.pipeline import _only_age
    assert all(_only_age(n) for n in ["만 7세 이상 관람 가능", "전체 관람가", "본 공연은 7세 이상 관람가입니다.", "만 12세 이상 입장 가능"])
    assert not any(_only_age(n) for n in ["입장 시 신분증으로 연령 확인", "공연 시작 30분 전 입장", "8세 이상 관람가 (2018년생부터)",
                                          "전체 관람가 (7세 미만 무료입장)"])


def test_concerts_read_the_old_way_are_read_again_a_few_per_run(sessions, monkeypatch):
    """CONCERT_VERSION 3 - 원문이 같아도 실행마다 CONCERT_REREAD 건까지 다시 정리, 뺀 공연(SKIPPED)은 다시 보지 않음"""
    from crawler import pipeline as pl
    w = web()
    monkeypatch.setattr(pl, "CONCERT_VERSION", 2)
    runner.run("lunch", sessions, pipeline(sessions, w, FakeStudio()), [])
    monkeypatch.setattr(pl, "CONCERT_VERSION", 3)
    monkeypatch.setattr(pl, "CONCERT_REREAD", 0)
    expire_kopis(sessions)
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM})
    p = pipeline(sessions, w, FakeStudio(), llm)
    runner.run("lunch", sessions, p, [])
    assert not any(c.startswith("concert:") for c in llm.calls) and p.stats["concert_reread_deferred"] == 1   # 몫이 없으면 다음에
    monkeypatch.setattr(pl, "CONCERT_REREAD", 5)
    expire_kopis(sessions)
    llm = FakeLlm(concerts={"PF100": {**INDIE, "program": ["1부"]}, "PF200": MAINSTREAM})
    studio = FakeStudio()
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    assert [c for c in llm.calls if c.startswith("concert:")] == ["concert:PF100"]                   # 뺀 PF200 은 다시 묻지 않음
    [item] = studio.posts[0][1]["items"]
    assert "## 프로그램\n- 1부" in item["concert"]["description"]
    expire_kopis(sessions)
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, FakeStudio(), llm), [])
    assert not any(c.startswith("concert:") for c in llm.calls)                                       # 다시 정리한 것은 한 번만


def test_changed_kopis_detail_is_sent_again_and_same_detail_is_not(sessions):
    w = web()
    runner.run("lunch", sessions, pipeline(sessions, w, FakeStudio()), [])
    expire_kopis(sessions)
    w.add("pblprfr/PF100?", KOPIS_DETAIL.replace("토요일(18:00)", "토요일(19:00)"))   # 출처에서 시간이 바뀜
    studio, llm = FakeStudio(), FakeLlm(concerts={"PF100": {**INDIE, "startsAt": "2026-12-12T19:00:00+09:00"}, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    assert llm.calls == ["concert:PF100"]                         # 그대로인 PF200 은 다시 묻지 않음
    [item] = studio.posts[0][1]["items"]                          # 다시 보냄 -> 등록된 공연이면 Studio 가 CHANGED
    assert item["concert"]["startsAt"] == "2026-12-12T19:00:00+09:00"


def test_known_artists_seen_in_concert_text_go_to_haiku_as_hints(sessions):
    w = web()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), ["검정치마"])   # 검정치마, Silica Gel 을 앎
    expire_kopis(sessions)
    w.add("pblprfr/PF100?", KOPIS_DETAIL.replace("<prfcast>실리카겔</prfcast>", "<prfcast>김춘추, 최웅희</prfcast>"))
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, FakeStudio(), llm), [])
    assert llm.facts["PF100"]["knownArtists"] == ["Silica Gel"]   # 제목의 "실리카겔"(별칭) -> 대표 이름


def test_member_names_become_a_team_only_when_musicbrainz_agrees(sessions):
    w = FakeWeb([("/robots.txt", 200, "")])
    for person, pid, bands in [("박솔", "p1", ["Band A"]), ("강경태", "p2", ["Band A", "Side B"]), ("권오경", "p3", ["Band B", "Band C"])]:
        w.add(quote(person), {"artists": [{"id": pid, "name": person, "score": 100, "country": "KR", "aliases": []}]})
        w.add(f"artist/{pid}?inc=artist-rels", {"relations": [
            {"type": "member of band", "direction": "forward", "artist": {"name": b}} for b in bands]})
    p = pipeline(sessions, w, FakeStudio())
    # 박솔, 강경태 -> 둘 다 Band A (한 번만), 권오경 -> 팀이 둘이라 애매 -> 남김, 김없음 -> MusicBrainz 에 없음 -> 남김
    assert p._teams_for(["박솔", "강경태", "권오경", "김없음"]) == (["Band A"], ["권오경", "김없음"], True)   # 김없음 조회 404 = 실패



def test_when_no_team_is_confirmed_member_names_go_through_musicbrainz(sessions):
    w, studio = web(), FakeStudio()
    for person, pid in [("박솔", "p1"), ("강경태", "p2")]:
        w.add(quote(person), {"artists": [{"id": pid, "name": person, "score": 100, "country": "KR", "aliases": []}]})
        w.add(f"artist/{pid}?inc=artist-rels", {"relations": [{"type": "member of band", "direction": "forward", "artist": {"name": "Band A"}}]})
    llm = FakeLlm(concerts={"PF100": {**INDIE, "artistNames": [], "unmatchedCast": ["박솔", "강경태", "김없음"]}, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    c = studio.posts[0][1]["items"][0]["concert"]
    assert c["artistNames"] == ["Band A"] and c["unmatchedCast"] == ["김없음"]


class FakeTicketSearch:
    def __init__(self, found):
        self.found, self.asked = found, []

    def ticket_open(self, concert):
        self.asked.append(concert)
        return self.found


def test_unconfirmed_cast_stays_out_of_lineup_and_missing_open_time_is_searched(sessions):
    w, studio = web(), FakeStudio()
    evidence = [{"url": "https://label.example/notice", "title": "실리카겔 단독 공연 티켓 오픈", "quote": "11월 20일(금) 오후 8시 오픈"}]
    search = FakeTicketSearch(("2026-11-20T20:00:00+09:00", evidence))
    llm = FakeLlm(concerts={"PF100": {**INDIE, "artistNames": ["실리카겔"], "unmatchedCast": ["양영호"]}, "PF200": MAINSTREAM})
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=studio, searcher=search)
    runner.run("lunch", sessions, p, [])
    [item] = studio.posts[0][1]["items"]
    c = item["concert"]
    assert c["artistNames"] == ["실리카겔"] and c["unmatchedCast"] == ["양영호"]     # 세션 등 확인 못 한 이름은 관리자 참고 칸에만
    assert search.asked == [{"title": "실리카겔 단독 공연", "artistNames": ["실리카겔"], "venueName": "무신사 개러지",
                             "startsAt": "2026-12-12T18:00:00+09:00"}]          # 인디 공연만 (PF200 은 묻지 않음)
    assert c["bookingOpenAt"] == "2026-11-20T20:00:00+09:00"
    assert c["evidence"] == [{"for": "bookingOpenAt", **evidence[0]}]
    assert not any("musicbrainz" in u for u in w.calls)              # 팀을 확인했으면 남은 이름을 MusicBrainz 에서 찾지 않음



class LimitedTicketSearch:
    """한 번 찾을 때마다 검색 3번을 쓰는 척 - limit 번째부터는 한도가 다 찬 것처럼"""
    def __init__(self, limit):
        self.limit, self.asked, self.searches = limit, [], 0

    @property
    def exhausted(self):
        return len(self.asked) >= self.limit

    def ticket_open(self, concert):
        self.asked.append(concert["title"])
        self.searches += 3
        return None


def test_ticket_search_goes_soonest_first_but_this_week_last_and_carries_over_to_the_next_run(sessions):
    w = web()
    ids = ["PF100", "PF200", "PF300", "PF400"]
    w.add("pblprfr?service=KEY", "<dbs>" + "".join(f"<db><mt20id>{i}</mt20id></db>" for i in ids) + "</dbs>")
    def dated(pf, day):   # KOPIS 기간을 그 공연 날짜로 - 원문 기간 밖 날짜는 크롤러가 고치므로
        kopis_day = day.strftime("%Y.%m.%d")
        return KOPIS_DETAIL.replace("PF100", pf).replace("2026.12.12</prfpdfrom><prfpdto>2026.12.13",
                                                         f"{kopis_day}</prfpdfrom><prfpdto>{kopis_day}")
    today = date.today()
    days = {"PF100": today + timedelta(days=90), "PF200": today + timedelta(days=20),
            "PF300": date(2020, 1, 1), "PF400": today + timedelta(days=3)}
    for pf, day in days.items():
        w.add(f"pblprfr/{pf}?", dated(pf, day))
    titles = {"PF100": "늦은 공연", "PF200": "가까운 공연", "PF300": "지난 공연", "PF400": "이번 주 공연"}
    llm = FakeLlm(concerts={pf: {**INDIE, "title": titles[pf], "startsAt": f"{days[pf].isoformat()}T19:00:00+09:00"} for pf in ids})

    first = LimitedTicketSearch(limit=1)
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio(), searcher=first)
    runner.run("lunch", sessions, p, [])
    assert first.asked == ["가까운 공연"]                   # 시작이 가까운 것부터, 지난 공연은 찾지 않음
    assert p.stats["concert_ticket_deferred"] == 2

    second = LimitedTicketSearch(limit=5)
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio(), searcher=second)
    runner.run("lunch", sessions, p, [])
    assert second.asked == ["늦은 공연", "이번 주 공연"]     # 7일 안 공연은 맨 뒤 , 찾아본 공연은 원문이 같으면 다시 찾지 않음

    third = LimitedTicketSearch(limit=5)
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio(), searcher=third)
    runner.run("lunch", sessions, p, [])
    assert third.asked == []


def test_ticket_search_uses_only_its_share_of_the_run_budget(sessions, monkeypatch):
    import crawler.pipeline as pl
    monkeypatch.setattr(pl, "TICKET_SEARCHES", 3)
    w = web()
    w.add("pblprfr/PF200?", KOPIS_DETAIL.replace("PF100", "PF200"))
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": {**INDIE, "title": "두 번째"}})
    search = LimitedTicketSearch(limit=99)
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio(), searcher=search)
    runner.run("lunch", sessions, p, [])
    assert len(search.asked) == 1 and p.stats["concert_ticket_deferred"] == 1    # 3크레딧을 쓴 뒤 멈춤 -> 나머지는 아티스트 조사 몫


# MusicBrainz 밖에서 찾기 (인디스트릿, 김밥레코즈, 웹 검색)

LABEL_URL = "https://label.example/power-andre-99"
STORE_URL = "https://store.example/silica-tee"


class FakeSearcher:
    def __init__(self, releases=None, goods=None, mentions=None, pages=None):
        self.r, self.g, self.m, self.calls = releases or {}, goods or {}, mentions or {}, []
        self.p = pages or {}                 # 검색어 -> 소개 글 출처 페이지 [{url, title, text, snippet}]

    def read_results(self, query):
        self.calls.append(("read_results", query))
        return self.p.get(query, [])

    def mentions(self, name):
        self.calls.append(("mentions", name))
        if self.m == "quota":
            raise SearchQuota("budget")
        return self.m.get(name, [])

    def releases(self, artist):
        self.calls.append(("releases", artist["name"]))
        return self.r.get(artist["name"], [])

    def goods(self, artist):
        self.calls.append(("goods", artist["name"]))
        return self.g.get(artist["name"], [])


def ev(url, title, quote_=None):
    return [{"url": url, "title": title, "quote": quote_}]


def searcher():
    return FakeSearcher(
        releases={"Silica Gel": [{
            "title": "POWER ANDRE 99", "type": "ALBUM", "released_at": "2026-11-04",
            "evidence": ev(LABEL_URL, "실리카겔 정규 2집 POWER ANDRE 99", "11월 4일 발매"),
            "formats": [
                {"format": "LP", "name": "2LP", "released_at": None, "pre_order": True, "vinyl_color": "BLACK",
                 "reference_price": 45000, "evidence": ev(LABEL_URL, "LP")},          # 김밥레코즈 LP 가 이김
                {"format": "CD", "name": "2CD", "released_at": "2026-11-04", "pre_order": False, "vinyl_color": "UNKNOWN",
                 "reference_price": None, "evidence": ev(LABEL_URL, "CD")}]}]},
        goods={"Silica Gel": [{"name": "로고 티셔츠 블랙", "category": "TSHIRT", "sizes": ["S", "M"], "reference_price": 35000,
                               "purchase_url": STORE_URL, "evidence": ev(STORE_URL, "실리카겔 로고 티셔츠")}]})


def found_web():
    from test_sources import ind_album, indistreet_page
    w = web()
    w.add("indistreet.com/albums", indistreet_page([
        ind_album("d1", "Ballad of You", artist=("실리카겔", "Silica Gel")),
        ind_album("d2", "흔한 클리셰처럼", artist=("리플리카", "RIPLEICA"))]))
    w.add(quote("리플리카"), {"artists": []})                                       # MusicBrainz 에 없는 팀
    w.add("product/search.html?keyword=" + quote("실리카겔"), """
<li id="anchorBoxId_77" class="xans-record-"><div class="name"><a href="/product/a/77/"><span>실리카겔 / Kyo181 (LP, Color Vinyl)</span></a></div></li>
<li id="anchorBoxId_25260" class="xans-record-"><div class="name"><a href="/product/b/25260/"><span>실리카겔 / 정규 2집 POWER ANDRE 99 (Vinyl, 2LP) *예약상품</span></a></div></li>
<li id="anchorBoxId_5" class="xans-record-"><div class="name"><a href="/product/c/5/"><span>실리카겔 / 어딘가의 앨범 (CD)</span></a></div></li>""")
    return w


def payloads(sessions, type_):
    return {json.loads(i.payload_json).get("title") or json.loads(i.payload_json)["name"]: json.loads(i.payload_json)
            for i in items(sessions, type_).values()}


def test_found_releases_physicals_and_goods_become_candidates_with_sources(sessions):
    w, studio, web_search = found_web(), FakeStudio(), searcher()
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), studio=studio, searcher=web_search,
                 llm=FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"Silica Gel": ["PSYCHEDELIC"]}))
    out = runner.run("dawn", sessions, p, ["검정치마"])
    assert out["status"] == "DONE"

    artists = payloads(sessions, "ARTIST")
    sg = artists["Silica Gel"]
    [tee] = sg["goods"]
    assert tee["name"] == "로고 티셔츠 블랙" and tee["category"] == "TSHIRT" and tee["purchase_url"] == STORE_URL
    assert [(o["size"], o["price"], o["qty"]) for o in tee["options"]] == [("S", None, None), ("M", None, None)]   # 판매가, 재고는 관리자가
    assert sg["evidence"] == [{"for": f"goods:{tee['external_id']}", "site": "websearch", "url": STORE_URL,
                               "title": "실리카겔 로고 티셔츠", "quote": None, "note": "참고 가격 35,000원 (출처 페이지 가격 · 판매가는 관리자가 정함)"}]
    assert artists["검정치마"]["goods"] is None and "evidence" not in artists["검정치마"]   # 찾은 것이 없으면 전과 같은 모양

    albums = payloads(sessions, "ALBUM")
    assert set(albums) == {"201", "Kyo181", "POWER ANDRE 99", "Ballad of You"}
    assert albums["201"]["physical_albums"] is None and "evidence" not in albums["201"]

    kyo = albums["Kyo181"]
    assert kyo["external_id"] == "b-rgB"
    assert [(x["external_id"], x["format"], x["options"][0]["vinyl_color"], x["options"][0]["price"]) for x in kyo["physical_albums"]] == [
        ("gimbab-77", "LP", "COLOR", None)]                     # 판매가는 관리자가 (null = Catalog 가 저장된 값 유지)

    pa = albums["POWER ANDRE 99"]
    assert pa["external_id"].startswith("w-") and pa["type"] == "ALBUM" and pa["released_at"] == "2026-11-04"
    assert pa["tracks"] is None and pa["artist_ref"]["external_id"] == sg["external_id"] and pa["genres"] == ["PSYCHEDELIC"]
    assert [(x["external_id"].split("-")[0], x["format"], x["name"]) for x in pa["physical_albums"]] == [
        ("gimbab", "LP", "LP · Vinyl, 2LP"), ("web", "CD", "2CD")]                # 웹 검색 LP 는 김밥레코즈 LP 와 겹쳐 뺌
    assert pa["evidence"][0] == {"for": "album", "site": "websearch", "url": LABEL_URL,
                                 "title": "실리카겔 정규 2집 POWER ANDRE 99", "quote": "11월 4일 발매"}
    assert any(e["for"] == "physical:gimbab-25260" and e["note"] == "예약 판매 · LP 색 모름 — 검정으로 둠" for e in pa["evidence"])

    ballad = albums["Ballad of You"]
    assert ballad["type"] == "SINGLE" and ballad["released_at"] == "2026-11-11" and ballad["external_id"].startswith("w-")
    assert ballad["evidence"][0]["site"] == "indistreet"

    assert out["stats"]["artist_not_found"] == 1                       # 인디스트릿의 리플리카 - MusicBrainz 에 없어 만들지 않음
    assert out["stats"]["physical_no_album"] == 1                      # '어딘가의 앨범' CD - 어느 앨범인지 몰라 붙이지 않음
    assert ("releases", "Silica Gel") in web_search.calls and ("goods", "Silica Gel") in web_search.calls
    sent = {path: [i["external_id"] for i in body["items"]] for path, body in studio.posts}
    assert len(sent[ALBUMS]) == 4


class FakePages:
    """webtools.PageReader 대신 - 주소 -> 마크다운, 안쪽 주소는 Blocked"""
    def __init__(self, pages):
        self.pages, self.read_urls = pages, []

    def read(self, url):
        self.read_urls.append(url)
        return url, self.pages[url]

    def check_public(self, url):
        from crawler.http import Blocked
        if "internal" in url:
            raise Blocked(url)


def test_goods_detail_from_the_official_store_page_goes_with_the_artist(sessions):
    """공식 판매 페이지의 글, 큰 이미지 -> Gemini 정리 -> 굿즈 description, detail_image_urls + 출처 (작은 이미지, 안쪽 주소는 빼고)"""
    w, studio, web_search = found_web(), FakeStudio(), searcher()
    w.add_bytes("cdn.example/detail-1.jpg", png(860, 2400))
    w.add_bytes("cdn.example/icon.png", png(40, 40))
    w.add_bytes("cdn.example/detail-2.jpg", png(860, 900))
    web_search.pages = FakePages({LABEL_URL: "실리카겔 정규 2집 안내", STORE_URL: "\n".join([
        "# 로고 티셔츠 블랙", "![](https://cdn.example/icon.png)", "면 100% · 실크 스크린 인쇄",
        "![상세](https://cdn.example/detail-1.jpg)", "![](http://internal.example/x.jpg)", "![](https://cdn.example/detail-2.jpg)"])})
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"Silica Gel": ["PSYCHEDELIC"]},
                  products={"로고 티셔츠 블랙": {"isProductPage": True, "summary": "실리카겔 로고를 앞면에 인쇄한 반팔 티셔츠.",
                                               "facts": ["소재: 면 100%", "인쇄: 실크 스크린"], "care": [], "detailImages": [1, 9]}})
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), studio=studio, searcher=web_search, llm=llm)
    out = runner.run("dawn", sessions, p, ["검정치마"])
    assert out["status"] == "DONE" and out["stats"]["product_detail_written"] == 1
    assert web_search.pages.read_urls == [LABEL_URL, STORE_URL]           # 웹 검색 CD 도 읽음 (김밥레코즈 LP 에 밀린 웹 LP 는 후보에 없어 안 읽음)
    assert out["stats"]["product_detail_not_product_page"] == 1
    assert llm.product_images["로고 티셔츠 블랙"] == 2                      # 아이콘, 안쪽 주소는 빼고 큰 이미지 2개
    [tee] = payloads(sessions, "ARTIST")["Silica Gel"]["goods"]
    assert tee["description"] == "\n".join(["실리카겔 로고를 앞면에 인쇄한 반팔 티셔츠.", "", "## 상품 정보", "- 소재: 면 100%",
                                            "- 인쇄: 실크 스크린", "", "정보 출처: store.example"])
    assert tee["detail_image_urls"] == ["https://cdn.example/detail-1.jpg"]   # 없는 번호(9)는 버림
    ev = payloads(sessions, "ARTIST")["Silica Gel"]["evidence"]
    assert ev[-1] == {"for": f"goods:{tee['external_id']}", "site": "detail", "url": STORE_URL,
                      "title": "상세 설명 · 상세 이미지를 읽은 페이지", "quote": None}
    sent = [i for path, body in studio.posts if path.endswith("/artists") for i in body["items"] if i["name"] == "Silica Gel"]
    assert sent[-1]["goods"][0]["description"] == tee["description"]      # 같은 실행에 보냄


def test_product_details_are_read_once_and_never_from_gimbab_or_shops(sessions):
    """한 번 읽은 상품은 다시 읽지 않음, 김밥레코즈 판본, 판매처, SNS 출처뿐인 상품은 열지 않음, 다시 만든 후보에도 그대로"""
    w, web_search = found_web(), searcher()
    web_search.g["Silica Gel"][0]["purchase_url"] = None
    web_search.g["Silica Gel"][0]["evidence"] = ev("https://www.instagram.com/p/abc", "인스타 글")
    web_search.pages = FakePages({LABEL_URL: "실리카겔 정규 2집 2CD · 디지팩"})
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"Silica Gel": ["PSYCHEDELIC"]},
                  products={"2CD": {"isProductPage": True, "summary": "", "facts": ["구성: CD 2장 · 디지팩"], "care": [], "detailImages": []}})
    run = lambda: runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"),
                                                        studio=FakeStudio(), searcher=web_search, llm=llm), ["검정치마"])
    out = run()
    assert web_search.pages.read_urls == [LABEL_URL] and out["stats"]["product_detail_no_page"] == 1   # 인스타 출처뿐인 굿즈
    pa = payloads(sessions, "ALBUM")["POWER ANDRE 99"]
    cd = next(x for x in pa["physical_albums"] if x["format"] == "CD")
    assert cd["description"].startswith("## 상품 정보\n- 구성: CD 2장 · 디지팩") and "detail_image_urls" not in cd
    lp = next(x for x in pa["physical_albums"] if x["format"] == "LP")
    assert lp["external_id"].startswith("gimbab-") and "description" not in lp                       # 김밥레코즈는 판 사실만
    with sessions() as s:
        s.execute(update(dbm.RawPage).where(dbm.RawPage.source == "musicbrainz").values(fetched_at=dbm.now() - timedelta(days=8)))
        s.commit()
    run()
    assert web_search.pages.read_urls == [LABEL_URL]                                                # 다시 읽지 않음
    cd2 = next(x for x in payloads(sessions, "ALBUM")["POWER ANDRE 99"]["physical_albums"] if x["format"] == "CD")
    assert cd2["description"] == cd["description"]                                                  # 다시 만든 후보에도 그대로


def test_discogs_edition_description_comes_from_discogs_release_data(sessions):
    """Discogs 판본 - 판 정보, 수록곡, 출처 (Gemini 안 씀, 모르는 일 00 은 뺌), 쓸 것이 없으면 칸이 없음"""
    w = web()
    w.add("artist/bs?", {"id": "bs", "name": "검정치마", "aliases": [],
                         "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/9"}}]})
    w.add("api.discogs.com/artists/9", {"id": 9, "name": "The Black Skirts"})
    w.add("database/search?artist=The%20Black%20Skirts", {"results": [{"id": 5, "format": ["Vinyl"]}, {"id": 6, "format": ["CD"]}]})
    w.add("api.discogs.com/releases/5", {"id": 5, "title": "201", "artists": [{"id": 9}], "released": "2021-06-00", "country": "South Korea",
                                         "uri": "https://www.discogs.com/release/5-201",
                                         "formats": [{"name": "Vinyl", "qty": "2", "descriptions": ["LP", "Album", "Reissue"], "text": "Clear"}],
                                         "labels": [{"name": "Damn Rascals", "catno": "DR-001"}],
                                         "tracklist": [{"position": "A1", "type_": "track", "title": "Antifreeze"},
                                                       {"position": "", "type_": "heading", "title": "Side B"},
                                                       {"position": "B1", "type_": "track", "title": "Dientes"}]})
    w.add("api.discogs.com/releases/6", {"id": 6, "title": "201", "artists": [{"id": 9}], "formats": [{"name": "CD"}]})
    p = Pipeline(sessions, make_http(sessions, w), settings(discogs_token="T"), llm=None, studio=FakeStudio())
    p.artists(["검정치마"])
    phys = {x["format"]: x for x in payloads(sessions, "ALBUM")["201"]["physical_albums"]}
    assert phys["LP"]["description"] == "\n".join([
        "## 판 정보", "- 형식: LP 2장 · 정규 · 재발매", "- 색: Clear", "- 레이블: Damn Rascals (DR-001)", "- 발매: 2021-06 · 한국", "",
        "## 수록곡", "- A1 Antifreeze", "- B1 Dientes", "", "정보 출처: Discogs (https://www.discogs.com/release/5-201)"])
    assert "description" not in phys["CD"]                                    # 형식뿐 - 판 이름에 이미 있음


def test_musicbrainz_later_adds_a_found_album_under_the_same_key(sessions):
    """웹 검색으로 먼저 보낸 앨범이 나중에 MusicBrainz 에 생기면 같은 후보(같은 external_id)를 MusicBrainz 값으로 고침 - 둘이 되지 않음"""
    w, web_search = found_web(), searcher()
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}, genres={"Silica Gel": ["PSYCHEDELIC"]})
    run = lambda studio: runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"),
                                                              llm=llm, studio=studio, searcher=web_search), ["검정치마"])
    run(FakeStudio())
    first_key = payloads(sessions, "ALBUM")["POWER ANDRE 99"]["external_id"]

    w.add("release-group?artist=sg", {"release-groups": [
        {"id": "rgB", "title": "Kyo181", "primary-type": "Album", "first-release-date": "2011-05-10"},
        {"id": "rgP", "title": "POWER ANDRE 99", "primary-type": "Album", "first-release-date": "2026-11-04"}]})
    w.add("release?release-group=rgP", {"releases": [{"date": "2026-11-04", "media": [{"tracks": [{"title": "Power"}]}],
                                                      "label-info": [{"label": {"name": "Magic Strawberry Sound"}}]}]})
    with sessions() as s:                                              # MusicBrainz 캐시가 지남 (웹 검색은 아직 7일 안)
        s.execute(update(dbm.RawPage).where(dbm.RawPage.source == "musicbrainz").values(fetched_at=dbm.now() - timedelta(days=8)))
        s.commit()
    calls = len(web_search.calls)
    studio = FakeStudio()
    run(studio)

    albums = payloads(sessions, "ALBUM")
    assert albums["POWER ANDRE 99"]["external_id"] == first_key and "b-rgP" not in items(sessions, "ALBUM")
    assert [t["title"] for t in albums["POWER ANDRE 99"]["tracks"]] == ["Power"]
    assert [x["format"] for x in albums["POWER ANDRE 99"]["physical_albums"]] == ["LP", "CD"]   # 찾아 둔 판본은 그대로 붙음
    assert first_key in [i["external_id"] for path, body in studio.posts if path == ALBUMS for i in body["items"]]
    assert len(web_search.calls) == calls                              # 웹 검색은 7일에 한 번


def test_web_search_once_a_week_with_a_per_run_limit(sessions, monkeypatch):
    import crawler.pipeline as pl
    monkeypatch.setattr(pl, "WEB_ARTISTS_PER_RUN", 1)
    w, web_search = web(), FakeSearcher()
    run = lambda: runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(), studio=None,
                                                       searcher=web_search), ["검정치마", "실리카겔"])
    out = run()
    assert [c[1] for c in web_search.calls] == ["검정치마", "검정치마"] and out["stats"]["web_search_deferred"] == 1
    run()
    assert [c[1] for c in web_search.calls][2:] == ["Silica Gel", "Silica Gel"]   # 다음 실행에 남은 팀
    run()
    assert len(web_search.calls) == 4                                  # 둘 다 7일 안
    with sessions() as s:
        s.execute(update(dbm.RawPage).where(dbm.RawPage.source == "websearch").values(fetched_at=dbm.now() - timedelta(days=8)))
        s.commit()
    run()
    assert len(web_search.calls) == 6


def test_found_values_stay_put_and_only_blanks_fill(sessions):
    with sessions() as s:
        assert dbm.remember(s, "a-1", "RELEASE", "x", "indistreet", {"title": "X", "type": None, "released_at": "2026-01-01"},
                            [{"site": "indistreet", "url": "https://i/x", "title": "X", "quote": None}])
        assert not dbm.remember(s, "a-1", "RELEASE", "x", "websearch", {"title": "X!", "type": None, "released_at": "2026-02-02"},
                                [{"site": "websearch", "url": "https://w/x", "title": "X", "quote": None}])
        assert dbm.remember(s, "a-1", "RELEASE", "x", "websearch", {"title": "X", "type": "EP", "released_at": None},
                            [{"site": "websearch", "url": "https://w/x", "title": "X", "quote": None}])
        row = s.scalar(select(dbm.Found))
        assert json.loads(row.data_json) == {"title": "X", "type": "EP", "released_at": "2026-01-01"} and row.site == "indistreet"
        assert [e["url"] for e in json.loads(row.evidence_json)] == ["https://i/x", "https://w/x"]


def test_album_key_for_keeps_same_title_single_and_album_apart(sessions):
    with sessions() as s:
        web_key = dbm.album_key_for(s, "a-1", "ALBUM", "Same")
        assert dbm.album_key_for(s, "a-1", "ALBUM", "Same", rgid="rg1") == web_key       # 웹 앨범을 MusicBrainz 가 이어받음
        assert dbm.album_key_for(s, "a-1", "ALBUM", "Same", rgid="rg2") == "b-rg2"       # 같은 제목 두 번째는 따로
        assert dbm.album_key_for(s, "a-1", "SINGLE", "Same", rgid="rg3") == "b-rg3"      # 종류가 다르면 다른 앨범
        assert dbm.album_key_for(s, "a-1", "ALBUM", "Same", rgid="rg1") == web_key       # 이미 이은 것은 그대로
        assert dbm.album_key_for(s, "a-2", "ALBUM", "Same") != web_key                  # 다른 아티스트


def test_failed_stage_does_not_stop_the_rest_of_the_run(sessions):
    """KOPIS 목록 400 하나로 새벽 실행 전체가 멈췄음 -> 공연만 실패로 남고 아티스트, 앨범은 그대로 보냄"""
    w, studio = web(), FakeStudio()
    w.add("pblprfr?service=KEY", "<html><body>Bad Request</body></html>", status=400)
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    assert out["status"] == "FAILED"
    assert out["error"].startswith("공연 [HTTP 400]: RuntimeError: GET ") and "service=***" in out["error"]
    assert out["error"].endswith("→ 400 <html><body>Bad Request</body></html>")       # 응답 본문도 남김
    assert [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]                   # 공연 말고는 보냄
    with sessions() as s:
        assert s.get(dbm.CrawlRun, out["run"]).status == "FAILED"


def test_dawn_retries_the_failed_concert_stage_at_the_end(sessions):
    """KOPIS 차단(Request Blocked)이 몇 분 이어져 공연 단계만 실패 -> 새벽 실행은 아티스트 단계 뒤 한 번 더"""
    w, studio = web(), FakeStudio()
    p = pipeline(sessions, w, studio)
    real, calls = p.concerts, []

    def blocked_then_ok():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("GET ... → 400 Request Blocked")
        return real()
    p.concerts = blocked_then_ok
    out = runner.run("dawn", sessions, p, ["검정치마"])
    assert len(calls) == 2 and out["status"] == "DONE" and out["error"] is None
    assert out["stats"]["concert_stage_retry"] == 1 and out["stats"]["send_concert_created"] == 1

    p2 = pipeline(sessions, web(), FakeStudio())
    p2.concerts = lambda: (_ for _ in ()).throw(RuntimeError("still blocked"))
    out2 = runner.run("dawn", sessions, p2, [])
    assert out2["status"] == "FAILED" and out2["error"].startswith("공연: RuntimeError: still blocked")
    assert "공연 다시" in out2["error"]                                  # 두 번 다 실패하면 둘 다 남김

    p3, lunch_calls = pipeline(sessions, web(), FakeStudio()), []
    p3.concerts = lambda: lunch_calls.append(1) or (_ for _ in ()).throw(RuntimeError("blocked"))
    assert runner.run("lunch", sessions, p3, [])["status"] == "FAILED" and len(lunch_calls) == 1   # 점심은 다시 안 함


def test_gemini_unavailable_defers_the_remaining_concerts_and_the_run_goes_on(sessions):
    """Gemini 가 계속 503 -> 남은 공연은 다음 실행에, 아티스트, 앨범은 그대로 보내고 실행은 DONE"""
    from crawler.llm import LlmSkip

    class BusyLlm(FakeLlm):
        exhausted, stop_reason = False, "quota"

        def concert(self, facts, images=None):
            self.exhausted, self.stop_reason = True, "unavailable"
            raise LlmSkip("unavailable")

    w, studio = web(), FakeStudio()
    llm = BusyLlm(genres={"검정치마": ["INDIE_ROCK"]})
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert out["status"] == "DONE" and out["error"] is None
    assert out["stats"]["concert_llm_unavailable"] == 2                          # 첫 공연은 503, 둘째는 부르지도 않음
    assert "concert_llm_quota" not in out["stats"]                               # 한도로 잘못 적지 않음
    assert items(sessions, "CONCERT") == {}                                      # 저장하지 않음 -> 다음 실행에 다시
    assert [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]


def test_kopis_down_stops_the_concert_loop_after_five_misses_in_a_row(sessions):
    """다시 불러도 상세가 5번 연달아 실패 -> 남은 공연은 하나씩 기다리지 않고 다음 실행에 (실행이 다음 일정까지 늘어지지 않게)"""
    w, studio = web(), FakeStudio()
    ids = [f"PF{n}00" for n in range(1, 8)]
    w.add("pblprfr?service=KEY", "<dbs>" + "".join(f"<db><mt20id>{i}</mt20id></db>" for i in ids) + "</dbs>")
    for i in ids:
        w.add(f"pblprfr/{i}?", "", status=500)
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    # 실행 끝 다시 하기()에서 못 받은 5건부터 한 번 더 -> 또 5번 실패 -> 멈춤, 5건은 다음 실행 맨 앞에
    assert out["stats"]["concert_fetch_error"] == 10 and out["stats"]["concert_kopis_down"] == 2
    assert out["stats"]["retry_left"] == 5
    assert not any("pblprfr/PF600" in u or "pblprfr/PF700" in u for u in w.calls)
    assert out["status"] == "DONE" and [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]


def test_discogs_error_skips_only_the_physical_not_the_artist(sessions):
    """안치환: MusicBrainz 가 가리키는 Discogs 아티스트가 지워져 404 -> 아티스트 전체가 빠졌음"""
    w = web()
    w.add("artist/bs?", {"id": "bs", "name": "검정치마", "aliases": [],
                         "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/9"}}]})
    w.add("api.discogs.com/artists/9", {"message": "Artist not found."}, status=404)
    llm = FakeLlm(genres={"검정치마": ["INDIE_ROCK"]})
    p = Pipeline(sessions, make_http(sessions, w), settings(discogs_token="T"), llm=llm, studio=FakeStudio())
    p.artists(["검정치마"])
    # 지워진 Discogs 아티스트(404)는 연결이 없는 것과 같음 - 오류로 세지 않음 (실행마다 Slack 에 뜨던 것)
    assert p.stats["discogs_gone"] == 1 and "discogs_error" not in p.stats and "discogs" not in p.failures
    assert "artist_error" not in p.stats and p.stats["artist_ok"] == 1
    albums = {json.loads(i.payload_json)["title"]: json.loads(i.payload_json) for i in items(sessions, "ALBUM").values()}
    assert albums["201"]["physical_albums"] is None                   # 피지컬만 건드리지 않음

    w.add("api.discogs.com/artists/9", {"message": "busy"}, status=500)   # 그 밖의 오류는 그대로 오류
    p = Pipeline(sessions, make_http(sessions, w), settings(discogs_token="T"), llm=llm, studio=FakeStudio())
    p.artists(["검정치마"])
    assert p.stats["discogs_error"] == 1 and "discogs_gone" not in p.stats


def test_artist_without_genres_waits_until_genres_are_judged(sessions):
    """Gemini 한도로 장르를 못 정한 새 아티스트는 HOLD - 아티스트, 앨범 모두 보내지 않고, 장르를 정한 실행에 함께 감"""
    from crawler.llm import LlmSkip

    class NoQuota(FakeLlm):
        def artist_info(self, name, aliases, albums, label, legal_names=(), web=()):
            raise LlmSkip("quota")

    w, studio = web(), FakeStudio()
    runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(), llm=NoQuota(), studio=studio), ["검정치마"])
    assert items(sessions, "ARTIST")[next(iter(items(sessions, "ARTIST")))].status == "HOLD"
    assert studio.posts == []                                         # 장르 없는 아티스트도, 그 앨범도 안 감

    studio2 = FakeStudio()
    llm = FakeLlm(genres={"검정치마": ["INDIE_ROCK"]})
    runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(), llm=llm, studio=studio2), ["검정치마"])
    assert [path for path, _ in studio2.posts] == [ARTISTS, ALBUMS]
    assert studio2.posts[0][1]["items"][0]["genres"] == ["INDIE_ROCK"]
    assert all(a["genres"] == ["INDIE_ROCK"] for a in studio2.posts[1][1]["items"])


def test_artist_not_judged_because_gemini_was_busy_is_asked_again_at_the_end_of_the_run(sessions):
    """Gemini 503 으로 못 물은 아티스트 - 실행 끝 다시 하기에서 한 번 더 (그 사이 Gemini 가 돌아왔으면 같은 실행에 보냄)"""
    from crawler.llm import LlmSkip

    class BusyOnce(FakeLlm):
        def artist_info(self, name, *args, **kwargs):
            if not any(c.startswith("artist:") for c in self.calls):
                self.calls.append(f"artist:{name}")
                raise LlmSkip("unavailable")
            return super().artist_info(name, *args, **kwargs)

    w, studio = web(), FakeStudio()
    llm = BusyOnce(concerts={"PF100": INDIE, "PF200": MAINSTREAM},
                   genres={"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC"]})
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["검정치마"])
    assert out["stats"]["artist_llm_unavailable"] == 1 and out["stats"]["artist_reask"] == 1
    assert "artist_not_judged_left" not in out["stats"]
    assert llm.calls.count("artist:검정치마") == 2                     # 처음 503, 실행 끝에 다시
    sent = {a["name"]: a["genres"] for path, body in studio.posts if path == ARTISTS for a in body["items"]}
    assert sent == {"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC"]}


def test_artist_not_judged_at_night_is_asked_again_by_the_lunch_run(sessions):
    """, 새벽 503 이 실행 내내 이어져 168팀을 못 물었는데, 점심은 공연만 해서 다음 새벽까지 그대로였음 -> 점심에도 다시 물음"""
    from crawler.llm import LlmSkip

    class Busy(FakeLlm):
        exhausted, stop_reason = False, "quota"

        def artist_info(self, name, *args, **kwargs):
            self.exhausted, self.stop_reason = True, "unavailable"
            raise LlmSkip("unavailable")

    w = web()
    out = runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio(), Busy(concerts={"PF100": INDIE, "PF200": MAINSTREAM})),
                     ["검정치마"])
    assert out["stats"]["artist_reask_deferred"] == 2 and out["stats"]["artist_not_judged_left"] == 2   # 쉬는 중이면 묻지 않음
    assert {i.status for i in items(sessions, "ARTIST").values()} == {"HOLD"}

    studio = FakeStudio()
    out = runner.run("lunch", sessions, pipeline(sessions, w, studio), [])
    assert out["stats"]["artist_reask"] == 2 and "artist_not_judged_left" not in out["stats"]
    assert [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]
    assert {a["name"]: a["genres"] for a in studio.posts[0][1]["items"]} == {
        "검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC", "INDIE_ROCK"]}


def test_store_titles_match_albums_without_edition_words():
    """김밥레코즈 판본 214개 중 69개가 앨범에 못 붙고 버려졌음 - '정규 3집', 'EP' 같은 앞말, 판 이름, 괄호 뒤를 떼고 봄"""
    from crawler.pipeline import _same_album
    kim = ["7102", "Romance", "헤븐 (Heaven)", "디폴트 (DEFAULT)", "수잔"]
    assert _same_album("3집 헤븐", kim) == "헤븐 (Heaven)"
    assert _same_album("1집 수잔", kim) == "수잔"
    assert _same_album("2집 로맨스", kim) is None                              # 한글, 영어 제목 차이는 관리자가
    assert _same_album("EP 2집 집(HOME)", ["집", "입춘"]) == "집"
    assert _same_album("정규 4집 : 0집", ["0집", "역성"]) == "0집"
    assert _same_album("사랑으로 (정규앨범)", ["사랑으로 (through love)", "20"]) == "사랑으로 (through love)"
    assert _same_album("서울병 10th Anniversary Edition", ["서울병", "계몽"]) == "서울병"
    assert _same_album("정규 5집 5.0", ["5.0", "5.0 (Acoustic)"]) == "5.0"   # 다른 판(Acoustic)은 같은 앨범으로 보지 않음
    assert _same_album("5.0", ["5.0 (Acoustic)"]) is None
    assert _same_album("동물", ["동물", "동물 (Remastered)"]) == "동물"
    assert _same_album("온(百)", ["ON(百)"]) is None                          # 괄호 안만 같은 것은 붙이지 않음
    assert _same_album("EPIK", ["IK"]) is None                                # 'EP' 로 시작하는 낱말은 그대로
    assert _same_album("정규 2집 POWER ANDRE 99", ["POWER ANDRE 99", "Kyo181"]) == "POWER ANDRE 99"   # 전과 같음


def test_concert_dates_outside_the_kopis_period_are_fixed_and_flagged_for_review():
    """사뮈 투어: 포스터에 도시별 일정이 함께 있어 KOPIS(12/19 17:00) 대신 다른 회차(12/11 20:30)를 고름 -> KOPIS 에 맞추고 '검수 필요'"""
    detail = {"prfpdfrom": "2026.12.19", "prfpdto": "2026.12.19", "dtguidance": "토요일(17:00)"}
    c = {"startsAt": "2026-12-11T20:30:00+09:00", "endsAt": "2026-12-11T22:30:00+09:00"}
    notes = fit_kopis_dates(detail, c)
    assert c == {"startsAt": "2026-12-19T17:00:00+09:00", "endsAt": "2026-12-19T19:00:00+09:00"}   # 길이 2시간은 그대로
    assert notes[0].startswith("검수 필요 — 시작 2026-12-11 20:30 → 2026-12-19 17:00 · 끝 2026-12-11 22:30 → 2026-12-19 19:00")
    assert fit_kopis_dates(detail, c) == []                                       # 고친 값은 다시 걸리지 않음

    time_only = {"startsAt": "2026-11-27T20:00:00+09:00", "endsAt": "2026-11-27T22:00:00+09:00"}   # RAMPAGE FEST 전야제
    assert fit_kopis_dates({"prfpdfrom": "2026.11.27", "prfpdto": "2026.11.27", "dtguidance": "금요일(20:30)"}, time_only)
    assert time_only["startsAt"] == "2026-11-27T20:30:00+09:00"

    run = {"startsAt": "2026-10-30T19:30:00+09:00", "endsAt": "2026-10-31T18:00:00+09:00"}   # 여러 날, 시각 여럿 -> 날짜만
    assert fit_kopis_dates({"prfpdfrom": "2026.11.20", "prfpdto": "2026.11.21", "dtguidance": "금요일(19:30), 토요일(16:00)"}, run)
    assert run == {"startsAt": "2026-11-20T19:30:00+09:00", "endsAt": "2026-11-21T18:00:00+09:00"}

    odd = {"startsAt": "2026-11-28T14:00:00+09:00", "endsAt": "2026-11-29T16:00:00+09:00"}   # 신촌블루스 - 끝도 하루 밀려 있었음
    assert fit_kopis_dates({"prfpdfrom": "2026.11.29", "prfpdto": "2026.11.29", "dtguidance": "일요일(14:00)"}, odd)
    assert odd == {"startsAt": "2026-11-29T14:00:00+09:00", "endsAt": "2026-11-29T16:00:00+09:00"}

    tour = {"startsAt": "2026-12-19T18:30:00+09:00", "endsAt": "2026-12-26T18:30:00+09:00"}   # 론 브랜튼 - 맞추면 끝 = 시작
    notes = fit_kopis_dates({"prfpdfrom": "2026.12.25", "prfpdto": "2026.12.25", "dtguidance": "금요일(16:00,19:30)"}, tour)
    assert tour == {"startsAt": "2026-12-25T18:30:00+09:00", "endsAt": None} and "끝 2026-12-26 18:30 → 비움" in notes[0]

    fine = {"startsAt": "2026-12-12T18:00:00+09:00", "endsAt": None}
    assert fit_kopis_dates({"prfpdfrom": "2026.12.12", "prfpdto": "2026.12.13", "dtguidance": "토요일(18:00), 일요일(17:00)"}, fine) == []
    assert fit_kopis_dates({}, {"startsAt": None}) == []                           # 기준이 없으면 그대로


def test_kopis_schedule_becomes_sessions():
    """KOPIS 기간과 시간 안내로 회차를 만들고 공휴일은 HOL 시각을 씀"""
    def starts(detail):
        found = kopis_sessions(detail)
        return None if found is None else ([s.isoformat() for s in found[0]], found[1])

    weekend = {"prfpdfrom": "2026.12.12", "prfpdto": "2026.12.13", "dtguidance": "토요일(18:00), 일요일(17:00)"}
    assert starts(weekend) == (["2026-12-12T18:00:00+09:00", "2026-12-13T17:00:00+09:00"], False)
    two_shows = {"prfpdfrom": "2026.12.25", "prfpdto": "2026.12.25", "dtguidance": "금요일(16:00,19:30)"}
    assert starts(two_shows) == (["2026-12-25T16:00:00+09:00", "2026-12-25T19:30:00+09:00"], False)
    overnight = {"prfpdfrom": "2026.12.17", "prfpdto": "2026.12.18", "dtguidance": "목요일(22:00)"}
    assert starts(overnight) == (["2026-12-17T22:00:00+09:00"], False)

    run = {"prfpdfrom": "2026.12.11", "prfpdto": "2026.12.27", "dtguidance": "목요일 ~ 금요일(20:00), 토요일 ~ 일요일(17:00), HOL(17:00)"}
    got, used_hol = starts(run)
    assert used_hol and len(got) == 11
    assert "2026-12-25T17:00:00+09:00" in got and "2026-12-25T20:00:00+09:00" not in got
    assert not any(d.startswith(("2026-12-14", "2026-12-15", "2026-12-16")) for d in got)

    assert starts({**weekend, "dtguidance": "주말 공연"}) is None
    assert starts({**weekend, "dtguidance": "토요일(25:00)"}) is None
    assert starts({"dtguidance": "토요일(18:00)"}) is None


def test_two_or_more_sessions_go_to_the_candidate():
    """회차가 둘 이상이면 sessions 를 넣고 시작은 첫 회차, 하나면 sessions 없이 startsAt 하나"""
    weekend = {"prfpdfrom": "2026.12.12", "prfpdto": "2026.12.13", "dtguidance": "토요일(18:00), 일요일(17:00)"}
    c = {"startsAt": "2026-12-12T18:00:00+09:00", "endsAt": "2026-12-13T19:00:00+09:00"}
    assert fit_sessions(weekend, c) == []
    assert c["sessions"] == [{"startsAt": "2026-12-12T18:00:00+09:00"}, {"startsAt": "2026-12-13T17:00:00+09:00"}]

    late = {"startsAt": "2026-12-13T17:00:00+09:00", "endsAt": "2026-12-12T20:00:00+09:00"}
    notes = fit_sessions(weekend, late)
    assert late["startsAt"] == "2026-12-12T18:00:00+09:00" and late["endsAt"] is None
    assert len(notes) == 2 and all(n.startswith("검수 필요 - 회차") for n in notes)

    single = {"startsAt": "2026-12-19T17:00:00+09:00", "sessions": [{"startsAt": "x"}]}
    assert fit_sessions({"prfpdfrom": "2026.12.19", "prfpdto": "2026.12.19", "dtguidance": "토요일(17:00)"}, single) == []
    assert "sessions" not in single

    daily = {"prfpdfrom": "2027.01.01", "prfpdto": "2027.03.31", "dtguidance": "월요일 ~ 일요일(20:00)"}
    many = {"startsAt": "2027-01-01T20:00:00+09:00"}
    assert "50개를 넘어" in fit_sessions(daily, many)[0] and "sessions" not in many

    hol = {"prfpdfrom": "2026.12.24", "prfpdto": "2026.12.25", "dtguidance": "목요일(20:00), HOL(17:00)"}
    c = {"startsAt": "2026-12-24T20:00:00+09:00"}
    assert "HOL" in fit_sessions(hol, c)[0] and c["sessions"][1] == {"startsAt": "2026-12-25T17:00:00+09:00"}


def test_sessions_are_added_to_candidates_already_sent_without_asking_gemini(sessions):
    """원문이 그대로인 공연도 다음 실행에서 회차를 넣어 다시 보냄. 날짜를 고친 글은 남김"""
    w = web()
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}),
                 studio=FakeStudio())
    p.concerts()
    assert len(json.loads(items(sessions, "CONCERT")["PF100"].payload_json)["concert"]["sessions"]) == 2
    with sessions() as s:
        item = s.scalar(select(dbm.Item).where(dbm.Item.key == "PF100"))
        payload = json.loads(item.payload_json)
        payload["concert"].pop("sessions")
        payload["concert"]["reviewNotes"] = ["검수 필요 - 전에 고친 날짜"]
        item.payload_json, item.status, item.source_hash = json.dumps(payload, ensure_ascii=False), "SENT", "old"
        s.commit()

    llm = FakeLlm(concerts={})
    p2 = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio())
    p2.concerts()
    c = json.loads(items(sessions, "CONCERT")["PF100"].payload_json)["concert"]
    assert llm.calls == [] and len(c["sessions"]) == 2 and c["reviewNotes"] == ["검수 필요 - 전에 고친 날짜"]
    assert items(sessions, "CONCERT")["PF100"].status == "READY"


def test_concerts_sent_before_the_date_check_are_fixed_without_asking_gemini_again(sessions):
    """이미 정리해 보낸 공연(원문 그대로)도 다음 실행에서 날짜를 고쳐 다시 보냄 - Gemini 는 다시 부르지 않음"""
    w = web()
    wrong = {**INDIE, "startsAt": "2026-12-05T18:00:00+09:00", "endsAt": "2026-12-05T20:00:00+09:00"}
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=FakeLlm(concerts={"PF100": wrong, "PF200": MAINSTREAM}),
                 studio=FakeStudio())
    p.concerts()
    with sessions() as s:                                                         # 검사 전 코드가 보낸 상태로 되돌림
        item = s.scalar(select(dbm.Item).where(dbm.Item.key == "PF100"))
        payload = json.loads(item.payload_json)
        payload["concert"].update(startsAt=wrong["startsAt"], endsAt=wrong["endsAt"], reviewNotes=[])
        item.payload_json, item.status, item.source_hash = json.dumps(payload, ensure_ascii=False), "SENT", "old"
        s.commit()

    llm = FakeLlm(concerts={})                                                  # 부르면 KeyError
    p2 = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio())
    p2.concerts()
    assert p2.stats["concert_same_input"] == 2 and p2.stats["concert_dates_fixed"] == 1 and llm.calls == []
    c = json.loads(items(sessions, "CONCERT")["PF100"].payload_json)["concert"]
    assert c["startsAt"] == "2026-12-12T18:00:00+09:00" and c["reviewNotes"][0].startswith("검수 필요")
    assert items(sessions, "CONCERT")["PF100"].status == "READY"                   # 다시 보냄


def test_rejected_concerts_and_artists_are_not_searched_again(sessions):
    """관리자가 거절한 공연, 아티스트는 다시 찾지 않음 (KOPIS 상세, Gemini, MusicBrainz), 관리자가 처리한 공연은 예매 오픈도 찾지 않음.
    Studio 에서 목록을 못 받으면 전처럼 전부"""
    w = web()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), ["검정치마"])
    keys = {json.loads(i.payload_json)["name"]: k for k, i in items(sessions, "ARTIST").items()}

    w.calls.clear()
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM},
                  genres={"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC", "INDIE_ROCK"]})
    studio = FakeStudio(reviewed={"concerts": [{"source": "kopis", "external_id": "PF100", "status": "REJECTED"}],
                                  "artists": [{"source": "crawler", "external_id": keys["검정치마"], "status": "REJECTED"}],
                                  "albums": []})
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=studio)
    out = runner.run("dawn", sessions, p, ["검정치마"])
    st = out["stats"]
    assert out["status"] == "DONE" and studio.gets == ["/internal/crawl/reviewed"]
    assert not any("prfplc/FC100" in u for u in w.calls)                        # 거절한 공연은 상세, 공연장을 보지 않음
    assert (st["concert_rejected_skip"], st["artist_rejected_skip"], st["reviewed_rejected"]) == (1, 1, 2)
    assert st["artist_refreshed"] == 1                                           # 다시 보기는 거절 안 한 실리카겔만

    down = FakeStudio(reviewed="down")
    p = pipeline(sessions, w, down)
    w.calls.clear()
    out = runner.run("lunch", sessions, p, [])
    assert out["status"] == "DONE" and out["stats"]["reviewed_fetch_error"] == 1
    assert out["failures"]["reviewed_fetch"] == {"count": 1, "codes": {"ConnectionError": 1}, "samples": [("studio", "studio down")]}
    assert out["failures"]["concert_facility"]["codes"] == {"HTTP 404": 1}            # 알림에 보일 코드
    assert any("prfplc/FC100" in u for u in w.calls)                             # 못 받으면 전처럼


def test_concerts_handled_by_admins_skip_the_ticket_open_search(sessions):
    w = web()
    search = FakeTicketSearch(None)
    studio = FakeStudio(reviewed={"concerts": [{"source": "kopis", "external_id": "PF100", "status": "APPROVED"}],
                                  "artists": [], "albums": []})
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM}),
                 studio=studio, searcher=search)
    runner.run("lunch", sessions, p, [])
    assert search.asked == []                          # 등록한 공연 - 예매 오픈은 관리자가 넣음 (KOPIS 상세는 바뀜 확인용으로 계속)
    assert items(sessions, "CONCERT")["PF100"].status == "SENT"


def _same_name_web():
    w = web()
    w.add(quote("정우"), {"artists": [
        {"id": "nct", "name": "정우", "score": 100, "country": "KR", "disambiguation": "NCT", "gender": "male", "aliases": []},
        {"id": "jw", "name": "정우", "score": 74, "country": "KR", "disambiguation": "CTRSOUND", "gender": "female",
         "aliases": [{"name": "JUNGWOO"}]}]})
    w.add("artist/jw?", {"id": "jw", "name": "정우", "type": "Person", "gender": "Female", "aliases": [{"name": "JUNGWOO"}]})
    w.add("artist/nct?", {"id": "nct", "name": "정우", "gender": "Male", "aliases": []})
    w.add("release-group?artist=jw", {"release-groups": [{"id": "rgJ", "title": "철의 삶", "primary-type": "Album", "first-release-date": "2024-04-01"}]})
    w.add("release-group?artist=nct", {"release-groups": [{"id": "rgN", "title": "SUGAR", "primary-type": "Single", "first-release-date": "2025-11-28"}]})
    w.add("release?release-group=rgJ", {"releases": [{"date": "2024-04-01", "media": [{"tracks": [{"title": "철의 삶"}]}],
                                                       "label-info": [{"label": {"name": "CTRSOUND"}}]}]})
    return w


def test_same_name_artist_is_picked_from_the_concert_it_came_from(sessions):
    """정우 - 같은 이름의 한국 아티스트(NCT, 인디 싱어송라이터)가 여럿이면 이 이름이 나온 공연을 보고 Gemini 가 고름
    (전에는 점수 높은 NCT 정우로 이어 대중으로 빠지고 그 단독 공연까지 라인업 확인으로 빠졌음)"""
    solo = {**INDIE, "title": "정우 단독 공연", "artistNames": ["정우"], "venueName": "NOL 씨어터 합정"}
    llm = FakeLlm(concerts={"PF100": solo, "PF200": MAINSTREAM}, genres={"정우": ["FOLK"]}, picks={"정우": "jw"})
    studio = FakeStudio()
    out = runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, _same_name_web()), settings(kopis_key="KEY"),
                                                llm=llm, studio=studio), [])
    ids, context = llm.picked["정우"]
    assert ids == ["nct", "jw"] and context["title"] == "정우 단독 공연" and context["venueName"] == "NOL 씨어터 합정"
    assert [(a["name"], a["gender"]) for path, body in studio.posts if path == ARTISTS for a in body["items"]] == [("정우", "FEMALE")]
    assert out["stats"]["artist_same_name_picked"] == 1
    assert items(sessions, "CONCERT")["PF100"].status == "SENT"                # 단독 공연도 그대로


def test_same_name_artist_without_a_concert_is_not_guessed(sessions):
    """시드처럼 나온 공연을 모르는 이름은 고를 근거가 없어 만들지 않음 (Gemini 도 묻지 않음)"""
    llm = FakeLlm(concerts={"PF100": {**INDIE, "artistNames": []}, "PF200": MAINSTREAM}, picks={"정우": "jw"})
    out = runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, _same_name_web()), settings(kopis_key="KEY"),
                                                llm=llm, studio=FakeStudio()), ["정우"])
    assert "pick:정우" not in llm.calls and out["stats"]["artist_same_name_unresolved"] == 1
    assert not [i for i in items(sessions, "ARTIST").values() if json.loads(i.payload_json)["name"] == "정우"]


def test_artists_in_the_include_list_are_kept_even_if_judged_mainstream(sessions):
    """사용자가 넣기로 한 팀(seeds/include.txt, QWER) - Gemini 가 대중으로 봐도 보내고 앨범, 공연도 그대로"""
    w, studio = web(), FakeStudio()
    llm = FakeLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres={"검정치마": ["INDIE_ROCK"], "Silica Gel": ["INDIE_ROCK"]},
                  mainstream={"검정치마", "Silica Gel"})
    out = runner.run("dawn", sessions, Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY", include_ids=frozenset({"sg"})),
                                                llm=llm, studio=studio), ["검정치마"])
    status = {json.loads(i.payload_json)["name"]: i.status for i in items(sessions, "ARTIST").values()}
    assert status == {"검정치마": "SKIPPED", "Silica Gel": "SENT"}
    assert out["stats"]["artist_included"] == 1 and items(sessions, "CONCERT")["PF100"].status == "SENT"


def test_marks_go_once_for_held_artists_and_never_for_candidates_studio_rejected(sessions):
    """보낸 뒤 보류(HOLD)가 된 팀은 '보류 중' 표시를 한 번 보냄, Studio 가 거절한 후보(REVIEWED)는 표시가 바뀌어도 보내지 않음.
    처음 보낼 때부터 MusicBrainz 주소(identity)가 붙음 - 동명이인 확인용"""
    w = web()
    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    first = {i["name"]: i for path, body in studio.posts if path == ARTISTS for i in body["items"]}
    assert first["검정치마"]["evidence"][0] == {"for": "identity", "site": "MusicBrainz",
                                              "url": "https://musicbrainz.org/artist/bs", "title": "검정치마"}
    albums = [i for path, body in studio.posts if path == ALBUMS for i in body["items"]]
    assert all(a["evidence"][0]["for"] == "identity" and "release-group/" in a["evidence"][0]["url"] for a in albums)

    keys = {json.loads(i.payload_json)["name"]: k for k, i in items(sessions, "ARTIST").items()}
    held, rejected = keys["Silica Gel"], keys["검정치마"]   # 검정치마는 공연이 없어 뺀 팀의 공연(라인업)이 함께 바뀌지 않음
    with sessions() as s:
        s.execute(update(dbm.Item).where(dbm.Item.key == held).values(status="HOLD"))
        s.execute(update(dbm.Item).where(dbm.Item.key == rejected).values(status="SKIPPED", error="HIPHOP", studio_result="REVIEWED"))
        s.commit()
    p = pipeline(sessions, w, studio := FakeStudio())
    p.send()
    assert [(path, [i["external_id"] for i in body["items"]]) for path, body in studio.posts] == [(ARTISTS, [held])]
    assert [e["note"] for e in studio.posts[0][1]["items"][0]["evidence"] if e["for"] == "check"] == \
        ["크롤러가 보류 중 (장르를 정하지 못함) — 판단이 끝나 다시 올 때까지 승인하지 말 것"]
    assert items(sessions, "ARTIST")[held].status == "HOLD"          # 상태는 그대로
    p.send()
    assert len(studio.posts) == 1                                     # 한 번만


def test_concert_lineup_goes_with_artist_ids_or_why_the_team_is_missing(sessions):
    """공연 라인업 이름마다 아티스트 후보 id(크롤러 이름 연결 - 영어, 한글이 달라도 같은 팀) 또는 수집하지 못한 이유.
    관리자 화면이 이름 대신 id 로 맞추고, 수집 못 한 팀(라인업에 넣을 수 없음)을 구별하게"""
    from crawler.keys import norm
    w = web()
    studio = FakeStudio()
    runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    with sessions() as s:
        sg = s.scalar(select(dbm.ArtistLink.key).where(dbm.ArtistLink.site == "name", dbm.ArtistLink.site_id == norm("실리카겔")))
        s.add(dbm.ArtistMiss(name_norm=norm("없는 밴드"), name="없는 밴드", reason="not_found", checked_at=dbm.now()))
        s.commit()
    concert = next(i for path, body in studio.posts if path == CONCERTS for i in body["items"])
    assert sg and concert["concert"]["lineup"] == [{"name": "실리카겔", "externalId": sg}]

    p = pipeline(sessions, w, studio := FakeStudio())
    p.send()
    assert studio.posts == []                                         # 라인업 연결이 그대로면 다시 가지 않음
    assert p._lineup(["Silica Gel", "없는 밴드", "처음 듣는 팀"]) == [
        {"name": "Silica Gel", "externalId": sg},
        {"name": "없는 밴드", "missing": "한국 아티스트로 못 찾음 (외국 팀 · 표기 차이 · 동명이인)"},
        {"name": "처음 듣는 팀", "missing": "아직 찾지 않음 (다음 실행에)"}]


class CoolingLlm(FakeLlm):
    """Gemini 가 쉬는 중(503 COOLDOWN)인 척 - wait 하면 풀림, budget 을 넘는 시간이 남았으면 풀리지 않음"""
    def __init__(self, *args, left=1200.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.left, self.waited = left, []

    @property
    def exhausted(self):
        return self.left > 0

    def cooldown_left(self):
        return self.left

    def wait(self, seconds):
        self.waited.append(seconds)
        self.left = 0.0


def test_reask_waits_out_the_gemini_cooldown_within_the_run_budget(sessions):
    """점심 실행이 쉬는 중이라 판단 15팀, 소개 글 0 으로 끝남 -> 실행 몫(3시간) 안이면 쉬는 시간을 기다렸다 이어서 물음.
    남은 쉬는 시간이 몫보다 길면 기다리지 않고 다음 실행에"""
    from crawler import pipeline as pl
    w = web()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), ["검정치마"])
    with sessions() as s:
        s.execute(update(dbm.Item).where(dbm.Item.type == "ARTIST").values(input_hash="not-judged"))
        s.commit()
    genres = {"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC", "INDIE_ROCK"]}

    llm = CoolingLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres, left=1200.0)
    p = pipeline(sessions, w, FakeStudio(), llm)
    p._reask()
    assert llm.waited == [1200.0] and p.stats["artist_reask"] == 2 and p.stats["llm_cooldown_waited_s"] == 1200
    assert p._llm_wait_left == pl.LLM_WAIT_BUDGET - 1200 and "artist_not_judged_left" not in p.stats

    with sessions() as s:
        s.execute(update(dbm.Item).where(dbm.Item.type == "ARTIST").values(input_hash="not-judged"))
        s.commit()
    llm = CoolingLlm({"PF100": INDIE, "PF200": MAINSTREAM}, genres, left=pl.LLM_WAIT_BUDGET + 1)
    p = pipeline(sessions, w, FakeStudio(), llm)
    p._reask()
    assert llm.waited == [] and p.stats["artist_reask_deferred"] == 2 and p.stats["artist_not_judged_left"] == 2
