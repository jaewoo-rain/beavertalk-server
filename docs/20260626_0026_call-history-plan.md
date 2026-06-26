# 전화 기록 조회 구현 플랜 (Flutter)

작성 2026-06-26. 통화 파트(속도/에코/오디오/전사) 완료 후 다음 작업. 프론트는 test 서버 사용.

## 한 줄 요약
**전화 기록 화면 UI(`record_list.dart`)는 이미 있고 전부 목업.** 백엔드는 필요한 엔드포인트가 다 있어 **서버 필수 변경 없음.** Flutter에서 데이터 연결만 하면 됨.

## 백엔드 현황 (준비됨 — 필수 변경 X)
| 용도 | 엔드포인트 | 응답 |
|---|---|---|
| 기록 목록 | `GET /calls?limit&offset` | `list[CallSummary]`: call_id, call_date, total_time, summary, rating, character{character_id,name,image_url} |
| 상세 | `GET /calls/{id}/result` | `CallResult`: call_id, summary, rating, **average**(저장된 평균), sentences[] |
| (대안 상세) | `GET /calls/{id}` | `CallDetail`: + 문장별 evaluation 포함 |
| 원본 대화 | `GET /calls/{id}/raw` | content/voice_url/total_time |
| 삭제 | `DELETE /calls/{id}` | 204 (sentences/raw/eval CASCADE) |

- 정렬: `ix_call_member_date`(member_id, call_date) 최신순.
- **선택 변경**: 목록 카드에 점수 배지 원하면 `CallSummary`에 average 추가(현재 rating만). 안 하면 스킵.

## Flutter 현황
- `lib/screens/record/record_list.dart` — 기록 목록, **목업**(`_records` 하드코딩). 탭 → `Routes.analysis`로 **callId 없이** 이동(현재는 빈/깨짐).
- `lib/screens/record/record_empty.dart` — 빈 상태.
- `lib/screens/record/record_archive.dart` — 문장 보관(북마크, 별도 작업).
- 데이터소스(`normalcall_remote_data_source.dart`): `submitRating`/`getStatus`/`getResult`만 있음 → **목록/삭제 없음.**

## Flutter 구현 작업
1. **DTO**: `CallSummaryDto`(+ `CallCharacterBriefDto`: id/name/image_url) + `toEntity`. fromJson snake_case.
2. **데이터소스**: 추가
   - `listCalls(limit, offset) -> List<CallSummaryDto>` → `GET /calls?limit&offset`
   - `deleteCall(id)` → `DELETE /calls/{id}`
   - (상세는 기존 `getResult(id)` 재사용)
3. **리포지토리/프로바이더**: 목록용 Riverpod(`FutureProvider`/Notifier, 페이지네이션·새로고침). `mapDioException`.
4. **record_list.dart 연결**:
   - 목업 `_records` 제거 → 프로바이더의 `List<CallSummary>` 바인딩.
   - 카드: 캐릭터 이미지(`image_url`, 네트워크), 제목(캐릭터명), 부제(summary), 메타(call_date 포맷·total_time 포맷).
   - 비었으면 `record_empty` 표시.
   - 탭 → `getResult(callId)` 로딩 → `Routes.analysis`에 **CallResult 전달**(기존 분석화면 재사용). (로딩 인디케이터 또는 analysis_loading 패턴 재사용)
   - (선택) 스와이프 삭제 → `deleteCall` + 낙관적 목록 갱신.
5. **상세(분석화면) 분기 — 중요**:
   - 라이브(방금 통화): 게이지 = `reviewScoresProvider`(프론트 계산, 세션).
   - **히스토리(과거 통화)**: 세션 점수가 없으므로 **백엔드 `CallResult.average` 사용**해서 게이지 표시. 분석화면이 "히스토리 모드"면 average를 직접 쓰도록 분기(서버 변경 불필요 — average 이미 옴).
6. **날짜/시간 포맷**: call_date → `YYYY.MM.DD`, total_time(초) → `N분 N초`.

## 작업 순서
1. DTO + 데이터소스(listCalls/deleteCall) + 프로바이더
2. record_list 실데이터 바인딩 + 빈상태 + 탭→상세
3. (선택) 삭제, 점수배지(백엔드 CallSummary 확장 시)
4. 검증: 통화 여러 건 만든 뒤 목록·정렬·상세·삭제 동작, `flutter analyze`

## 다음(별도)
- 문장 보관(`record_archive`) = 북마크: `GET /members/me/bookmarks` 등 — 기록 끝나고.

## 결정 필요
- 목록에 통화별 **점수 배지** 넣을까요? (넣으면 백엔드 `CallSummary`에 average 추가 — 소규모)
- 상세는 **분석화면 재사용**(average 기반)으로 OK? (추천)
