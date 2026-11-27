"""소개 글 - 아티스트 bio, 앨범 description (한국어 위키백과 -> 웹 검색 -> 없으면 빈칸)"""
import json
from datetime import timedelta

from sqlalchemy import select

from conftest import FakeLlm, FakeStudio
from crawler import db as dbm
from crawler import describe as desc
from crawler import pipeline as pipeline_module
from crawler import runner
from crawler.sources import wikipedia
from crawler.sources.websearch import WebSearch
from test_pipeline import INDIE, MAINSTREAM, FakeSearcher, items, pipeline, web
from test_websearch import FakePages, FakeTavily, hit

ARTICLE = """실리카겔(Silica Gel)은 대한민국의 4인조 록 밴드이다. 2015년 결성되었다.
2016년 정규 1집 《Kyo181》을 발매했다.


== 데뷔 이전 ==
멤버 김한주(본명: 김한주)는 서울에서 태어났다.


=== 학창 시절 ===
같은 고등학교를 다녔다.


== 활동 ==
2018년 한국대중음악상 올해의 신인상을 받았다.


== 음반 목록 ==


== 각주 =="""
WIKI_URL = "https://ko.wikipedia.org/wiki/%EC%8B%A4%EB%A6%AC%EC%B9%B4%EA%B2%94_%28%EB%B0%B4%EB%93%9C%29"
BIO = [
    {"text": "실리카겔은 2015년 결성한 4인조 록 밴드입니다.", "source": 1},
    {"text": "멤버 김한주는 서울에서 태어났습니다.", "source": 1},              # 개인정보 - 뺌
    {"text": "2016년 첫 정규 앨범 《Kyo181》을 냈습니다.", "source": 1},
    {"text": "2019년 그래미상을 받았습니다.", "source": 1},                    # 출처에 없는 숫자 - 뺌
    {"text": "2018년 한국대중음악상 올해의 신인상을 받았습니다.", "source": 1},
]
ALBUM = [
    {"text": "실리카겔이 2016년에 낸 첫 정규 앨범입니다.", "source": 1},
    {"text": "Antifreeze 와 Dientes 2곡이 실렸습니다.", "source": 0},
]


def wiki_web():
    w = web()
    w.add("artist/sg?", {"id": "sg", "name": "Silica Gel", "aliases": [{"name": "실리카겔"}],
                         "relations": [{"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1"}}]})
    w.add("wbgetentities&ids=Q1", {"entities": {"Q1": {"sitelinks": {"kowiki": {"title": "실리카겔 (밴드)"}}}}})
    w.add("ko.wikipedia.org/w/api.php", {"query": {"pages": [{"title": "실리카겔 (밴드)", "extract": ARTICLE}]}})
    return w


def describing_llm():
    return FakeLlm(concerts={"PF100": INDIE, "PF200": MAINSTREAM},
                   genres={"검정치마": ["INDIE_ROCK"], "Silica Gel": ["PSYCHEDELIC", "INDIE_ROCK"]},
                   describes={"Silica Gel": BIO, "Kyo181": ALBUM})


def payload_of(sessions, type_, name):
    for i in items(sessions, type_).values():
        p = json.loads(i.payload_json)
        if p.get("name") == name or p.get("title") == name:
            return p, i
    raise AssertionError(name)


def described(sessions):
    with sessions() as s:
        return {(d.kind, d.key): d for d in s.scalars(select(dbm.Description))}


def test_artist_and_album_get_a_checked_korean_description_from_korean_wikipedia_and_are_sent_with_it(sessions):
    w, studio, llm = wiki_web(), FakeStudio(), describing_llm()
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["실리카겔", "검정치마"])
    assert out["status"] == "DONE"

    artist, _ = payload_of(sessions, "ARTIST", "Silica Gel")
    assert artist["bio"] == ("실리카겔은 2015년 결성한 4인조 록 밴드입니다. 2016년 첫 정규 앨범 《Kyo181》을 냈습니다. "
                             "2018년 한국대중음악상 올해의 신인상을 받았습니다.")
    [ev] = [e for e in artist["evidence"] if e["for"] == "bio"]
    assert (ev["site"], ev["url"], ev["title"]) == ("wikipedia", WIKI_URL, "실리카겔 (밴드)")
    # 위키백과 출처로 넘긴 본문 - 데뷔 이전, 그 아래 학창 시절, 각주 단락은 빠지고, 내용 없는 소제목도 빠짐
    [(_, sources)] = [d for d in llm.described if d[0] == "Silica Gel"]
    assert "태어났다" not in sources[0]["text"] and "고등학교" not in sources[0]["text"] and "음반 목록" not in sources[0]["text"]
    assert "[활동]" in sources[0]["text"]

    album, _ = payload_of(sessions, "ALBUM", "Kyo181")
    assert album["description"] == "실리카겔이 2016년에 낸 첫 정규 앨범입니다. Antifreeze 와 Dientes 2곡이 실렸습니다."
    assert [e["url"] for e in album["evidence"] if e["for"] == "description"] == [WIKI_URL]
    [(_, album_sources)] = [d for d in llm.described if d[0] == "Kyo181"]
    assert album_sources[1]["text"] == "2016년 정규 1집 《Kyo181》을 발매했다."      # 《제목》 으로 적은 문장만

    # 위키백과 연결이 없는 팀(검정치마, 웹 검색 키 없음) - Gemini 를 부르지 않고 빈칸 (관리자가 씀)
    bs, _ = payload_of(sessions, "ARTIST", "검정치마")
    assert "bio" not in bs and "describe:검정치마" not in llm.calls
    rows = described(sessions)
    assert {(k[0], r.reason) for k, r in rows.items() if r.text is None} == {("ARTIST", "no_source"), ("ALBUM", "no_source")}

    # 보낸 후보에 소개 글이 들어 있음 (전송 전 단계)
    [sent] = [i for path, body in studio.posts if path.endswith("/artists") for i in body["items"] if i["name"] == "Silica Gel"]
    assert sent["bio"] == artist["bio"]

    # 다음 실행 - 다시 쓰지 않고, 다시 보기로 후보를 새로 만들어도 소개 글이 그대로라 다시 보내지 않음
    calls, posts = len(llm.described), len(studio.posts)
    runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["실리카겔", "검정치마"])
    assert len(llm.described) == calls
    assert payload_of(sessions, "ARTIST", "Silica Gel")[0]["bio"] == artist["bio"]
    assert not [i for _, body in studio.posts[posts:] for i in body["items"] if i.get("name") == "Silica Gel"]


def test_lunch_run_writes_descriptions_left_by_the_evening_run(sessions, monkeypatch):
    w, studio, llm = wiki_web(), FakeStudio(), describing_llm()
    monkeypatch.setattr(pipeline_module, "DESCRIBE_ARTISTS", 0)
    out = runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["실리카겔"])
    assert out["stats"]["describe_artist_deferred"] == 1 and "bio" not in payload_of(sessions, "ARTIST", "Silica Gel")[0]
    monkeypatch.undo()

    out = runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    assert out["stats"]["describe_artist_written"] == 1
    artist, item = payload_of(sessions, "ARTIST", "Silica Gel")
    assert artist["bio"].startswith("실리카겔은 2015년") and item.status == "SENT"     # 바뀐 후보 -> 점심에 바로 보냄


def test_too_few_sentences_leave_it_blank_with_the_sources_for_the_admin_and_try_again_after_30_days(sessions):
    w, studio = wiki_web(), FakeStudio()
    llm = describing_llm()
    llm.describes["Silica Gel"] = BIO[:2]                     # 남는 문장이 하나 - 2문장 미만이면 빈칸
    runner.run("dawn", sessions, pipeline(sessions, w, studio, llm), ["실리카겔"])
    artist, item = payload_of(sessions, "ARTIST", "Silica Gel")
    assert "bio" not in artist
    assert [e["url"] for e in artist["evidence"] if e["for"] == "bio"] == [WIKI_URL]   # 찾은 출처는 관리자 참고용으로
    row = described(sessions)[("ARTIST", item.key)]
    assert (row.text, row.reason) == (None, "too_thin")

    llm.describes["Silica Gel"] = BIO
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    assert "bio" not in payload_of(sessions, "ARTIST", "Silica Gel")[0]           # 30일 안에는 다시 쓰지 않음
    with sessions() as s:
        s.get(dbm.Description, row.id).checked_at = dbm.now() - timedelta(days=31)
        s.commit()
    runner.run("lunch", sessions, pipeline(sessions, w, studio, llm), [])
    assert payload_of(sessions, "ARTIST", "Silica Gel")[0]["bio"].startswith("실리카겔은")


def test_artist_without_korean_wikipedia_uses_one_web_search_within_the_run_share(sessions, monkeypatch):
    page = {"url": "https://mag.example/bs", "title": "검정치마 인터뷰", "snippet": "…",
            "text": "검정치마는 조휴일의 1인 프로젝트로 2008년 정규 1집 《201》로 데뷔했다. 2011년 한국대중음악상을 받았다."}
    searcher = FakeSearcher(pages={"검정치마 음악 소개": [page]})
    llm = describing_llm()
    llm.describes["검정치마"] = [{"text": "검정치마는 2008년 정규 1집 《201》로 데뷔한 1인 프로젝트입니다.", "source": 1},
                                 {"text": "2011년 한국대중음악상을 받았습니다.", "source": 1}]
    p = pipeline(sessions, wiki_web(), FakeStudio(), llm)
    p.searcher = searcher
    monkeypatch.setattr(pipeline_module, "WEB_ARTISTS_PER_RUN", 0)
    p._web_left = 0                                           # 신보, 굿즈 웹 조사는 이 시험과 상관없음
    runner.run("dawn", sessions, p, ["검정치마"])
    bs, _ = payload_of(sessions, "ARTIST", "검정치마")
    assert bs["bio"] == "검정치마는 2008년 정규 1집 《201》로 데뷔한 1인 프로젝트입니다. 2011년 한국대중음악상을 받았습니다."
    assert [(e["site"], e["url"]) for e in bs["evidence"] if e["for"] == "bio"] == [("websearch", "https://mag.example/bs")]
    assert ("read_results", "검정치마 음악 소개") in searcher.calls
    assert not [c for c in searcher.calls if c[0] == "read_results" and "201" in c[1]]  # 피지컬 판본이 없는 앨범은 검색하지 않음


def test_web_pages_skip_namu_wiki_and_fall_back_to_the_snippet():
    tavily = FakeTavily({"q": [hit("https://namu.wiki/w/x", "나무위키", "조각"), hit("https://blocked.example/a", "막힘", "검색 조각"),
                               hit("https://mag.example/b", "기사", "조각")]})
    pages = FakePages({"https://mag.example/b": "기사 본문"})
    pages.read = lambda url, real=pages.read: (_ for _ in ()).throw(RuntimeError("x")) if "blocked" in url else real(url)
    out = WebSearch(None, tavily, pages).read_results("q")
    assert [(p["url"], p["text"]) for p in out] == [("https://blocked.example/a", "검색 조각"), ("https://mag.example/b", "기사 본문")]


def test_checked_keeps_only_sentences_backed_by_their_source():
    sources = [{"no": 0, "text": "제목 집 · 발매일 2020-03-01 · 트랙 4곡"},
               {"no": 1, "text": "한로로(2000년 11월 11일 ~ , 본명: 한지수)는 싱어송라이터이다. 2022년 3월 싱글 〈입춘〉으로 데뷔했다."}]
    raw = [{"text": "한로로는 2000년생 싱어송라이터입니다.", "source": 1},          # 생년
           {"text": "본명은 한지수입니다.", "source": 1},
           {"text": "한지수라는 이름으로도 활동했습니다.", "source": 1},          # 출처의 본명
           {"text": "2022년 싱글 〈입춘〉으로 데뷔했습니다.", "source": 1},
           {"text": "2022년 싱글 〈봄〉으로 데뷔했습니다.", "source": 1},           # 출처에 없는 제목
           {"text": "Debut single.", "source": 1},                                # 한글이 아님
           {"text": "없는 출처입니다.", "source": 7},
           {"text": "4곡이 실렸습니다.", "source": 0}]
    text, used = desc.checked(raw, sources, max_sentences=4, max_chars=300, min_sentences=2,
                              banned_names=desc.legal_names(s["text"] for s in sources))
    assert text == "2022년 싱글 〈입춘〉으로 데뷔했습니다. 4곡이 실렸습니다." and used == {0, 1}
    # 앨범 - MusicBrainz 값(0번)만으로 쓴 글은 빈칸
    assert desc.checked([{"text": "2020년에 냈습니다.", "source": 0}, {"text": "4곡이 실렸습니다.", "source": 0}], sources,
                        max_sentences=3, max_chars=200, min_sentences=2, facts_no=0) == (None, set())
    # 길이 - 넘치는 문장부터 버림
    long = [{"text": "가" * 150 + "입니다.", "source": 1}, {"text": "나" * 150 + "입니다.", "source": 1}]
    assert desc.checked(long, sources, max_sentences=4, max_chars=300, min_sentences=2) == (None, set())


def test_wikipedia_sentences_about_an_album_need_its_title_in_brackets():
    text = "2017년 첫 EP 《여름깃》을 냈다. 여름깃이라는 말을 좋아했다.\n== 음반 ==\n싱글 〈집〉은 2020년에 나왔다. 집에서 녹음했다."
    assert wikipedia.sentences_about(text, ["여름깃"]) == ["2017년 첫 EP 《여름깃》을 냈다."]
    assert wikipedia.sentences_about(text, ["집"]) == ["싱글 〈집〉은 2020년에 나왔다."]
