# 스키마 v1 — 논문·저자·인용·토픽 (2026-08-10)

## 노드

### Paper (라벨: Full 또는 Stub)

| 프로퍼티 | 타입 | 비고 |
|---|---|---|
| openalexId | string | PK. `W2194775991` 형태 |
| title | string | Stub은 없음 |
| abstract | string | Stub은 없음. 원본은 inverted index → 복원 필요 |
| citedByCount | int | Stub은 없음 |
| publicationDate | date | Stub은 없음 |
| embedding | float[] | 4주차에 추가. Stub은 없음 |

- **Full**: 우리 데이터셋 5만 건. 모든 프로퍼티 있음
- **Stub**: 5만 건 밖인데 인용 관계에 필요한 노드. openalexId만 있음

Stub이 필요한 이유: 공통 인용 패턴 `논문A → 논문X ← 논문B`에서 논문X가
데이터셋 밖이면 이 삼각형이 안 만들어진다. 벡터 검색이 못 잡는 연결을
그래프가 잡는 지점이므로 5주차 비교에 필요.

### Author

| 프로퍼티 | 타입 | 비고 |
|---|---|---|
| openalexId | string | PK. `A5100700361` 형태 |
| name | string | |

### Topic

| 프로퍼티 | 타입 | 비고 |
|---|---|---|
| openalexId | string | PK |
| name | string | |

토픽 계층(상위-하위)은 지금은 안 넣는다. 용도가 생기면 추가.

---

## 관계

### (Author)-[AUTHORED]->(Paper:Full)

| 프로퍼티 | 타입 | 비고 |
|---|---|---|
| position | int | 저자 순서 (1저자, 2저자, ...) |
| corresponding | boolean | 교신저자 여부 |
| institution | string | 소속 기관명. 표시용 (노드 아님) |

기관을 노드로 안 만드는 이유: 기관으로 필터링·탐색하는 질문이 없다.
"논문 보여줄 때 저자 옆에 소속 표시" 정도면 관계 프로퍼티로 충분.

### (Paper)-[CITES]->(Paper)

방향: A가 B를 인용했다 = `(A)-[CITES]->(B)`.
OpenAlex `referenced_works`가 "이 논문이 인용한 논문들"이므로 이 방향이 맞다.
Full → Full, Full → Stub 둘 다 가능.

### (Paper:Full)-[HAS_TOPIC]->(Topic)

| 프로퍼티 | 타입 | 비고 |
|---|---|---|
| score | float | 이 논문이 해당 토픽에 얼마나 해당하는지 |
