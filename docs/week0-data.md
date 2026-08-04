# 0주차 — 논문 데이터 실재 확인

**결론: OpenAlex 단독 채택. 무료 한도 안에서 가능. 0주차 통과 (2026-08-04).**

판정의 핵심은 데이터 양이 아니라 **관계의 존재 여부**다.
엔티티가 많아도 관계가 평평하면 벡터 RAG로 충분하고 프로젝트 전제가 무너진다.

## 확인 항목

1. 논문 메타데이터 (제목, 초록, 발행일) — 벡터 검색용 텍스트가 되는가
2. **인용 관계** — 논문→논문. 이게 없으면 그래프의 핵심이 죽는다
3. **저자 관계** — 저자→논문←공저자. 되돌아오는 패턴의 원천. 동명이인이 구분되는가(저자 ID 유무)
4. 분야/카테고리 — 교차 질의용
5. 인용 수·발행일 — 집계와 필터용

---

## API 실물 응답 비교 (2026-08-04)

| 항목 | arXiv | OpenAlex |
|---|---|---|
| 1. 메타데이터 | ✅ `title` `summary`(원문 초록) `published` | ✅ 단 초록이 `abstract_inverted_index`(단어→위치) — 복원 필요 |
| 2. **인용 관계** | ❌ **없음** | ✅ `referenced_works` — 상대 논문 ID 배열 통째로 |
| 3. **저자 ID** | ❌ 이름 문자열뿐 (`Wei Zhao` 동명이인 구분 불가) | ✅ `A5100700361` + ORCID + 소속 기관 ID |
| 4. 분야 | ✅ `category` 다중 | ✅ `topics` / `concepts` (score 있음) |
| 5. 인용 수·날짜 | ❌ 인용 수 없음 | ✅ `cited_by_count` + `counts_by_year` |

호출한 URL:

```
http://export.arxiv.org/api/query?search_query=cat:cs.CL&start=0&max_results=5
https://api.openalex.org/works?filter=concepts.id:C41008148&per-page=2
```

**Semantic Scholar는 익명 호출이 429.** 전 세계 익명 사용자가 한도를 공유하는 구조라
재시도로 뚫리지 않는다. 무료 API 키 신청 시 개인 한도를 받으나 승인에 며칠 걸린다.

### arXiv 단독 탈락 — 저자 ID가 없는 게 치명적이다

인용이 없는 것보다 이쪽이 더 위험하다. 이름으로 저자 노드를 만들면
서로 남남인 `Wei Zhao` 수십 명이 한 노드로 합쳐진다. 그러면 핵심 패턴이

```
저자A → 논문X ← 저자B      (공저)
```

**틀린 답을 내는 그래프**가 된다. 없는 것보다 나쁘다.

### OpenAlex는 되돌아오는 패턴을 두 종류 준다

```
저자A → 논문X ← 저자B      (공저)
논문A → 논문X ← 논문B      (공통 인용)
```

두 번째는 arXiv였으면 아예 못 만들었다. 도메인 체크리스트 3번(되돌아오는 관계) 통과.

---

## 요금 판정 — 무료 한도로 충분

2026년 2월부터 사용량 기반 과금으로 바뀌었다. **데이터셋 자체는 여전히 무료**, 과금 대상은 API 서비스다.

| 엔드포인트 | 호출당 | 비고 |
|---|---|---|
| 단일 조회 (ID/DOI) | **$0** | 무제한 |
| **list + filter** | $0.0001 | 우리가 적재에 쓸 것 |
| **search** | $0.001 | **filter의 10배** |
| PDF/XML 다운로드 | $0.01 | 안 씀 |

무료 한도: **키 있으면 $1/day**, 키 없으면 $0.10/day. 키 발급은 무료·즉시(openalex.org/settings/api).

**우리 규모 계산.** 논문 5만 건을 100개씩 페이징 → 500 calls × $0.0001 = **$0.05**.
하루 무료 한도의 5%다. (공식 예시: 핀란드 저자 논문 694k건 = 7k requests = $0.70, 무료 한도 안)

**인용·저자는 추가 호출이 필요 없다.** `referenced_works`와 `authorships`가 논문 응답에
인라인으로 들어 있다. 논문 목록만 페이징하면 그래프 전체가 딸려온다.

출처:
[요금 공지](https://blog.openalex.org/openalex-api-new-features-and-usage-based-pricing/) ·
[인증 가이드](https://developers.openalex.org/guides/authentication)

---

## 여기서 나온 설계 제약 (2주차로 넘김)

1. **`search=`는 쓰지 않는다.** `filter=`의 10배. 의미 검색은 Neo4j 벡터 인덱스가 하므로
   OpenAlex search를 쓸 이유가 없다
2. **초록 복원이 필요하다.** `abstract_inverted_index`는 `{단어: [위치들]}` 구조다.
   위치를 다 주므로 복원에 정보 손실이 거의 없고 20줄이면 된다
3. **분야 필터가 느슨하다.** `concepts.id:C41008148`(Computer science)에
   score 0.2짜리 심리학 논문(`Using thematic analysis in psychology`)이 딸려 나왔다.
   어떻게 좁힐지는 1주차 스키마에서 푼다
