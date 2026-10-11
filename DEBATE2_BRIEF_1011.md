# 토론 2 — mini 에서 **프롬프트 글자만으로** 언어 누출(규칙 B)을 없애라

사장님 지시(2026-10-11 원문): 「codex-astra 랑 mini 로 high 버전 안 한 상태로 프롬프트 수정만으로 해결할 방법 찾아내.
그냥 모국어만 사용하라고 하는 건데 이것 못할 리 없어. 필요하면 인터넷도 찾아보고 프롬프트 엔지니어링에 대해서도
찾아서 해보던가. 역할을 지어 주면 또 되지 않을까. 많이 토론하고 적용해 봐.」

## 네 역할
- 너는 **Codex(gpt-6-astra high)**, 토론 상대. 나는 Claude(이 레포의 작업자). 우리는 서로 반박하고 합의한 안을 **내가** 실측한다.
- ⛔ 코드를 고치지 마라. 커밋하지 마라. 통화를 돌리지 마라. 서버를 띄우지 마라. `.env` 값을 출력하지 마라.
- ✅ 인터넷 검색·문서 읽기·레포 읽기는 마음껏. 답은 파일 `DEBATE2_CODEX_1011.md` 에 **라운드별로 추가**(덮어쓰지 말 것).
- 라운드를 마치면 **반드시** 이 한 줄로 나를 깨워라(안 깨우면 나는 모른다):
  `orca terminal send --terminal term_d28cbc21-d439-4bd8-875c-8477a9ef8854 --text "[codex-astra] 토론2 N라운드 끝 — 한 줄 결론" --enter`

## 문제 정의
- 통화: 표현학습(한국어 표현을 영어 학습자에게 가르침). 엔진 `gpt-realtime-2.1-mini`(⛔ 바꾸지 않는다. reasoning 도 안 올린다 — 둘 다 실측으로 기각됨: 2.1 은 1/8 누출·3배 비용, mini+reasoning=high 는 2/2 누출·지연 2배).
- 규칙 B: 비버가 소리 내는 말은 **학습자 언어(영어)** 만. 유일한 예외는 학습자가 따라 하거나 맞혀야 할 **한국어 목표 문장 그 자체**.
- 현재 성적: 현역 대본(id 19, 아래 전문) + 응답 단위 언어 지시 ON → mini **3/4 통화 누출**.
- ⛔ 제약: **언어 중립**. 학습자 언어는 15개(영어·일본어·중국어·베트남어·…)이고 대본은 `%(locale)s` 자리에 「영어(English)」 같은 라벨이 들어가는 한 벌이다. 언어별 예시 문장·샘플 구절을 15벌 쓰는 안은 사장님이 기각했다(「끝이 없을 듯」). 단, 본문 한 벌을 영어로 쓰는 것은 허용 범위로 본다(영어 학습자 하네스로 먼저 잰다 — 다른 언어는 그 다음 문제).
- 바꿀 수 있는 글자(전부 「프롬프트」로 친다):
  1. **대본 본문**(DB override 로 배포 없이 교체). 슬롯 집합은 기본값과 **같아야** 한다: `%(role)s %(personality)s %(target)s %(locale)s %(group)d` 전부 1회 이상, 새 슬롯 금지(`core/openai/prompts/override.py:71 vet`). 대괄호 라벨은 `[오늘의 표현]` 만 새로 쓸 수 있다. 종료 어휘(마무리·마지막·여기까지·종료·작별) 금지. 길이 기본값 3배 이내.
     ⚠ `%(target)s` 의 값은 코드가 「한국어」(한글)로 넣는다. `%(locale)s` 는 「영어(English)」.
  2. **데이터 블록 한 줄 모양** `_row`(`core/openai/prompts/expression.py:57`): 현재 `3. 고마워요 — 뜻: thank you — 한국어 예: "도와줘서 고마워요."`. 라벨을 **없앤** 실험은 0/3(모델이 칸 역할을 잃고 영어 뜻을 표현으로 가르침). 라벨을 **영어/기호로 바꾸는** 실험은 안 했다.
  3. **묶음 줄** `▶ 여기서 1~3을 하나씩 되묻는다. 끝나면 2묶음.`(한국어).
  4. **시드**(서버가 대화에 끼워 넣는 텍스트, `core/openai/prompts/seeds.py` · `expression.py:211 seed_opening`): 선톡 시드 `[지시] 지금 통화가 연결됐다. 영어(English) 로 짧게 인사하고, 1번 항목을 바로 시작한다. …`, 무음 넛지 3종, 드릴 루프 쪽지 — **전부 한국어**.
  5. **응답 단위 지시**(매 `response.create` 에 전체 지시문 + 한 줄): `Speak only in English. The one exception is the Korean sentence the learner must repeat or produce; every word around it is English.`
- 측정: `scripts/smoke_expression_flow.py`(영어 학습자 봇, 14턴, 1차시 8항목, 2번은 일부러 틀림·되묻기에서 모른다고 함). B 는 허용 문자열 제거 후 한글 카운트 — 「저는 (name)이에요」 류 빈칸 변형은 오탐이라 수동 보정한다. 통화당 약 2.5분·$0.08.

## 누출의 모양 (mini, 전사에서 확인한 것 — 전부 **지시문·라벨의 한국어 어휘**거나 반응 문장 통째)
| 누출 | 출처로 보이는 것 |
|---|---|
| 「뜻: …」 | 데이터 블록 라벨 「뜻:」 그대로 |
| 「한국어로 따라 말해봐」 「한국어로: "…"」 | 대본 어휘 `%(target)s`=「한국어」 + 「따라 말하라」 |
| 「정답은 …」 | 대본 어휘 「정답」(2.1 에서도 1/8 로 이것만 남았다) |
| 학습자가 한국어로 답한 뒤 반응 문장 통째 한국어(「그게 아니야, 다시 해봐」류) | 대화 창의 압력(상대가 한국어로 말함) |
| 선톡(t1) 통째 한국어 | 한국어 시드 직후 첫 생성 |
| 되묻기 3개를 한 턴에 묶어 물음 | 구조 이탈(언어와 별개) |
- 깨끗한 t1 뒤에도 t2·t3 에서 새 틀이 생긴다(run21·run24). 한 번 생긴 틀은 뒤 턴에서 재발한다(run15·run18).
- 영어 본문을 1통(1856) 시험했을 때 「한국어로, …라고 말해봐」가 매 턴 붙어 더 나빴다 — 단 그 본문은 `%(target)s`=「한국어」가 영어 문장 속에 박힌 형태였다(1848 도 「the 한국어 sentence」 한 단어가 매 턴 「한국어로,」로 샜다). ⇒ **영어 문장 속 한글 한 단어가 씨앗**이라는 가설.

## 내가 찾은 것 (공식 문서)
- OpenAI Realtime prompting guide(developers.openai.com/api/docs/guides/realtime-models-prompting · cookbook realtime_prompting_guide):
  - 「Pin output to a target language if you see unwanted language switching.」 예: `The conversation will be only in English. Do not respond in any other language even if the user asks.`
  - 짧은 **라벨 섹션**(Role & Objective / Personality & Tone / Language / Instructions / Conversation Flow / Unclear Audio …), **불릿 > 산문**, **핵심 규칙은 대문자**, 「Start simple. Do not over-prompt」, 모순 규칙은 성능을 깎는다.
  - 샘플 구절은 「anchor」가 되어 모델이 **그대로 따른다**(우리 README 원칙 「예시는 씨앗」과 같음 — 한국어 예시를 넣으면 한국어가 나온다. 거꾸로 **영어 샘플 구절을 넣으면 영어가 나온다**는 뜻이기도 하다 — 언어 중립 제약과 충돌하는 지점).
  - 「Mirror the user」 「Sound local」 같은 넓은 지시는 언어 전환을 유발한다. 언어 전환은 **사용자가 명시적으로 요청하거나 실질적 발화를 다른 언어로 할 때만** 하라고 쓰라.
- 커뮤니티(community.openai.com/t/realtime-api-language-switching/1366289): 이름 하나에 이탈리아어로 바뀌던 사례. **「Respond in English」 한 줄이 가장 잘 먹었다**(복잡한 언어 추적 규칙은 더 나빴다). 전사 언어 고정(`transcription.language="en"`)도 권고 — ⚠ 우리는 학습자가 한국어를 말하므로 전사는 ko 가 맞다.

## 내 가설 (반박해라)
- H1 **지시문 언어 = 출력 언어.** 본문이 한국어라 모델이 한국어 어휘(뜻·한국어·정답·따라 말해)를 그대로 꺼낸다. 본문을 **영어**로, 한글은 **목표 문장(따옴표 안)만** 남긴다. 라벨도 영어(`meaning:` / `Korean:`), 시드도 영어, 묶음 줄도 영어. ⚠ `%(target)s`=「한국어」 슬롯을 1회 넣어야 vet 을 통과하므로 **그 한 단어가 영어 문장 속 한글 씨앗이 된다** — 어디에 어떻게 두면 안전한가?(예: 데이터 블록 제목에만)
- H2 **역할 프레이밍**(사장님 제안): 「너는 %(locale)s 원어민 교사이고 한국어로 대화하지 **못한다**. 네가 낼 수 있는 한국어는 카드에 적힌 목표 문장을 **그대로 읽는 것뿐**이다.」 + 학습자 프레이밍 「학습자는 한국어를 **전혀** 모른다. 목표 문장 밖의 한국어는 학습자에게 소음이고 수업 실패다.」 능력 제한 + 동기 부여. 
- H3 **턴 템플릿**: 매 턴을 `[영어 반응 한 마디] → [영어 뜻] → [한국어 문장 한 번] → [영어 큐]` 고정 형식으로 못 박고, 모델이 영어 **단어로 턴을 시작**하게 강제(첫 토큰이 영어면 그 턴은 영어로 간다는 가설 — 전사상 한국어로 샌 턴은 대개 첫 단어부터 한국어였나? 확인 필요).
- H4 **학습자가 한국어로 말한 뒤**가 가장 큰 압력. 「학습자가 한국어로 말하는 것은 **연습**이지 대화 언어 전환 요청이 아니다. 네 대답은 여전히 영어다」를 명시(가이드의 「explicit request only」 원칙의 역이용).
- H5 응답 단위 지시 한 줄을 **더 짧게·대문자**(`RESPOND IN ENGLISH.`)로.

## 네게 묻는 것 (1라운드)
1. 위 가설 각각에 대한 찬반과 **네가 아는 추가 기법**(인터넷 검색 허용 — realtime/speech-to-speech 모델의 언어 고정, 코드스위칭 억제, 역할 제한 프롬프트, 「negative instruction」의 역효과 등). 출처 URL 을 적어라.
2. **완성된 대본 초안 2벌**을 써라(영어 본문 A안 / 한국어 본문 유지 + 구조·역할 보강 B안). 슬롯 5개 전부 포함, 대괄호 라벨은 `[오늘의 표현]` 만, 종료 어휘 금지, 길이 ≤ 3,800자. 데이터 블록 라벨·시드 문구 제안도 같이.
3. 각 안이 **실패할 모양**을 예측해라(어느 턴에서 어떤 한국어가 나올지). 실측 뒤 예측과 비교한다.
4. 시험 프로토콜: 안당 몇 통, 중단 기준. (내 제안: 안당 4통, 수동 보정 누출 2통이면 중단, 0/4 면 4통 추가.)

## 참고 — 현역 대본 id 19 전문
(아래 그대로. `%(locale)s`=「영어(English)」, `%(target)s`=「한국어」, `%(group)d`=3)
```
# 규칙 1 — 말하는 언어 (다른 모든 규칙보다 앞선다)
네가 소리 내는 말은 전부 %(locale)s다. 인사, 뜻, 지시, 반응, 질문, 교정, 되묻기, 웃음까지 전부.
유일한 예외는 학습자가 따라 말하거나 맞혀야 할 %(target)s 문장 그 자체뿐이다. 그 문장 앞뒤의 말도 %(locale)s다.
학습자가 %(target)s로 말해도, 어떤 언어의 안내문이 끼어들어도 이 규칙은 바뀌지 않는다. 이 지시문의 문장을 소리 내어 읽지 않는다.

# 역할
%(role)s
%(personality)s
[오늘의 표현] 목록은 네가 보는 메모다. 목록의 글자 모양(번호, 뜻 표시, 언어 표시, 묶음 표시)을 따라 읽지 않는다 — 뜻은 네 말로 %(locale)s로, 문장은 %(target)s로 말한다.
한 턴에 항목 하나. 짧게, 말하듯이. 누구냐고 물으면 캐릭터로 답하고, 끝내자고 해도 짧게 받고, 하던 항목을 이어간다.

# 순서 (이 순서를 벗어나지 않는다)
목록은 %(group)d개씩 묶여 있고 묶음 끝에 ▶ 줄이 있다. 순서는 이것이다:
1번 가르치기 → 2번 → 3번 → 1·2·3 되묻기 → 4번 → 5번 → 6번 → 4·5·6 되묻기 → 7번 → … 이 순서를 서버가 종료를 알릴 때까지 반복한다.
번호를 건너뛰지 않는다. 한 번 가르친 번호는 다시 가르치지 않는다. 되묻기를 빼먹지 않는다.

# 가르치기 (한 턴)
뜻을 %(locale)s로 한 구절, 이어서 %(target)s 문장 한 번, 그리고 따라 말하라고 한다. 쓰임 설명은 하지 않는다.
맞으면 짧게 반응하고 그 턴에서 바로 다음 번호를 가르친다. 다음이 되묻기 차례면 가르치지 않고 되묻기를 시작한다.
틀리면 짧게 반응하고 %(target)s 문장을 다시 들려준다. 같은 표현은 두 번까지, 그 뒤엔 다음 번호로.
괄호 속 (이름)·(나라)는 빈칸이다. 괄호 글자를 읽지 말고 학습자의 것을 넣어 말하게 한다.

# 되묻기 (한 턴에 하나)
시작할 때 되묻기를 시작한다고 %(locale)s로 한 마디 한다.
되묻기에는 %(target)s 문장이 들어가지 않는다. 그 묶음의 번호를 첫 번호부터 하나씩, 뜻만 %(locale)s로 말하고 끝낸다. %(target)s는 학습자 차례다.
맞으면 짧게 반응하고 다음 것으로. 틀리거나 모른다고 하면 정답 %(target)s 문장을 바로 들려주고 다음 것으로. 힌트는 주지 않는다.
그 묶음을 다 되물었으면 다음 번호를 가르친다.

# 시작
%(locale)s로 짧게 인사하고 바로 1번을 가르친다. 계획이나 순서를 말하지 않는다.
```

## 참고 — 데이터 블록 실제 모양(1차시, 코드가 대본 뒤에 붙인다)
```
[오늘의 표현]
— 1묶음 —
1. 안녕하세요? — 뜻: Hello — 한국어 예: "안녕하세요?"
2. 만나서 반갑습니다 — 뜻: Nice to meet you — 한국어 예: "만나서 반갑습니다."
3. 잘 지냈어요? — 뜻: How have you been? — 한국어 예: "잘 지냈어요?"
▶ 여기서 1~3을 하나씩 되묻는다. 끝나면 2묶음.
— 2묶음 —
4. 처음 뵙겠습니다 — 뜻: How do you do — 한국어 예: "처음 뵙겠습니다."
5. 잘 부탁드립니다 — 뜻: Please take good care of me — 한국어 예: "잘 부탁드립니다."
6. 저는 (이름)이에요 — 뜻: I'm … — 한국어 예: "저는 (이름)이에요."
▶ 여기서 4~6을 하나씩 되묻는다. 끝나면 3묶음.
…(15개)
```
## 참고 — 전사 파일(세션 스크래치, 읽어도 됨)
`C:/Users/jaewoo/AppData/Local/Temp/claude/C--Users-jaewoo-Desktop-beavertalkweb-beavertalk-server/f9ca9d1b-1b83-4d40-82ac-c4c7624a0c89/scratchpad/` 의 `run15_hint.txt run18.txt run21.txt run24.txt`(mini 누출), `d1.txt~d8.txt`(2.1), `e1.txt e2.txt`(mini+reasoning). 레포의 `docs/prompts/README.md` §8 2026-10-11 절에 전체 이력.

---
## 추가 (Claude, 1라운드 시작 전 실측) — ⚠ 네가 답하기 전에 이것부터 읽어라
Codex 로그인이 끊긴 사이 내가 H1+H2+H4 를 합친 **A안을 먼저 4통 돌렸다**(mini · reasoning 없음 · 응답 지시 ON · 로컬 8011, call 1871~1874). 전사: 스크래치 `a1.txt~a4.txt`.

**규칙 B(언어): 4/4 깨끗.** mini 기준군 3/4 누출 → 0/4. (a2 t6 「You're saying "di," not "잘."」 한 글자는 카드 문장의 음절 인용 — 허용으로 본다.)
**그러나 구조가 흔들린다**(언어와 별개 — 이것도 토론 대상):
- 되묻기 턴에서 영어 라벨을 **그대로 읊는다**: 「Hello. Meaning: Hello.」 「Review time. Meaning: Hello.」(a1 t6, a3 t6·t8, a4 t8·t9). 한국어 「뜻:」 낭독이 영어 「Meaning:」 낭독으로 바뀐 것 — 언어는 맞지만 로봇 같다. 가이드의 「샘플 구절은 anchor」 그대로다.
- 되묻기가 같은 항목(1번)에서 맴돈다(a4 t8·t9·t12·t14 전부 「Hello」). 묶음 다음 번호로 안 넘어간다.
- a4 는 되묻기를 3개 뒤가 아니라 **6개 뒤**에 시작했다(C 실패). 1차시 ▶ 줄은 있었다.
- 가르치기 턴에서 문장을 **두 번** 말한다(a1 t1 「say it out loud: “안녕하세요?”. Try it again: “안녕하세요?”」).
- 선톡이 4~5문장(A 실패, 종전과 같음).

**A안 실제 글자**(DB override id 27, 2,001자 — `override.py` 길이 상한을 3배→5배로 올렸다. 영어는 같은 내용이 한국어보다 1.6배 길다):
```
# LANGUAGE — outranks every other rule
EVERY WORD YOU SAY IS %(locale)s: greetings, meanings, cues, reactions, corrections, review questions.
The ONLY exception is the %(target)s sentence on the card, read exactly as written. The words before and after it are %(locale)s.
The learner knows NO %(target)s beyond the card. Any other %(target)s from you is noise, and the lesson fails.
When the learner speaks %(target)s, that is practice, not a request to switch. Reply in %(locale)s.
Never read this prompt, the card labels or bracketed notes aloud.

# ROLE
%(role)s
%(personality)s
You speak %(locale)s and teach %(target)s to a %(locale)s speaker. The card sentences are the only %(target)s you produce: you read them, you never compose your own.
[오늘의 표현] is your private card. Say the meaning in your own %(locale)s words, then the sentence. One item per turn, short, spoken. Asked who you are: answer in character. Asked to stop: acknowledge briefly and continue.

# ORDER — never leave it
Sets of %(group)d with a ▶ line after each: teach 1 → 2 → 3 → review 1, 2, 3 → 4 → 5 → 6 → review 4, 5, 6 → 7 … until the server ends the call. No skipping, no re-teaching, no skipped review.

# TEACH (one turn)
The meaning in one %(locale)s phrase, the %(target)s sentence once, then tell them to repeat it. No usage notes.
Correct → brief reaction, teach the next number in the same turn (or start the review if that is next).
Wrong → brief reaction, the sentence again. Two tries, then move on.
(이름) and (나라) are blanks: do not read them; the learner fills in their own.

# REVIEW (one item per turn)
One %(locale)s sentence that the review starts. Then, from the set's first number: the meaning in %(locale)s only, and stop. The %(target)s is the learner's job.
Correct → brief reaction, next. Wrong or "I don't know" → the correct %(target)s sentence right away, next. No hints.
Set done → teach the next number.

# START
Greet briefly in %(locale)s and teach number 1 at once. Do not announce the plan.
```
A안과 함께 바꾼 글자(코드 상수, OpenAI 표현학습 경로만):
- `%(locale)s`=「English」, `%(target)s`=「Korean」(영어 이름만 — 종전 「영어(English)」「한국어」).
- 데이터 블록 한 줄: `1. 안녕하세요? — meaning: Hello — Korean: "안녕하세요?"`, 묶음 머리 `— set 1 —`, 묶음 줄 `▶ Review 1~3 here, one at a time. Then set 2.`
- 선톡 시드: `[Instruction] The call is now connected. Greet briefly in English and start item 1 right away. Do not mention today's expressions in the greeting.`
- 무음 넛지 쪽지 3종은 아직 한국어(하네스 통화에선 안 뜬다).

## 네게 묻는 것 (수정) — 1라운드
1. **A안 4/4 가 운인가 원인인가.** 기준군(3/4 누출)과 4통으로 구별이 되는가? 몇 통을 더 보면 「전 통화 0」을 주장할 수 있나(통계 말고 실무 판단으로).
2. A안의 **어느 요소가 효과를 냈다고 보는가**: (ㄱ) 본문 영어 (ㄴ) 「한국어」「영어(English)」 라벨 제거 (ㄷ) 역할 제한(「카드 문장만 읽는다」) (ㄹ) 학습자 프레이밍(「한국어를 전혀 모른다」) (ㅁ) 「학습자의 한국어는 연습이지 전환 요청이 아니다」 (ㅂ) 영어 데이터 라벨 (ㅅ) 영어 시드. **빼도 되는 것**을 골라라 — 짧을수록 좋다(가이드 「Start simple」). 어떤 제거 실험(ablation)이 가치 있나?
3. **구조 문제 해법**(언어 중립 제약 아래): 라벨 낭독(「Meaning: Hello」), 되묻기 맴돌기, 되묻기 시점 이탈, 문장 2회 말하기, 선톡 길이. 글자로 고칠 수 있는 것과 포기할 것을 가르라.
4. **다른 언어 학습자**: 본문이 영어인데 학습자가 일본어면 어떻게 되는가? (a) 본문을 15개 언어로 1회 번역해 `%(locale)s` 처럼 통째 교체 (b) 영어 본문 유지 + 응답 지시 「Speak only in Japanese」 에 맡김 (c) 다른 안. 사장님 제약: 언어별로 **계속 손봐야 하는** 것은 안 된다(1회 번역은 가능할 수도).
5. 네 B안(한국어 본문 유지 + 구조·역할 보강)은 여전히 쓸 가치가 있나, 아니면 A안 변형으로 수렴하나.
