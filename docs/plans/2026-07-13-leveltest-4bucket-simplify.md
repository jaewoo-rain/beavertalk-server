# 레벨테스트 판정 4버킷 단순화 + 저평가 버그 제거

- 작성일: 2026-07-13
- 상태: **구현·시니어리뷰·테스트 완료 / 실통화 미검증 / 미커밋·미배포**
- 브랜치: `feat/leveltest-fast-probe`
- 관련 파일: `domains/learning/service/normalcall_service.py`, `core/persona_prompt.py`, `tests/test_level_test_call.py`

## 목표 & 범위
통화후 판정관을 13단계 정밀배치에서 **4버킷 택1**로 강등하고, 체계적 저평가(실측 call 257 C급→6, call 256 중급→2)의 기계적 원인을 제거한다. 밴드 판정 기준을 **관찰 가능한 행동**으로 재정의(사장님 확정).

**확정 판정 기준(라이브 분류기·통화후 판정관 공유):**
| 버킷 | 판정 기준(관찰가능) | 예시 | 서수 | → korean_level |
|---|---|---|---|---|
| 입문 survival | 인사·정형청크만 | "안녕하세요","감사합니다" | 0 | 1 |
| 초급 beginner | 단어+구문 — 조사·활용 없는 전보식·사전형 나열 | "김치 좋다","나는 김치다" | 1 | 2 |
| 중급 intermediate | 온전한 문장 — 조사+활용된 종결어미 | "김치를 좋아해요" | 2 | 3 |
| 고급 advanced | 어법 정확 + 복문·긴 담화 | (긴 담화) | 3 | 6 |

핵심 변별: (초급↔중급) 조사·활용된 종결어미를 갖춘 문법적 문장을 만드는가(단어 슬롯채우기 "N 좋다"는 여러 개여도 초급). (중급↔고급) 복문으로 길게 이어 유창하게 말하는가.

**설계 원칙:** 각 버킷을 '확실히 할 수 있는' 밴드 바닥에 보수 배정 → 과배치(강등 불가 plateau) 방지, 부족분은 체크판 자동 레벨업이 단조 상승으로 회복.

**비범위:** level/·docs/learn 무수정. R4 불변식·라이브 조기종료(obs_max·plateau·비화자천장) 무변경. DB/Alembic 무변경(int 컬럼). `leveltest_ladder.py`(死코드) 미삭제.

## 아키텍처 & 데이터 흐름
```
[통화중] classify_leveltest_band(사이드카) → 0..3|None → obs_max/plateau/total 추적
   → _band_ceiling_reached → 조기종료(타이밍 전용, obs_max는 최종배치로 안 흐름)
[통화후] analyze_level_test_call:
   전사 user_chars<20 → done·미저장(재테스트)
   → generate_structured(LevelAssessment 4버킷, temp=0)
   → _place_from_band(dict 룩업): unknown+sufficient→None(failed) / unknown|none→1 /
        sparse→min(bucket,2) / 그외 _BUCKET_LEVEL[band]
   → _save_level_assessment(단일 commit): korean_level·assessed_level·note·summary + grandfathering
```
밴드 정의는 `_BUCKET_DEFINITIONS` 단일 상수를 두 판정기가 공유(정의 불일치가 저평가·정합 버그의 원인이었음).

## 구현 내역
**normalcall_service.py**
- `LevelAssessment`: band Literal에 survival 추가, `level_in_band`·`level_no` **제거**. evidence/reasoning/confidence/sample_quality/summary/feedback 유지.
- `_BUCKET_DEFINITIONS`(공유 4버킷 행동정의) + `_BUCKET_LEVEL{survival:1,beginner:2,intermediate:3,advanced:6}` 신설(구 `_BAND_RANGE` 대체).
- `_leveltest_instruction`: 저평가 규칙("하단 앵커링"·"망설이면 낮게"·"유도실패→강등"·level_no 산술) 삭제, `_BUCKET_DEFINITIONS` 주입, band만 출력. rubric은 참고표로 유지. 표본 1개라도 자발 복문이면 바닥으로 안 깎음.
- `_clamp_assessed_level` → `_place_from_band`(순수 dict 룩업)로 대체. 호출부·로그 갱신.
- `_BAND_CLASSIFY_INSTRUCTION`: 자체 밴드정의 → `_BUCKET_DEFINITIONS` 공유 + "heard_grammar 전사 그대로 복사".
- `_citation_coverage(heard, answer)` 신설: 정규화 후 어절 토큰 겹침 비율. `classify_leveltest_band` 인용검증 강등을 `heard not in answer`(엄격) → `coverage==0.0`(통째 부재)일 때만 -1 로 완화(ASR 왜곡 과잉강등=저평가 방지). 반환계약(int|None,0~3) 불변.

**core/persona_prompt.py**
- `_LEVELTEST_TEMPLATE`에 "실력 끝까지 확인" 개방형 프로브 지시 1줄 추가(초급/중급 변별 — 단답만으로 못하는 사람 넘겨짚기 금지). 일반통화 build_system_instruction 출력 바이트 불변.

## 테스트 결과 (실제 실행)
- **`pytest tests/` → 180 passed / 0 failed** (경고 1: 무관한 StarletteDeprecationWarning).
- `tests/test_level_test_call.py` 재작성(test-engineer): `_assessment` 빌더 4버킷화, `_clamp` 단위테스트 5개 삭제 → `_place_from_band`·`_citation_coverage` 신규 단위테스트로 대체(매핑 전수·sparse 캡·모순→None·ASR드리프트 0.5·부재 0.0), success-saves 갱신.
- `tests/test_normalcall_ws.py` 40 passed(밴드 관측·비화자 조기종료 포함 라이브 회귀 — 서수 의미만 재정의, 로직 불변).
- 오프라인 스모크: `_place_from_band` 매핑·`_citation_coverage`(exact 1.0/drift 0.5/absent 0.0) 실행 확인.

## 시니어 리뷰 (python-expert)
- **blocker 0.** 파괴변경 소비처 누락 없음(LevelAssessment.level_in_band/level_no 라이브 참조 0). `_place_from_band` KeyError 불가(unknown 선필터). `_citation_coverage` 한글 보존·0나눗셈 가드 확인. 라이브 서수-천장 정합. R5 예외흡수 유지.
- minor 처리: M1(stale docstring) 수정, M2(classify 완화 vs judge 엄격 강도차 — 의도 주석 추가). M3(`leveltest_ladder.py` 死코드)은 후속.

## 미해결 / 후속 작업 (TODO)
- **실통화 검증**: 입문/초급/중급/고급 각각 1/2/3/6으로 배정되는지, call 257 재판정이 advanced로 나오는지 = 배포+실통화(사장님 지시시).
- **고급 버킷 폭**: advanced=6이 실제 L6~13을 다 담아 진짜 C급 자가치유가 느림(mastery 분석: 1레벨당 ~30~40통화). 향후 고급을 프로브 1문항으로 세분화(~L6 vs 상위) 여지.
- `leveltest_ladder.py` 死코드 삭제.
- 커밋·배포 미실행.

## 리스크 & 결정 사항
- **결정(사장님)**: 행동 기준 4버킷, 매핑 1/2/3/6(라벨보다 낮은 실제 실력 위치 반영, 과배치 방지 보수 배정).
- **R-A**: 행동정의 판정 정확도·매핑 적정성 미검증 → 실통화. 부정확해도 통화후 판정관+자동레벨업 자가치유(라이브는 조기종료 타이밍 자문).
- obs_max(라이브)는 상향편향이라 최종 배치에서 배제(조기종료 전용). bracket 힌트 TODO 폐기.
