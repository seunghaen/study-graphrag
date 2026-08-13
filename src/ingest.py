"""
OpenAlex → Neo4j 적재 파이프라인

전체 흐름:
1. Neo4j에 constraint 생성 (중복 방지 + 인덱스 자동 생성)
2. OpenAlex API를 커서 페이징으로 호출 (100건씩)
3. 페이지마다: Paper, Author, Topic 노드 MERGE → 관계 MERGE
"""

import os
import sys

import requests
from neo4j import GraphDatabase

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
# 접속 정보는 환경변수로 받는다. 코드가 보는 건 os.environ 하나뿐이고
# 그 값을 누가 채웠는지(로컬 .env / Lambda 환경변수 / Secrets Manager)는 코드가 모른다.
# 덕분에 6주차에 실행 위치가 바뀌어도 이 파일은 안 고친다.
#
# URI·USER는 비밀이 아니라 기본값을 둔다. 비번만 기본값이 없다 —
# 있으면 "환경변수 세팅을 깜빡했는데 조용히 돌아가는" 경우가 생기고,
# 그러면 하드코딩을 뺀 의미가 없어진다. 없으면 즉시 죽는 게 맞다.
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

# OpenAlex API 설정
# mailto를 넣으면 polite pool (요청 한도 10배)
OPENALEX_EMAIL = "tmdgus9701@gmail.com"
OPENALEX_BASE = "https://api.openalex.org/works"
# 200이 OpenAlex 최대값. 100 → 200으로 올리면 1,000건 기준 27.3초 → 15.5초 (1.8배).
# 요청당 시간은 2.7 → 3.1초로 거의 안 늘었다. 데이터 양이 아니라 왕복 횟수가 비용이라는 뜻.
# 적재 병목은 Neo4j 쓰기가 아니라 API 왕복이다 (1,000건 적재 시 CPU 7%).
PER_PAGE = 200
TARGET_COUNT = 50000  # 목표 적재 건수

# 적재 범위 — T10181 = Natural Language Processing Techniques, 2020년 이후
# 이 필터로 51,907건 (2026-08-12 측정)
#
# 이 조합을 고른 이유는 "공통 인용 밀도"다. 200건 표본에서 A→X←B 삼각형을 세보니:
#   NLP 2020~ 169.0 / NLP 2024~ 85.1 / NetSecurity 2023~ 46.6 / Blockchain 2023~ 21.4
# 그래프 RAG가 벡터 RAG보다 나은 지점이 딱 이 확장 단계 하나라서(docs/eval.md),
# 데이터에 A→X←B가 없으면 5주차 비교가 "차이 없음"으로 끝난다.
#
# NLP는 LLM이 제일 잘 아는 분야라 도메인 체크리스트 1번이 약하지만,
# eval.md의 질문 자격 3번("LLM이 그냥 맞히면 탈락")이 설계상 걸러준다.
# 대신 평가셋 만들 때 유명 논문 쌍은 탈락이 많을 것 — 덜 알려진 쌍을 골라야 한다.
OPENALEX_FILTER = "type:article,topics.id:T10181,publication_year:>2019"


# ──────────────────────────────────────────────
# 1단계: constraint 생성
# ──────────────────────────────────────────────
# 데이터 넣기 전에 한 번만 실행하면 된다.
# uniqueness constraint를 걸면 인덱스가 자동으로 따라온다.
# IF NOT EXISTS라서 이미 있으면 무시된다.
CONSTRAINT_QUERIES = [
    "CREATE CONSTRAINT paper_openalex_id IF NOT EXISTS "
    "FOR (p:Paper) REQUIRE p.openalexId IS UNIQUE",

    "CREATE CONSTRAINT author_openalex_id IF NOT EXISTS "
    "FOR (a:Author) REQUIRE a.openalexId IS UNIQUE",

    "CREATE CONSTRAINT topic_openalex_id IF NOT EXISTS "
    "FOR (t:Topic) REQUIRE t.openalexId IS UNIQUE",
]


def create_constraints(driver):
    """constraint를 하나씩 실행한다."""
    with driver.session() as session:
        for query in CONSTRAINT_QUERIES:
            session.run(query)
    print(f"constraint {len(CONSTRAINT_QUERIES)}개 생성 완료")


# ──────────────────────────────────────────────
# 2단계: OpenAlex API 호출 (커서 페이징)
# ──────────────────────────────────────────────
def fetch_works(target=None):
    """
    OpenAlex에서 논문을 커서 페이징으로 가져오는 제너레이터.

    제너레이터란: 데이터를 한꺼번에 메모리에 올리지 않고,
    yield를 만나면 값을 하나 돌려주고 멈췄다가,
    다음 요청이 오면 이어서 실행한다.
    5만 건을 한번에 리스트로 들고 있을 필요가 없어진다.

    커서 페이징: 첫 요청은 cursor=*로 보내고,
    응답의 meta.next_cursor 값을 다음 요청에 넣는다.
    next_cursor가 None이면 끝.
    """
    target = target or TARGET_COUNT
    cursor = "*"  # 첫 요청은 항상 *
    total_fetched = 0

    while cursor and total_fetched < target:
        params = {
            "filter": OPENALEX_FILTER,
            "per_page": PER_PAGE,
            "cursor": cursor,
            "mailto": OPENALEX_EMAIL,
        }
        response = requests.get(OPENALEX_BASE, params=params)
        response.raise_for_status()  # HTTP 에러 나면 바로 터뜨림
        data = response.json()

        results = data.get("results", [])
        if not results:
            break

        yield results  # 이 페이지의 논문 목록을 돌려줌

        total_fetched += len(results)
        cursor = data.get("meta", {}).get("next_cursor")

        # 진행 상황 출력
        total = data.get("meta", {}).get("count", "?")
        print(f"  {total_fetched} / {total} 건 수신")


# ──────────────────────────────────────────────
# 3단계: 초록 복원
# ──────────────────────────────────────────────
def restore_abstract(inverted_index):
    """
    OpenAlex의 abstract_inverted_index를 원래 문장으로 복원한다.

    원본 형태: {"Using": [0], "thematic": [1, 5], "analysis": [2], ...}
    → 위치(숫자)를 키로, 단어를 값으로 뒤집은 뒤 순서대로 이어 붙인다.
    """
    if not inverted_index:
        return None
    # {단어: [위치들]} → {위치: 단어}로 뒤집기
    word_positions = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions[pos] = word
    # 위치 순서대로 정렬해서 이어 붙이기
    return " ".join(word_positions[i] for i in sorted(word_positions))


# ──────────────────────────────────────────────
# 4단계: 페이지 하나를 Neo4j에 적재
# ──────────────────────────────────────────────
#
# 여기가 핵심이다. 한 페이지(논문 100건)가 오면:
#   1) Paper 노드 MERGE  (Full로)
#   2) Author 노드 MERGE + AUTHORED 관계 MERGE
#   3) Topic 노드 MERGE + HAS_TOPIC 관계 MERGE
#   4) CITES 관계 MERGE  (인용 대상이 없으면 Stub Paper 생성)
#
# MERGE는 "있으면 가져오고 없으면 만든다".
# 그래서 Stub이 먼저 생겼다가 나중에 Full 데이터가 오면 프로퍼티가 채워진다.
#
# UNWIND: 리스트를 받아서 한 줄씩 펼쳐서 처리한다.
# Python에서 for문으로 한 건씩 보내면 네트워크 왕복이 100번이지만,
# UNWIND로 100건을 한 번에 보내면 왕복 1번. 5만 건일 때 차이가 크다.

# ── Cypher 쿼리들 ──

# Paper MERGE 쿼리
#
# ON CREATE / ON MATCH를 안 나누고 그냥 SET을 쓴다 = 매번 덮어쓴다.
# 이유는 멱등성 — 몇 번 돌리든 결과가 같아야 한다.
# 변환 코드(restore_abstract 등)를 고치고 재실행하면 기존 데이터가 갱신된다.
# ON CREATE만 쓰면 이미 있는 노드는 영영 안 고쳐져서, 고칠 때마다 DB를 비워야 한다.
#
# REMOVE paper:Stub — 인용 때문에 Stub으로 먼저 만들어져 있던 노드가
# 여기서 Full 데이터를 받으면 Stub 딱지를 뗀다. (스키마 v1에서 Full/Stub은 배타적)
# 반대 방향(Full → Stub)은 안 생긴다. CITES 쿼리가 ON CREATE로만 Stub을 붙이기 때문.
#
# date(null)은 에러가 아니라 NULL이라서 publication_date가 없는 논문도 안전하다.
QUERY_MERGE_PAPERS = """
UNWIND $papers AS p
MERGE (paper:Paper {openalexId: p.id})
SET paper.title = p.title,
    paper.abstract = p.abstract,
    paper.citedByCount = p.citedByCount,
    paper.publicationDate = date(p.publicationDate)
SET paper:Full
REMOVE paper:Stub
"""

# Author MERGE + AUTHORED 관계 쿼리
#
# 원칙: MERGE는 그 자리에서 "완전한 노드"를 만들 수 있을 때만 쓴다.
#   - Author는 openalexId + name이 전부고 둘 다 여기 있다 → MERGE
#   - Paper는 title/abstract/citedByCount/publicationDate가 필요한데
#     여기엔 paperId뿐이다 → MERGE로 만들면 ID만 있는 껍데기가 생긴다.
#     그 껍데기는 Stub과 구분이 안 되므로 나중에 원인 추적이 불가능해진다.
#     Paper는 바로 앞 QUERY_MERGE_PAPERS에서 이미 만들어졌으므로 MATCH로 충분.
#     혹시 못 찾으면 그 줄은 조용히 건너뛴다 = 그래프에 쓰레기가 안 남는다.
#
# 저자 식별을 name이 아니라 openalexId로 하는 이유:
#   동명이인(J. Kim이 수천 명)이 노드 하나로 뭉치고,
#   같은 사람의 표기 흔들림(S. Kim / Seunghyun Kim)이 노드를 쪼갠다.
#   name은 정체성이 아니라 표시용 프로퍼티.
QUERY_MERGE_AUTHORS = """
UNWIND $authors AS a
MERGE (author:Author {openalexId: a.authorId})
SET author.name = a.name
WITH a, author
MATCH (paper:Paper {openalexId: a.paperId})
MERGE (author)-[r:AUTHORED]->(paper)
SET r.position = a.position,
    r.corresponding = a.corresponding,
    r.institution = a.institution
"""

# Topic MERGE + HAS_TOPIC 관계 쿼리
#
# Author 쿼리와 완전히 같은 구조. Topic은 topicId + name이 전부라 여기서 완전히 만들 수 있고,
# Paper는 이미 있어야 하므로 MATCH.
# 관계 방향은 스키마 v1대로 (Paper)-[HAS_TOPIC]->(Topic).
QUERY_MERGE_TOPICS = """
UNWIND $topics AS t
MERGE (topic:Topic {openalexId: t.topicId})
SET topic.name = t.name
WITH t, topic
MATCH (paper:Paper {openalexId: t.paperId})
MERGE (paper)-[r:HAS_TOPIC]->(topic)
SET r.score = t.score
"""

# CITES 관계 쿼리
#
# 여기만 "완전한 노드일 때만 MERGE" 원칙의 예외다. cited는 openalexId만 있는 껍데기로 생긴다.
# 그래도 되는 이유는 :Stub 라벨이 붙기 때문 — "일부러 비워둔 노드"라는 사실이 그래프에 기록된다.
# 라벨 없는 껍데기(Author 쿼리에서 MERGE를 썼을 때 생길 것)는 Full도 Stub도 아닌
# 정체불명이 되어 버그인지 설계인지 구분할 수 없다. 차이는 불완전함이 표시되느냐다.
#
# cited가 Stub이 되는 경우는 둘:
#   1) 아직 적재 안 된 페이지의 논문 (나중에 Paper 쿼리가 Full로 승격시킨다)
#   2) 애초에 5만 건 밖의 논문 (영원히 Stub. 이게 공통 인용 패턴 A→X←B를 살린다)
#
# ON CREATE인 이유: 이미 Full인 논문이 인용당했을 때 :Stub이 덧붙으면
# 스키마 v1의 "Full 또는 Stub" 배타성이 깨진다. ON CREATE면 기존 노드는 안 건드린다.
QUERY_MERGE_CITATIONS = """
UNWIND $citations AS c
MATCH (citing:Paper {openalexId: c.fromId})
MERGE (cited:Paper {openalexId: c.toId})
ON CREATE SET cited:Stub
MERGE (citing)-[:CITES]->(cited)
"""


def process_page(driver, works):
    """
    논문 목록(한 페이지)을 받아서 Neo4j에 적재한다.

    OpenAlex 응답에서 필요한 필드를 뽑아서
    Cypher가 기대하는 형태의 딕셔너리 리스트로 변환한 뒤,
    쿼리 4개를 순서대로 실행한다.
    """

    # ── 데이터 변환: API 응답 → Cypher 파라미터 ──

    papers = []    # Paper 노드용
    authors = []   # Author + AUTHORED 관계용
    topics = []    # Topic + HAS_TOPIC 관계용
    citations = [] # CITES 관계용

    for work in works:
        # OpenAlex ID에서 URL 부분 떼기: "https://openalex.org/W123" → "W123"
        paper_id = work["id"].replace("https://openalex.org/", "")

        # Paper 데이터
        papers.append({
            "id": paper_id,
            "title": work.get("title"),
            "abstract": restore_abstract(work.get("abstract_inverted_index")),
            "citedByCount": work.get("cited_by_count", 0),
            "publicationDate": work.get("publication_date"),
        })

        # Author 데이터 — authorships 배열에서 뽑기
        for i, authorship in enumerate(work.get("authorships", [])):
            author_info = authorship.get("author") or {}
            # .get("id", "")로는 부족하다. 키가 존재하면서 값이 None인 응답이 실제로 온다
            # (저자가 disambiguation 안 된 경우). 그때 기본값이 안 먹고 None이 그대로 나온다.
            author_id = (author_info.get("id") or "").replace("https://openalex.org/", "")
            if not author_id:
                continue
            # 소속 기관 — institutions 배열의 첫 번째 것만
            institutions = authorship.get("institutions", [])
            institution_name = institutions[0].get("display_name") if institutions else None

            authors.append({
                "authorId": author_id,
                "name": author_info.get("display_name"),
                "paperId": paper_id,
                "position": i + 1,  # 1저자, 2저자, ...
                "corresponding": authorship.get("is_corresponding", False),
                "institution": institution_name,
            })

        # Topic 데이터 — topics 배열에서 뽑기
        for topic in work.get("topics", []):
            topic_id = (topic.get("id") or "").replace("https://openalex.org/", "")
            if not topic_id:
                continue
            topics.append({
                "topicId": topic_id,
                "name": topic.get("display_name"),
                "paperId": paper_id,
                "score": topic.get("score", 0.0),
            })

        # Citation 데이터 — referenced_works 배열에서 뽑기
        for ref in work.get("referenced_works", []):
            cited_id = ref.replace("https://openalex.org/", "")
            citations.append({
                "fromId": paper_id,
                "toId": cited_id,
            })

    # ── Cypher 실행 (순서 중요: 노드 먼저, 관계 나중) ──

    with driver.session() as session:
        if papers:
            session.run(QUERY_MERGE_PAPERS, papers=papers)
        if authors:
            session.run(QUERY_MERGE_AUTHORS, authors=authors)
        if topics:
            session.run(QUERY_MERGE_TOPICS, topics=topics)
        if citations:
            session.run(QUERY_MERGE_CITATIONS, citations=citations)


# ──────────────────────────────────────────────
# 메인 실행
# ──────────────────────────────────────────────
def main():
    # 적재 건수를 인자로 받는다. 안 주면 TARGET_COUNT(5만).
    #   python src/ingest.py 200   ← 소량 테스트용
    target = int(sys.argv[1]) if len(sys.argv) > 1 else TARGET_COUNT

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # 연결 확인
    driver.verify_connectivity()
    print("Neo4j 연결 성공")

    # 1) constraint 생성
    create_constraints(driver)

    # 2) 페이지 반복하면서 적재
    print(f"적재 시작... (목표 {target:,}건)")
    page_count = 0
    for works in fetch_works(target):
        process_page(driver, works)
        page_count += 1

    print(f"완료! 총 {page_count} 페이지 처리")

    driver.close()


if __name__ == "__main__":
    main()
