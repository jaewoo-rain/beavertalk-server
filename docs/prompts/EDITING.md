# 프롬프트 편집 가이드 — 어디를 고치고, 무엇은 절대 고치지 않나

> 대상: 비버 대사·톤·설명을 다듬으려는 사람(기획·언어 담당). 코드를 몰라도 된다.
> 정본은 코드다(`README.md` §0 — 문서의 문구를 코드에 복사하지 마라). 이 문서는 **길잡이**다.
> 2026-09-12 «잠금 / 편집 분리» — 사장님: 사람들이 톤·설명을 고칠 때 표정·진도·판정 배관이 깨지지 않게.

## 1. 어느 파일을 고치나 — `core/prompts/editable/*.md` 네 개뿐

| 파일 | 통화 | 여기서 고칠 수 있는 것(섹션) |
|---|---|---|
| `normal.md` | 일반 통화 | 페르소나 문단(`persona_intro`) · 규칙 1 모드 분기(`rule1_head` / `rule1_mode_default` / `rule1_mode_checkboard` / `rule1_tail`) · 규칙 3 언어 사용·비율(`rule3`) · 규칙 4 교정 문구(`rule4`) · 밴드별 모국어 발판(`lang_policy_survival/beginner/intermediate/advanced`) · 선톡 시드 말투(`seed_opening`, `seed_opening_lean`) |
| `expression.md` | 표현학습 | 페르소나 문단 · 코스 소개(`rule1`) · 언어 비율 첫 불릿(`rule3_codeswitch`) · 교정(`rule4`) · 드릴 첫 불릿 말투(`drill_intro`) · 선톡(`seed_opening`) |
| `freetalk.md` | 프리토킹 | 옛 경로(`*_old`) 와 차시판 역할극(`*_lesson`) 각각의 페르소나·규칙 1·3·4·선톡 · [이번 차시] 문장(`lesson_header`·`lesson_partner_line`·`lesson_partner_fallback`·`lesson_material_line`·`lesson_probes_prefix`) |
| `leveltest.md` | 레벨테스트 | 시험관 소개 한 줄(`intro`) · 선톡(`seed_opening`) |

각 파일 맨 위 `<!-- -->` 주석에 **편집 규칙 7개**가 있다. 요약:
1. `## 이름` 헤더는 그대로(추가·삭제·개명 금지). 본문만 고친다.
2. `{target}` `{locale_label}` `{username}` `{role}` `{personality}` `{lang_policy}` `{partner}` 같은 **중괄호 슬롯**은 그 섹션에 원래 있던 것을 전부 남기고, 새 슬롯을 만들지 마라. 중괄호를 문장 부호로 쓰지 마라.
3. **금지어를 늘리지 마라**(원래 있던 개수까지만): 마무리 · 정리 · 마지막 · 여기까지 · 종료 · 작별 · 퀴즈 · 테스트 · 채점 · 통화종료 · [시스템].
4. `[` 로 시작하는 줄(대괄호 라벨)을 **새로** 만들지 마라. 원래 있던 `[모국어]` `[페르소나]` 줄은 문장을 고쳐도 된다.
5. 리터럴 예시 대사(«이렇게 말해요», 한국어 예문)를 넣지 마라.
6. 톤 부사(따뜻하게·부드럽게·친절히)를 넣지 마라 — 톤은 캐릭터(role·personality)가 소유한다.
7. 한 줄 늘리면 토큰이 는다. 파일별 예산(근사): normal 3,400 · expression 900 · freetalk 1,900 · leveltest 450.

어기면 어떻게 되나: **서버는 뜬다.** 로더(`core/prompts/editable_loader.py`)가 그 파일을 **버리고** 옆의 `*.default.md`(기본판 스냅샷)로 돌아가며 로그에 `ERROR 편집 프롬프트 … 거절: …` 를 남긴다. 즉 잘못 고치면 통화가 깨지는 게 아니라 **고친 게 반영되지 않는다.**

⚠ 고친 뒤 **`*.default.md` 도 같은 내용으로 갱신**해라(시험 `test_editable_file_equals_its_default_snapshot_at_commit_time` 이 둘이 같아야 통과한다). 기본판은 «지금 문구의 스냅샷» 이라 편집이 확정되면 스냅샷도 그 문구다.

## 2. 절대 고치면 안 되는 것 — `core/prompts/locked/*.py` (gemini 2.5 · 3.1 공통)

여기 있는 문장은 **서버 코드가 그대로 기대한다.** 한 글자만 바뀌어도 판정이 틀리거나 표정이 안 나오거나 통화가 스스로 끊긴다. 파일마다 `⛔⛔ 절대 고치지 마라` 주석이 있고, `tests/test_prompt_locked_hash.py` 가 해시로 잠근다 — 고치면 시험이 죽고 «고치려면 bt-back» 메시지가 뜬다.

| 잠금 파일 | 무엇 | 왜(한 줄) |
|---|---|---|
| `rules.py` | 규칙 2(대화 지속 + 대괄호 안내문 규칙) · 규칙 5·6·7 · `PERSONA_TAIL` · `CONTROL_TAG` `[안내]` / `CLOSE_TAG_DEFAULT` | 대괄호 안내문을 «읽지 말고 따르라» 가 재접지·넛지·퀴즈 큐 전부의 전제. 규칙 6·7 이 «규칙 5» 를 번호로 인용한다(번호 재부여 금지). 종료 태그·안내 태그 접두는 서버 필터·오독 방지 계약(call 683·706) |
| `face.py` | `[표정]` 규칙 블록(`FACE_TOOL_RULE`, **한 상수**) · `set_face` 툴 선언 문구·enum · 캐스케이드 감정 태그/언어 마커 표기 규칙 | 표정은 function-call 로만 나간다 — 규칙 블록과 툴 선언이 **같은 말**(동시성·첫 인사 금지)을 해야 한다. 연구실의 표정 변경은 이 한 자리에서 갈아끼운다 |
| `expression.py` | 규칙 3 둘째·셋째 불릿(새 항목 **먼저 묻기**·착지) · 드릴 절차(정답 공개→따라 말하기·2회 한도·**맞았다고 하지 마라**·**반말은 맞힌 게 아니다**·무음) · `[퀴즈]` 절차(«[안내] 알림이 오면») · `[문형] … — 연습 문장` 항목 렌더 · `[오늘의 표현]` 머리·재료 소진 줄 · `[반응]` · `[3.1 말투]` | 서버 퀴즈 상태기계(T16)가 큐를 열고 닫는다 — 대본이 스스로 퀴즈를 열면 판정이 무너진다. 판정기(quiz_judge)가 격식·정답 공개·예문 OR 을 이 문장대로 판단한다. «재료 소진 = 다음» 은 자체 종료(call 870) 방어 |
| `normal.py` | 공부 체크판 블록(항목 렌더·유형/상태 절차·5개 확인·발설 금지·진행 규칙) · 대화 가이드(유도 규칙) · 최근 이력 · 승급 알림 | 진도 배관(체크판·증거 검출)이 «항목이 어떻게 적히고 어떻게 다뤄지나» 를 전제한다 |
| `leveltest.py` | 측정 절차 본문(언어 규칙·진행·유도 사다리·막힘·대화 지속·응답 길이) · 언어별 사다리 | 서버 밴드 관측·천장 판정이 이 발화 모양을 전제한다(에코 금지·모국어 리액션) |
| `seeds.py` | 무음 넛지 1·2단(일반·레벨테스트·표현학습·프리토킹 옛/차시판) · 이어하기 시드 · 종료 시드 · 표현학습 퀴즈 큐·STT 폴백 판정기 지시 | 접두 태그 계약(`[안내]`/종료 태그) · 조각 재개 규약 · 하네스 로그 계약 |
| `reground.py` | 재접지 쪽지 3종(일반·표현학습·프리토킹) · 이어하기 브리프 · 재접지 사이드카 JSON 지시 · 힌트 사이드카 지시 | 재접지 배관(arm → 마이크 얹기)·사이드카 슬롯(번호·인용)·ServerHint 3예시 형식 |
| `freetalk.py` | [이번 차시] 블록 **구조**(문형 «— "예문"»·표현·어휘 줄·probes 이름 치환·상대 폴백) | 힌트·재접지의 «아직 안 쓴 소재» 대조가 같은 렌더 규칙을 기대한다(문장은 `editable/freetalk.md`) |

정말 잠금 문장을 바꿔야 하면: **bt-back 에게 먼저** — 근거(실측 통화 번호)·바뀌는 배관·시험(해시 갱신 + 해당 회귀)을 한 커밋에 묶고 `README.md §8` 결정 로그에 적는다.

## 3. 고친 뒤 확인

```bash
# 1) 시험 — 편집 검사 + 잠금 해시 + 4대본 스냅샷(일반 94·레벨테스트·표현학습 2.5/3.1·프리토킹 옛/차시판)
PYTHONIOENCODING=utf-8 DATABASE_URL_POOL="postgresql+psycopg2://u:p@localhost:5432/dummy" \
  conda run -n beavertalk-server python -m pytest tests/test_editable_prompts.py tests/test_prompt_locked_hash.py tests/test_prompt_*.py \
  tests/test_persona_prompt.py tests/test_expression_prompt.py tests/test_freetalk_lesson.py -q
```
- 편집 파일을 고쳤으면 **스냅샷 시험은 터지는 게 정상**이다(대본이 바뀌었으니). 그때 확인할 것: 터진 시험이 **내가 고친 코스의 해시 시험만**인가(다른 코스가 같이 터지면 공용 규칙을 건드린 것 — 잠금 위반). 확인됐으면 그 시험의 기준 해시를 갱신하고 `README.md §8` 에 한 줄 적는다.
- `test_editable_prompts.py` 가 터졌으면 편집 규칙 위반이다 — 메시지에 어느 섹션·무엇(슬롯/금지어/대괄호/예산)인지 적혀 있다.

```bash
# 2) 토큰 실측(count_tokens, gemini-2.5-flash) — 표현학습·프리토킹 지시문 길이. GEMINI_API_KEY 필요(.env 는 운영 키 — 읽기만)
conda run -n beavertalk-server python scripts/dev_dump_prompt.py --help   # 대본 전문 뽑기(부록 참조)
```
실측 기준(2026-09-12): 표현학습 18항목 2,782 · 프리토킹 차시판 30항목 1,703(예산 상한 1,600 은 사장님 «그대로 실측» 결정으로 넘김) · 옛 프리토킹 1,341.

## 4. PR · 배포 흐름

1. 브랜치에서 `core/prompts/editable/<코스>.md` 와 같은 내용의 `<코스>.default.md` 를 고친다(잠금 파일·코드는 건드리지 않는다).
2. 위 §3 시험을 돌린다. 스냅샷 해시를 갱신했으면 커밋 메시지에 «대본 변경 — <코스> <무엇을>» 과 실측 통화 번호를 적는다. ⛔ 커밋에 `Co-Authored-By`·`Claude-Session` 트레일러 금지(공개 저장소).
3. PR → bt-back 검수(잠금 위반 여부·README §8 기록) → 하네스 실측(`scripts/e2e_expression_call.py --course …`, 2~3통) → 사장님 실통화 → «배포해».
4. 배포 뒤 Cloud Logging 에서 `편집 프롬프트 … 거절` ERROR 가 **0건**인지 본다 — 있으면 기본판으로 폴백돼 고친 게 반영되지 않은 것이다.

⚠ `.dockerignore`·`.gcloudignore` 가 `*.md` 를 제외하되 `!core/prompts/editable/*.md` 로 이 폴더만 살린다 — 이 예외를 지우면 이미지에 기본판도 없어 기동이 죽는다.
