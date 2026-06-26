# 전화 기록 · 문장 보관 페이지 플랜 (+ 평균점수 프론트 계산 원칙)

작성 2026-06-25. **아직 구현 전** — 사용자 테스트 후 착수 예정. 여기엔 설계/엔드포인트/작업목록만.

## 설계 원칙: 집계는 프론트, 백엔드는 개별 데이터
- **백엔드** = 문장별 개별 점수(`Sentence.evaluation`: total_score/pronunciation/fluency/rhythm)만 영속화·반환. (영속화는 `ReviewService._apply_evaluation` 가 "마지막 시도"로 덮어씀 — 이미 동작.)
- **프론트** = 화면에 보일 **평균(게이지)** 을 문장별 점수로 직접 계산. 백엔드 `CallResult.average` 는 의존하지 않음(무시 가능).
- **라이브 복습 흐름**: 문장 채점(`POST /sentences/{id}/reviews`) 응답의 `evaluation` 을 상태에 반영 → 로컬 평균 재계산 → 게이지 즉시 갱신(서버 왕복 X). 백엔드는 개별 점수만 저장.
- 평균은 **채점된 문장만** 대상(점수 없는 문장 제외). 통화 직후엔 전부 null → 게이지 0/비활성.

### ⚠️ 선행 백엔드 변경 (기록 화면이 프론트 계산하려면 필수)
현재 `GET /calls/{id}/result` 의 `sentences`(`CallResultSentence`) 에 **문장별 점수가 없음**. 둘 중 하나:
- (권장) `CallResultSentence` 에 `evaluation: EvaluationOut | None` 필드 추가 → 결과 한 방에 개별 점수까지. `call_service.get_call_result` 에서 `CallResultSentence.model_validate(s)` 가 evaluation 까지 싣도록 스키마만 확장.
- 또는 기록 상세는 `GET /calls/{id}`(CallDetail, 이미 문장별 evaluation 포함) 를 사용.
- `CallResult.average` 는 남겨둬도 무해(프론트가 안 쓰면 그만). 원하면 제거 가능.

---

## 백엔드 엔드포인트 현황 (대부분 준비됨)
| 용도 | 엔드포인트 | 응답 | 비고 |
|---|---|---|---|
| 통화 목록(기록) | `GET /calls?limit&offset` | `list[CallSummary]` | call_id, call_date, total_time, summary, rating, character{id,name,image_url} |
| 통화 상세 | `GET /calls/{id}` | `CallDetail` | summary + sentences(+evaluation) |
| 통화 결과 | `GET /calls/{id}/result` | `CallResult` | average + sentences(현재 점수 미포함 → 위 변경) |
| 통화 원본(대화) | `GET /calls/{id}/raw` | `list[RawDataOut]` | content, voice_url, total_time |
| 통화 삭제 | `DELETE /calls/{id}` | 204 | sentences/raw/eval CASCADE |
| 평점 | `PATCH /calls/{id}` | CallSummary | rating 1~3 |
| 북마크 목록 | `GET /members/me/bookmarks` | `list[SentenceOut]` | sentence_id, korean, native, locale, voice_url, is_bookmarked, evaluation |
| 북마크 토글 | `PATCH /sentences/{id}/bookmark` | SentenceOut | body {is_bookmarked} |
| 문장 삭제 | `DELETE /sentences/{id}` | 204 | 소프트 삭제 |
| 문장 복습 | `POST /sentences/{id}/reviews` | ReviewFeedback | 채점(글자별 상중하 + 점수) |
| 복습 이력 | `GET /sentences/{id}/reviews` | list[ReviewOut] | |
| 복습 피드백 | `GET /reviews/{id}/feedback` | ReviewFeedback | |

---

## A. 전화 기록 페이지 (Call History)
**백엔드**: 거의 준비됨. 목록 정렬은 `ix_call_member_date`(member_id, call_date) 최신순.
- (선택) `CallSummary` 에 통화당 평균점수 보여주려면 필드 추가 필요 — 현재 rating 만 있음. 우선순위 낮음(프론트가 상세 진입 시 계산).

**프론트 (Flutter)**:
- 데이터소스: `getCalls(limit, offset)`, `getCallDetail(id)` 또는 `getResult(id)`(점수 포함 변경 후), `deleteCall(id)`.
- DTO: `CallSummaryDto`(+CallCharacterBrief). CallResult/LearnedSentence 는 기존 재사용.
- 화면: 과거 통화 리스트(캐릭터 이미지·날짜·통화시간·한줄요약·평점). 무한스크롤/페이지네이션, 당겨서 새로고침, 스와이프 삭제(`DELETE /calls/{id}`).
- 상세 진입 → 기존 **분석화면(analysis) 재사용**(게이지=프론트 계산 평균 + 문장 리스트). callId 로 result 가져와 바인딩.
- Riverpod: 페이지네이션 리스트 Notifier + 상세 FutureProvider.
- 참고: `analysis.dart` 에서 제외했던 **날짜/통화시간(_MetaRow)** 은 `CallSummary`/`CallDetail` 에 있으니 기록 상세에선 복원 가능.

## B. 문장 보관 페이지 (Bookmarks)
**백엔드**: 준비됨(`GET /members/me/bookmarks`). 단 **페이지네이션 없음**(전체 반환) — 북마크 많아지면 추후 limit/offset 추가 고려.

**프론트 (Flutter)**:
- 데이터소스: `getBookmarks()`, `setBookmark(id, bool)`, `deleteSentence(id)`.
- DTO: `SentenceOut`(korean/native/locale/voice_url/is_bookmarked/evaluation) → 기존 LearnedSentence 확장 또는 신규.
- 화면: 북마크 문장 리스트(한국어+모국어, 점수 배지, voice_url 재생). 북마크 해제 시 목록에서 제거(낙관적 갱신 후 PATCH). 탭 → 해당 문장 복습(`POST /sentences/{id}/reviews`).
- Riverpod: 북마크 리스트 Notifier(토글/삭제 시 invalidate).
- **교차 영향 주의**: 보관함에서 문장 복습하면 그 문장 `evaluation` 이 갱신되고 → **그 통화의 평균에도 반영**됨(같은 Sentence 행). 기록 화면 평균과 일관되게 유지.

---

## 작업 순서(제안)
1. (선행) 백엔드: `CallResultSentence.evaluation` 추가 — 프론트 평균 계산 enable.
2. 기록 페이지: 목록 → 상세(분석화면 재사용) → 삭제.
3. 보관 페이지: 목록 → 토글/삭제 → 복습 연결.
4. 복습 루프에서 게이지 로컬 재계산(문장 채점마다 평균 갱신) 정식 연결.

## 미해결/결정 필요
- 기록 목록에 통화별 점수 표시할지(→ CallSummary 확장) vs 상세에서만.
- 보관함 페이지네이션 필요 시점.
- `CallResult.average` 유지 vs 제거(프론트 계산으로 일원화 시 제거 가능).
- "대화 상세"(transcript) 화면 살릴지 → `GET /calls/{id}/raw` 로 붙일 수 있음.
