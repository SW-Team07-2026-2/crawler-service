import json
from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select, update

from crawler import db as dbm
from crawler import runner
from crawler.pipeline import Pipeline, fit_kopis_dates, to_concert

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
    assert bs["gender"] is None and bs["composition"] is None    # MusicBrainz 에 종류 · 멤버가 없으면 null (관리자가 승인 때)

    albums = {a["title"]: a for a in studio.posts[1][1]["items"]}
    assert albums["201"]["artist_ref"] == {"source": "crawler", "external_id": bs["external_id"]}
    assert "artist_id" not in albums["201"]
    assert [t["track_no"] for t in albums["201"]["tracks"]] == [1, 2]
    assert albums["201"]["physical_albums"] is None              # Discogs 토큰 없음 → 건드리지 않음

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

    # MusicBrainz 의 트랙이 바뀜 (캐시가 지난 뒤) → 그 앨범만 다시 감
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
    assert len(llm.calls) == llm_calls                              # 아티스트 원본(이름 · 앨범 목록 · 레이블)은 그대로


def test_known_artist_is_refreshed_without_seed_or_concert_and_genres_stay(sessions):
    """공연에서 찾은 실리카겔은 시드에 없음 — 그래도 다음 새벽에 신보가 들어오고, 장르는 다시 묻지 않음 (모든 앨범이 다시 가지 않게)"""
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
    assert sent[ALBUMS] == ["b-rgC"]                                 # 새 앨범만 — 옛 앨범 · 장르는 그대로
    assert [a["label"] for a in studio.posts[0][1]["items"]] == ["CAM"]   # 아티스트는 레이블이 바뀌어서만 다시 감
    assert studio.posts[0][1]["items"][0]["genres"] == ["PSYCHEDELIC"] and len(llm.calls) == llm_calls

    # 최근 앨범(rgC)은 7일 지나면 트랙 · 레이블을 다시 보고, 2011년 앨범(rgB)은 90일까지 캐시
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
    """11/22 성별 · 구성 — 처음엔 MusicBrainz 에 멤버가 없어 null, 멤버가 생기면 아티스트만 다시 감 (해시가 바뀜 · 장르는 다시 묻지 않음)"""
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
    assert (bs["name"], bs["gender"], bs["composition"]) == ("검정치마", "MALE", "SOLO")   # 한 명만 남은 팀 → 솔로
    assert bs["genres"] == ["INDIE_ROCK"] and len(llm.calls) == llm_calls
    assert bs["source_hash"] != before[bs_key].source_hash


def test_alias_only_person_without_releases_is_not_created(sessions):
    """11/18 실측: '이지민' 이 별칭인 작사가 · 배우(앨범 0개)가 걸림 — 앨범이 없으면 아티스트를 만들지 않음"""
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
    """11/23 실측: 새 이름 칸(50)을 매번 같은 실패 이름 50개가 차지해 뒤의 483개를 한 번도 못 보고 39팀에서 멈췄음"""
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
    """처음 fails 번은 part 가 든 주소에 500 — 그다음부터는 web 그대로 (외부 서버가 잠깐 안 되는 경우)"""

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
    """11/23 아웃박스 — MusicBrainz 가 잠깐 오류면 못 찾음(miss)으로 두지 않고 같은 실행 끝에 한 번 더"""
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

    for part, body in HYUKOH:                                           # 다음 실행 — 서버가 돌아옴
        w.add(part, body)
    w.calls.clear()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), [])
    searches = [u for u in w.calls if "artist?query=" in u]
    assert quote("혁오") in searches[0]                                 # 공연 출연진(실리카겔)보다 먼저
    assert retries(sessions) == {}


def test_concert_whose_cast_lookup_failed_is_cleaned_up_again_even_if_the_source_is_the_same(sessions):
    """원문이 같으면 다시 정리하지 않는데, 출연진 → 팀 조회가 실패한 공연은 영영 다시 안 보던 것 (11/23)"""
    w = web()
    for person, pid in [("박솔", "p1"), ("강경태", "p2")]:
        w.add(quote(person), {"artists": [{"id": pid, "name": person, "score": 100, "country": "KR", "aliases": []}]})
        w.add(f"artist/{pid}?inc=artist-rels", {"relations": [{"type": "member of band", "direction": "forward", "artist": {"name": "Band A"}}]})
    llm = FakeLlm(concerts={"PF100": {**INDIE, "artistNames": [], "unmatchedCast": ["박솔", "강경태"]}, "PF200": MAINSTREAM})
    flaky = Flaky(w, "artist/p1?inc=artist-rels", fails=2)                  # 실행 안에서 두 번(처음 · 실행 끝) 다 실패
    runner.run("lunch", sessions, Pipeline(sessions, make_http(sessions, flaky), settings(kopis_key="KEY"), llm=llm,
                                           studio=FakeStudio()), [])
    assert retries(sessions) == {("CONCERT", "PF100"): 2}
    assert json.loads(items(sessions, "CONCERT")["PF100"].payload_json)["concert"]["artistNames"] == ["Band A"]   # 강경태만으로

    studio = FakeStudio()
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])   # 원문 그대로 · 조회는 이제 됨
    assert llm.calls.count("concert:PF100") == 3                          # 첫 실행 2번(처음 · 끝) + 다음 실행 1번
    assert retries(sessions) == {}


def test_concert_skipped_by_the_gemini_daily_limit_waits_without_counting_as_a_failure(sessions):
    class Exhausted(FakeLlm):
        exhausted, stop_reason = True, "quota"
    runner.run("lunch", sessions, pipeline(sessions, web(), FakeStudio(), Exhausted()), [])
    assert retries(sessions) == {("CONCERT", "PF100"): 0, ("CONCERT", "PF200"): 0}   # 한도 탓 — 실패로 세지 않음
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, web(), FakeStudio(), llm), [])
    assert sorted(llm.calls) == ["concert:PF100", "concert:PF200"] and retries(sessions) == {}


def test_search_budget_spreads_the_month_over_the_days_left(sessions):
    """11/23 검색 한도 — 이번 달 남은 크레딧(1,000 - 예비 100 - 쓴 것)을 남은 날수로 나눈 것이 하루 몫 · 실행 횟수와 무관"""
    now = datetime(2026, 11, 23, 13, 0, tzinfo=runner.KST)                 # 11/23 ~ 11/30 = 8일
    utc = lambda d: d.astimezone(runner.timezone.utc).replace(tzinfo=None)

    def run(when, n):
        with sessions() as s:
            s.add(dbm.CrawlRun(kind="dawn", status="DONE", started_at=utc(when), stats_json=json.dumps({"web_search_requests": n})))
            s.commit()

    assert runner.search_budget(sessions, now) == 40                     # 900 // 8 = 112 → 실행당 40
    run(datetime(2026, 10, 31, 12, 0, tzinfo=runner.KST), 500)            # 지난달 — 세지 않음
    run(datetime(2026, 11, 20, 3, 30, tzinfo=runner.KST), 143)            # 이번 달 앞선 날 → 하루 (900 - 143) // 8 = 94
    run(datetime(2026, 11, 23, 3, 30, tzinfo=runner.KST), 40)
    assert runner.search_budget(sessions, now) == 40                     # 오늘 남은 54 → 실행당 40
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
    assert albums["Kyo181"]["physical_albums"] is None                # 실리카겔은 MusicBrainz 에 Discogs 연결 없음 → 건드리지 않음
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
    assert out["stats"]["artist_new_names"] == 0                       # 공연 · 인디스트릿에서 새 이름 없음 (연달아 돌릴 때 멈추는 기준)
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
    w.add_bytes("pfmIntroImage/PF_PF100_1", png(1200, 4000))      # 세로로 긴 소개 이미지 → 1000px 로 줄여 3조각
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
        "정보 출처: 공연예술통합전산망(KOPIS)"])                    # 가격 · 예매처 안내는 넣지 않음 (indig 가 직접 판매)


def test_changed_kopis_detail_is_sent_again_and_same_detail_is_not(sessions):
    w = web()
    runner.run("lunch", sessions, pipeline(sessions, w, FakeStudio()), [])
    expire_kopis(sessions)
    w.add("pblprfr/PF100?", KOPIS_DETAIL.replace("토요일(18:00)", "토요일(19:00)"))   # 출처에서 시간이 바뀜
    studio, llm = FakeStudio(), FakeLlm(concerts={"PF100": {**INDIE, "startsAt": "2026-12-12T19:00:00+09:00"}, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    assert llm.calls == ["concert:PF100"]                         # 그대로인 PF200 은 다시 묻지 않음
    [item] = studio.posts[0][1]["items"]                          # 다시 보냄 → 등록된 공연이면 Studio 가 CHANGED
    assert item["concert"]["startsAt"] == "2026-12-12T19:00:00+09:00"


def test_known_artists_seen_in_concert_text_go_to_haiku_as_hints(sessions):
    w = web()
    runner.run("dawn", sessions, pipeline(sessions, w, FakeStudio()), ["검정치마"])   # 검정치마 · Silica Gel 을 앎
    expire_kopis(sessions)
    w.add("pblprfr/PF100?", KOPIS_DETAIL.replace("<prfcast>실리카겔</prfcast>", "<prfcast>김춘추, 최웅희</prfcast>"))
    llm = FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM})
    runner.run("lunch", sessions, pipeline(sessions, w, FakeStudio(), llm), [])
    assert llm.facts["PF100"]["knownArtists"] == ["Silica Gel"]   # 제목의 "실리카겔"(별칭) → 대표 이름


def test_member_names_become_a_team_only_when_musicbrainz_agrees(sessions):
    w = FakeWeb([("/robots.txt", 200, "")])
    for person, pid, bands in [("박솔", "p1", ["Band A"]), ("강경태", "p2", ["Band A", "Side B"]), ("권오경", "p3", ["Band B", "Band C"])]:
        w.add(quote(person), {"artists": [{"id": pid, "name": person, "score": 100, "country": "KR", "aliases": []}]})
        w.add(f"artist/{pid}?inc=artist-rels", {"relations": [
            {"type": "member of band", "direction": "forward", "artist": {"name": b}} for b in bands]})
    p = pipeline(sessions, w, FakeStudio())
    # 박솔 · 강경태 → 둘 다 Band A (한 번만) · 권오경 → 팀이 둘이라 애매 → 남김 · 김없음 → MusicBrainz 에 없음 → 남김
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
    """한 번 찾을 때마다 검색 3번을 쓰는 척 — limit 번째부터는 한도가 다 찬 것처럼"""
    def __init__(self, limit):
        self.limit, self.asked, self.searches = limit, [], 0

    @property
    def exhausted(self):
        return len(self.asked) >= self.limit

    def ticket_open(self, concert):
        self.asked.append(concert["title"])
        self.searches += 3
        return None


def test_ticket_search_goes_soonest_first_and_carries_over_to_the_next_run(sessions):
    w = web()
    w.add("pblprfr?service=KEY", "<dbs><db><mt20id>PF100</mt20id></db><db><mt20id>PF200</mt20id></db>"
                                 "<db><mt20id>PF300</mt20id></db></dbs>")
    def dated(pf, day):   # KOPIS 기간을 그 공연 날짜로 — 원문 기간 밖 날짜는 크롤러가 고치므로
        return KOPIS_DETAIL.replace("PF100", pf).replace("2026.12.12</prfpdfrom><prfpdto>2026.12.13", f"{day}</prfpdfrom><prfpdto>{day}")
    w.add("pblprfr/PF100?", dated("PF100", "2027.03.01"))
    w.add("pblprfr/PF200?", dated("PF200", "2026.12.01"))
    w.add("pblprfr/PF300?", dated("PF300", "2020.01.01"))
    later = {**INDIE, "title": "늦은 공연", "startsAt": "2027-03-01T19:00:00+09:00"}
    sooner = {**INDIE, "title": "가까운 공연", "startsAt": "2026-12-01T19:00:00+09:00"}
    past = {**INDIE, "title": "지난 공연", "startsAt": "2020-01-01T19:00:00+09:00"}
    llm = FakeLlm(concerts={"PF100": later, "PF200": sooner, "PF300": past})

    first = LimitedTicketSearch(limit=1)
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio(), searcher=first)
    runner.run("lunch", sessions, p, [])
    assert first.asked == ["가까운 공연"]                   # 시작이 가까운 것부터 · 지난 공연은 찾지 않음
    assert p.stats["concert_ticket_deferred"] == 1

    second = LimitedTicketSearch(limit=5)
    p = Pipeline(sessions, make_http(sessions, w), settings(kopis_key="KEY"), llm=llm, studio=FakeStudio(), searcher=second)
    runner.run("lunch", sessions, p, [])
    assert second.asked == ["늦은 공연"]                    # 찾아본 공연은 원문이 같으면 다시 찾지 않음

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
    assert len(search.asked) == 1 and p.stats["concert_ticket_deferred"] == 1    # 3크레딧을 쓴 뒤 멈춤 → 나머지는 아티스트 조사 몫


# ---------- MusicBrainz 밖에서 찾기 (인디스트릿 · 김밥레코즈 · 웹 검색) ----------

LABEL_URL = "https://label.example/power-andre-99"
STORE_URL = "https://store.example/silica-tee"


class FakeSearcher:
    def __init__(self, releases=None, goods=None):
        self.r, self.g, self.calls = releases or {}, goods or {}, []

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
    assert [(o["size"], o["price"], o["qty"]) for o in tee["options"]] == [("S", None, None), ("M", None, None)]   # 판매가 · 재고는 관리자가
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

    assert out["stats"]["artist_not_found"] == 1                       # 인디스트릿의 리플리카 — MusicBrainz 에 없어 만들지 않음
    assert out["stats"]["physical_no_album"] == 1                      # '어딘가의 앨범' CD — 어느 앨범인지 몰라 붙이지 않음
    assert ("releases", "Silica Gel") in web_search.calls and ("goods", "Silica Gel") in web_search.calls
    sent = {path: [i["external_id"] for i in body["items"]] for path, body in studio.posts}
    assert len(sent[ALBUMS]) == 4


def test_musicbrainz_later_adds_a_found_album_under_the_same_key(sessions):
    """웹 검색으로 먼저 보낸 앨범이 나중에 MusicBrainz 에 생기면 같은 후보(같은 external_id)를 MusicBrainz 값으로 고침 — 둘이 되지 않음"""
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
    """11/19 KOPIS 목록 400 하나로 새벽 실행 전체가 멈췄음 → 공연만 실패로 남고 아티스트 · 앨범은 그대로 보냄"""
    w, studio = web(), FakeStudio()
    w.add("pblprfr?service=KEY", "<html><body>Bad Request</body></html>", status=400)
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    assert out["status"] == "FAILED"
    assert out["error"].startswith("공연: RuntimeError: GET ") and "service=***" in out["error"]
    assert out["error"].endswith("→ 400 <html><body>Bad Request</body></html>")       # 응답 본문도 남김
    assert [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]                   # 공연 말고는 보냄
    with sessions() as s:
        assert s.get(dbm.CrawlRun, out["run"]).status == "FAILED"


def test_dawn_retries_the_failed_concert_stage_at_the_end(sessions):
    """11/22 KOPIS 차단(Request Blocked)이 몇 분 이어져 공연 단계만 실패 → 새벽 실행은 아티스트 단계 뒤 한 번 더"""
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
    """Gemini 가 계속 503 → 남은 공연은 다음 실행에, 아티스트 · 앨범은 그대로 보내고 실행은 DONE"""
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
    assert out["stats"]["concert_llm_unavailable"] == 2                          # 첫 공연은 503 · 둘째는 부르지도 않음
    assert "concert_llm_quota" not in out["stats"]                               # 한도로 잘못 적지 않음
    assert items(sessions, "CONCERT") == {}                                      # 저장하지 않음 → 다음 실행에 다시
    assert [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]


def test_kopis_down_stops_the_concert_loop_after_five_misses_in_a_row(sessions):
    """다시 불러도 상세가 5번 연달아 실패 → 남은 공연은 하나씩 기다리지 않고 다음 실행에 (실행이 다음 일정까지 늘어지지 않게)"""
    w, studio = web(), FakeStudio()
    ids = [f"PF{n}00" for n in range(1, 8)]
    w.add("pblprfr?service=KEY", "<dbs>" + "".join(f"<db><mt20id>{i}</mt20id></db>" for i in ids) + "</dbs>")
    for i in ids:
        w.add(f"pblprfr/{i}?", "", status=500)
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio), ["검정치마"])
    # 실행 끝 다시 하기(11/23)에서 못 받은 5건부터 한 번 더 → 또 5번 실패 → 멈춤 · 5건은 다음 실행 맨 앞에
    assert out["stats"]["concert_fetch_error"] == 10 and out["stats"]["concert_kopis_down"] == 2
    assert out["stats"]["retry_left"] == 5
    assert not any("pblprfr/PF600" in u or "pblprfr/PF700" in u for u in w.calls)
    assert out["status"] == "DONE" and [path for path, _ in studio.posts] == [ARTISTS, ALBUMS]


def test_discogs_error_skips_only_the_physical_not_the_artist(sessions):
    """11/19 안치환: MusicBrainz 가 가리키는 Discogs 아티스트가 지워져 404 → 아티스트 전체가 빠졌음"""
    w = web()
    w.add("artist/bs?", {"id": "bs", "name": "검정치마", "aliases": [],
                         "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/9"}}]})
    w.add("api.discogs.com/artists/9", {"message": "Artist not found."}, status=404)
    llm = FakeLlm(genres={"검정치마": ["INDIE_ROCK"]})
    p = Pipeline(sessions, make_http(sessions, w), settings(discogs_token="T"), llm=llm, studio=FakeStudio())
    p.artists(["검정치마"])
    assert p.stats["discogs_error"] == 1 and "artist_error" not in p.stats and p.stats["artist_ok"] == 1
    albums = {json.loads(i.payload_json)["title"]: json.loads(i.payload_json) for i in items(sessions, "ALBUM").values()}
    assert albums["201"]["physical_albums"] is None                   # 피지컬만 건드리지 않음


def test_artist_without_genres_waits_until_genres_are_judged(sessions):
    """Gemini 한도로 장르를 못 정한 새 아티스트는 HOLD — 아티스트 · 앨범 모두 보내지 않고, 장르를 정한 실행에 함께 감"""
    from crawler.llm import LlmSkip

    class NoQuota(FakeLlm):
        def artist_genres(self, name, aliases, albums, label):
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


def test_concert_dates_outside_the_kopis_period_are_fixed_and_flagged_for_review():
    """11/22 사뮈 투어: 포스터에 도시별 일정이 함께 있어 KOPIS(12/19 17:00) 대신 다른 회차(12/11 20:30)를 고름 → KOPIS 에 맞추고 '검수 필요'"""
    detail = {"prfpdfrom": "2026.12.19", "prfpdto": "2026.12.19", "dtguidance": "토요일(17:00)"}
    c = {"startsAt": "2026-12-11T20:30:00+09:00", "endsAt": "2026-12-11T22:30:00+09:00"}
    notes = fit_kopis_dates(detail, c)
    assert c == {"startsAt": "2026-12-19T17:00:00+09:00", "endsAt": "2026-12-19T19:00:00+09:00"}   # 길이 2시간은 그대로
    assert notes[0].startswith("검수 필요 — 시작 2026-12-11 20:30 → 2026-12-19 17:00 · 끝 2026-12-11 22:30 → 2026-12-19 19:00")
    assert fit_kopis_dates(detail, c) == []                                       # 고친 값은 다시 걸리지 않음

    time_only = {"startsAt": "2026-11-27T20:00:00+09:00", "endsAt": "2026-11-27T22:00:00+09:00"}   # RAMPAGE FEST 전야제
    assert fit_kopis_dates({"prfpdfrom": "2026.11.27", "prfpdto": "2026.11.27", "dtguidance": "금요일(20:30)"}, time_only)
    assert time_only["startsAt"] == "2026-11-27T20:30:00+09:00"

    run = {"startsAt": "2026-10-30T19:30:00+09:00", "endsAt": "2026-10-31T18:00:00+09:00"}   # 여러 날 · 시각 여럿 → 날짜만
    assert fit_kopis_dates({"prfpdfrom": "2026.11.20", "prfpdto": "2026.11.21", "dtguidance": "금요일(19:30), 토요일(16:00)"}, run)
    assert run == {"startsAt": "2026-11-20T19:30:00+09:00", "endsAt": "2026-11-21T18:00:00+09:00"}

    odd = {"startsAt": "2026-11-28T14:00:00+09:00", "endsAt": "2026-11-29T16:00:00+09:00"}   # 신촌블루스 — 끝도 하루 밀려 있었음
    assert fit_kopis_dates({"prfpdfrom": "2026.11.29", "prfpdto": "2026.11.29", "dtguidance": "일요일(14:00)"}, odd)
    assert odd == {"startsAt": "2026-11-29T14:00:00+09:00", "endsAt": "2026-11-29T16:00:00+09:00"}

    tour = {"startsAt": "2026-12-19T18:30:00+09:00", "endsAt": "2026-12-26T18:30:00+09:00"}   # 론 브랜튼 — 맞추면 끝 = 시작
    notes = fit_kopis_dates({"prfpdfrom": "2026.12.25", "prfpdto": "2026.12.25", "dtguidance": "금요일(16:00,19:30)"}, tour)
    assert tour == {"startsAt": "2026-12-25T18:30:00+09:00", "endsAt": None} and "끝 2026-12-26 18:30 → 비움" in notes[0]

    fine = {"startsAt": "2026-12-12T18:00:00+09:00", "endsAt": None}
    assert fit_kopis_dates({"prfpdfrom": "2026.12.12", "prfpdto": "2026.12.13", "dtguidance": "토요일(18:00), 일요일(17:00)"}, fine) == []
    assert fit_kopis_dates({}, {"startsAt": None}) == []                           # 기준이 없으면 그대로


def test_concerts_sent_before_the_date_check_are_fixed_without_asking_gemini_again(sessions):
    """이미 정리해 보낸 공연(원문 그대로)도 다음 실행에서 날짜를 고쳐 다시 보냄 — Gemini 는 다시 부르지 않음"""
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
