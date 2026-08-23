# -*- coding: utf-8 -*-
"""Live setup 분할 주입의 **순수 계약** 회귀 (2026-08-23).

⛔ 왜 이 파일이 있나 — `gemini-2.5-flash-native-audio-preview-09-2025`(AI Studio)는
  **긴 system_instruction 과 function tool 이 같은 setup 페이로드에 있으면 100% 1011 로
  죽는다.** 같은 시간대 라운드로빈 실측:
      지시문 5,057자 + set_face 를 setup 에 전부  → 0/8
      코어 46자 + 툴 → 붙은 뒤 나머지를 통화 중 주입 → 14/14 (무음턴 0/70)
      긴 지시문 + 툴 **없음**                      → 7/8
  벤더 티켓: googleapis/python-genai#1832 (2025-12-08 open, 워크어라운드 없음).

여기서 지키는 계약은 셋이다.
  ① setup 코어가 실측 상한(LIVE_SETUP_MAX_CHARS) 이하 — 넘으면 1011 구간으로 들어간다
  ② 조각이 원문의 **순수 슬라이스** — 한 글자도 잃지 않는다
  ③ 코어에 "잃으면 사고 나는 것"이 들어 있다 — 종료 규약·낭독 금지

통화 배관(훅 시점·가드·실패 흡수)은 tests/test_normalcall_ws.py 가 맡는다.
"""
import pytest

from core import persona_prompt as pp


_BASE_KW = dict(
    role="한국어를 가르치는 다정한 선생님",
    personality="밝고 칭찬을 아끼지 않는다",
    level_profile="왕초보. 아주 쉬운 말로 천천히.",
    locale="en",
    interests=["여행", "음식"],
    name="jaewoo",
    lang_band="beginner",
)

# 실서비스에 가까운 최악 케이스 — 체크판 항목·기지 항목·최근 소재·이력·승급까지 전부 채운다.
_HEAVY_KW = dict(
    _BASE_KW,
    study_items=[
        {"no": i + 1, "text": f"항목{i + 1}", "kind": "어휘", "state": "처음",
         "meaning": f"meaning {i + 1}"}
        for i in range(10)
    ],
    known_items={"expressions": ["안녕하세요", "감사합니다"]},
    recent_topics=["여행", "음식"],
    promotion_notice=True,
    face_tool=True,
)


# --------------------------------------------------------------------------- #
# ① setup 코어 예산
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("locale", sorted(pp._LOCALE_LABEL))
@pytest.mark.parametrize("face_tool", [False, True])
def test_the_setup_core_stays_under_the_measured_cap(locale, face_tool):
    """코어가 상한을 넘으면 1011 구간으로 들어간다 — 로케일 전수 × 표정 on/off.

    ⛔ 상한 380자는 취향이 아니라 **실측**이다(라운드로빈 9바퀴, 선톡 시드 234자 동반):
        46자 8/9 · 158자 8/9 · **380자 9/9** · 494자 6/9 · 788자 2/9
      500자를 넘으면 급격히 무너진다.
    ⚠ 로케일 전수인 이유: 모국어 라벨이 코어에 들어가고, 최장 라벨
      `인도네시아어(Bahasa Indonesia)`(24자)에서만 터진 전례가 있다(구현 중 실측).
    """
    core = pp.build_setup_core(locale=locale, name="jaewoo", face_tool=face_tool)
    assert len(core) <= pp.LIVE_SETUP_MAX_CHARS, (
        f"setup 코어 {len(core)}자 > 상한 {pp.LIVE_SETUP_MAX_CHARS} "
        f"(locale={locale}, face_tool={face_tool}) — 1011 위험"
    )


def test_a_long_learner_name_does_not_blow_the_budget():
    """이름은 사용자 입력이다 — 긴 이름이 예산을 뚫으면 그 회원만 통화가 안 된다."""
    core = pp.build_setup_core(
        locale="id", name="가" * 40, target_language="한국어", face_tool=True
    )
    assert len(core) <= pp.LIVE_SETUP_MAX_CHARS + 40, (
        "이름 길이가 예산에 선형으로 실린다 — 코어에서 이름을 잘라야 한다"
    )


# --------------------------------------------------------------------------- #
# ② 조각 왕복 — 아무것도 잃지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kw", [_BASE_KW, dict(_BASE_KW, face_tool=True), _HEAVY_KW],
                         ids=["base", "face", "heavy"])
def test_every_byte_of_the_persona_survives_the_split(kw):
    """⭐ 이게 test_persona_prompt.py 의 바이트 스냅샷과 잇는 다리다.

    스냅샷이 `build_system_instruction` 출력을 얼리고, 이 시험이 "얼린 그것이 통째로
    배달된다"를 잇는다. 둘이 함께 있어야 "분할했더니 규칙이 사라졌다"가 막힌다.
    """
    full = pp.build_system_instruction(**kw)
    chunks = pp.split_persona_for_injection(full)
    assert "".join(chunks) == full, "조각을 합쳐도 원문이 아니다 — 지시문이 유실됐다"


@pytest.mark.parametrize("kw", [_BASE_KW, _HEAVY_KW], ids=["base", "heavy"])
def test_no_chunk_is_empty(kw):
    """빈 조각은 주입 턴 하나를 공짜로 태운다(그리고 왕복 단언을 무의미하게 만든다)."""
    chunks = pp.split_persona_for_injection(pp.build_system_instruction(**kw))
    assert chunks, "조각이 하나도 없다"
    assert all(c.strip() for c in chunks), "빈 조각이 섞였다"


def test_chunks_are_cut_on_line_boundaries():
    """⛔ 문자 수로 아무 데나 자르면 규칙 한복판이 잘린다.

    불변 규칙 블록 하나가 2,513자로 전체의 68%라 그럴 여지가 크다 — 반쪽 규칙을 한 턴 동안
    들고 있는 상태를 만들면 안 된다.
    """
    full = pp.build_system_instruction(**dict(_BASE_KW, face_tool=True))
    chunks = pp.split_persona_for_injection(full)
    for c in chunks[:-1]:
        assert c.endswith("\n"), f"조각이 줄 중간에서 끊겼다: ...{c[-40:]!r}"


def test_an_empty_instruction_yields_no_chunks():
    assert pp.split_persona_for_injection("") == []


# --------------------------------------------------------------------------- #
# ③ 코어에 반드시 남아야 하는 것
# --------------------------------------------------------------------------- #
def test_the_core_carries_the_no_readout_rule():
    """⛔ 낭독 금지가 코어에 없으면, 뒤이어 오는 주입문·넛지·종료 시드를 전부 소리 내어 읽는다.

    native-audio 는 모델이 직접 소리를 내므로 **오디오를 되돌릴 수 없다** — 서버 필터
    (`_CONTROL_TAG_RE`)는 저장본 전사만 고친다. 첫 조각이 도착하기 전에 이미 걸려 있어야 한다.
    """
    core = pp.build_setup_core(locale="en", name="jaewoo")
    assert "대괄호" in core and "읽" in core


def test_the_core_carries_the_keep_talking_rule():
    """⛔⛔ 종료 규약이 코어에 없으면 압축 후 비버가 먼저 작별한다.

    압축(sliding window)은 `system_instruction` 만 면제하고 대화 히스토리는 오래된 것부터
    밀어낸다. 주입분은 턴1~2라 **1순위 희생자**다. 실측 사고: call 706 47초 死구간,
    call 870 4분24초 자체종료, 5분 12건 중 3건이 종료 신호보다 4~16턴 먼저 마무리.
    """
    core = pp.build_setup_core(locale="en", name="jaewoo")
    assert "이어가라" in core, "대화 지속 지시가 코어에서 빠졌다"
    assert "수업 재료" in core, (
        "'가르치는 것 ≠ 끝내는 것' 구분이 빠졌다 — L1 청크의 '안녕히 가세요'를 가르치다가 "
        "실제 작별로 미끄러진다(15분 실측)"
    )


def test_the_core_never_leaks_a_close_tag():
    """⛔ 제어 태그 2종을 섞지 마라(docs/20260727_1710). 코어에 종료 태그가 있으면 비버가
    그걸 종료 신호로 오독한다."""
    core = pp.build_setup_core(locale="en", name="jaewoo", face_tool=True)
    assert pp.CLOSE_TAG_DEFAULT not in core
    assert "[통화종료]" not in core


def test_the_core_uses_forward_instructions_not_forbidden_examples():
    """⛔ 부정 지시 + 금지 예시를 쓰지 마라 — 모델이 그 예시를 그대로 뱉는다.

    실측: 옛 문구가 "'슬슬 끊자' 등 금지" 였는데 비버가 "슬슬 마무리할 시간이다"를 뱉었다
    (call 782). 그래서 전진 지시로만 쓴다.
    """
    core = pp.build_setup_core(locale="en", name="jaewoo", face_tool=True)
    for word in ("슬슬", "마지막으로", "여기까지", "안녕히 가세요"):
        assert word not in core, f"코어에 종료 어휘가 씨앗으로 들어 있다: {word}"


def test_the_face_rule_appears_only_when_the_tool_is_on():
    """⛔ 옵트인이다. 툴이 없는데 set_face 를 말하면 모델이 없는 함수를 찾는다."""
    off = pp.build_setup_core(locale="en", name="jaewoo", face_tool=False)
    on = pp.build_setup_core(locale="en", name="jaewoo", face_tool=True)
    assert "set_face" not in off
    assert "set_face" in on
    assert on.startswith(off), "표정 규칙은 코어 뒤에 덧붙기만 해야 한다"
