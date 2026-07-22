"""레벨테스트 "서버 주도 적응형 사다리" — 데이터 + 분기 로직 모듈(순수 파이썬).

이 모듈은 레벨테스트 통화 중 비버가 다음에 무엇을 물을지 결정하는 **6계단 노드 트리**
를 상태 없는 그래프로 인코딩한다. 통화 엔진은 이 모듈의 계약(root_id / node /
pick_question / target_desc / advance / band_and_anchor)만 의존하고, "지금 어느 노드인가"
라는 상태만 들고 다니면 된다(분기 규칙은 전부 그래프 구조에 박혀 있음).

설계 정합(반드시 유지):
- 6계단 목표 자질은 ``domains/learning/service/normalcall_service._leveltest_instruction``
  의 ``[계단별 목표 자질]`` 블록과 1:1 대응한다.
    N0 생존·현재(-아요/어요+조사) / N1 과거(-았/었-) / N2 계획+이유(-(으)ㄹ 거예요+이유절)
    / N3 간접화법(-다고/-대) / N4 대조·추측 복문(-(으)ㄴ 반면/-나 보다) / N5 추상논증(-(으)므로).
- band / anchor 는 같은 파일의 ``_BAND_RANGE`` 와 정합한다:
    survival→1 · beginner(2~5) · intermediate(6~9) · advanced(10~13).
  앵커는 밴드 하단 기본배치를 따른다(beginner 기본 3 등) — 사이드카 판정기가
  이 앵커를 강한 사전확률로 받아 미세 조정한다(앵커 = 코드가 심판한 천장, 관통 원칙 1).

판정 자체(pass/fail/unclear)는 이 모듈이 하지 않는다 — 사이드카 판정기(LLM+인용검증)가
``target_desc`` 를 기준으로 verdict 를 만들고, 이 모듈은 그 verdict 로 다음 노드만 고른다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 상수
# --------------------------------------------------------------------------- #
_VERDICTS = ("pass", "fail", "unclear")

# {N} 치환용 기본 명사(interests 가 비었을 때). question_bank 영어 소스에 끼워도
# 자연스럽도록 일반 명사 사용.
_DEFAULT_NOUN = "food"

_STAGE_COUNT = 6  # N0 ~ N5

# 통과 노드의 stage → (band, anchor_level). 교차확인 노드(N{k}e)도 같은 stage 이므로
# 같은 앵커로 매핑된다(교차확인 통과 = 그 목표 확정).
# 앵커는 _BAND_RANGE(beginner 2~5 / intermediate 6~9 / advanced 10~13) 안에서
# 밴드 하단 기본배치. survival 은 밴드 밖 특수 배정(레벨 1).
_STAGE_ANCHOR: dict[int, tuple[str, int]] = {
    0: ("beginner", 2),      # 현재형만 — 초급 바닥
    1: ("beginner", 3),      # 과거까지 — 초급 기본배치
    2: ("beginner", 4),      # 계획+이유 — 초급 상단
    3: ("intermediate", 6),  # 간접화법 — 중급 진입
    4: ("intermediate", 8),  # 대조·추측 복문 — 중급 상단
    5: ("advanced", 10),     # 추상논증 — 고급 진입
}
_SURVIVAL_ANCHOR: tuple[str, int] = ("survival", 1)


# --------------------------------------------------------------------------- #
# 노드
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LadderNode:
    """사다리 노드 1개. 상태는 없고 '이 노드가 재는 목표'만 담는다.

    node_id     : "N0"~"N5"(주 노드) / "N0e"~"N5e"(교차확인 노드).
    stage       : 0~5. 교차확인 노드는 부모와 같은 stage(같은 목표 자질).
    target_desc : 사이드카 판정기가 그대로 쓰는 pass 기준(한국어, 이분법적).
    question_bank : 같은 목표·같은 난이도의 영어 소스 변형 질문 5개({N} 명사 슬롯 허용).
    korean_cue  : "한국어로 말해 볼래요?" 류 영어 소스(비버가 학습자 locale 로 렌더).
    """

    node_id: str
    stage: int
    target_desc: str
    question_bank: list[str]
    korean_cue: str


def _cross_desc(stage_label: str, base: str) -> str:
    """교차확인 노드 target_desc — 부모 기준 + '다른 화제로 한 번 더(암기·우연 배제)' 주석."""
    return (
        f"[교차확인 · {stage_label}] 앞서 애매했던(또는 실패한) 같은 목표를 화제·어휘만 "
        f"바꿔 한 번 더 잰다. 통째로 외운 청크나 우연한 1회 성공을 배제하기 위한 확인이므로, "
        f"앞 발화와 '다른 어휘·다른 상황'에서 아래 기준을 다시 충족해야 pass 다.\n" + base
    )


# 주 노드 목표 기준(target_desc) — normalcall_service 의 계단별 목표 자질과 1:1.
_N0_DESC = (
    "현재 시제 종결어미 '-아요/어요'(또는 '-이에요/예요', '있어요/없어요')와 조사(을/를·에·"
    "에서)를 사용해 현재의 상태나 행동을 스스로 한 문장 이상 말했는가. "
    "예: '저는 서울에 살아요', '커피를 좋아해요'. "
    "인사·자기소개 정형 청크('안녕하세요', '만나서 반갑습니다', '저는 ○○이에요'만)나 "
    "단어·명사 나열은 pass 아님 — 서술어가 현재형으로 실제 활용되어야 pass."
)
_N1_DESC = (
    "과거 시제 선어말어미 '-았/었-'으로 어제·최근에 실제로 한 일을 스스로 한 문장으로 "
    "서술했는가. 예: '어제 김치찌개를 먹었어요', '주말에 친구를 만났어요'. "
    "현재형으로만 답하거나('먹어요') 동사 없이 명사만 말하면 pass 아님 — "
    "과거형 활용이 실제로 나와야 pass."
)
_N2_DESC = (
    "미래·의도 표현('-(으)ㄹ 거예요/-(으)ㄹ게요/-고 싶다')으로 앞으로 할 일을 말하고, "
    "거기에 이유절('-아서/어서·-(으)니까·왜냐하면 ~')을 붙여 이유까지 함께 말했는가. "
    "예: '내일 비가 와서 집에 있을 거예요'. "
    "계획만 말하고 이유가 없으면 부분 정답(pass 아님) — 미래·의도 표현과 이유 연결이 "
    "한 발화 안에 모두 있어야 pass."
)
_N3_DESC = (
    "간접화법('-다고/-냐고/-자고/-(으)라고 하다', 축약 '-대(요)')로 남이 한 말이나 자기 "
    "말을 옮겨 전달했는가. 예: '친구가 내일 온다고 했어요', '엄마가 일찍 자라고 했어요'. "
    "직접 인용을 그대로 되풀이하거나('친구가 \"가자\" 했어요') 간접화법 어미가 없으면 "
    "pass 아님 — 인용 어미가 실제로 활용되어야 pass."
)
_N4_DESC = (
    "대조 연결('-(으)ㄴ 반면/-지만/-는데')이나 근거 있는 추측('-나 보다/-는 것 같다')을 "
    "사용해 두 가지를 견주거나 추측을 복문으로 말했는가. "
    "예: '서울은 복잡한 반면 부산은 여유로운 것 같아요'. "
    "단문 나열('A 좋아요. B 싫어요')은 pass 아님 — 대조·추측의 연결어미가 한 복문 안에서 "
    "두 절을 실제로 이어야 pass."
)
_N5_DESC = (
    "문어·논리 연결('-기 때문에/-(으)므로/-(으)ㄴ 반면/N에 따르면/N(으)로 인해')로 추상적·"
    "사회적 주제에 자기 의견과 근거를 논리적으로 전개했는가. "
    "예: '온라인 수업은 시간을 아낄 수 있으므로 효율적이라고 생각해요'. "
    "단순 찬반 한 마디('좋아요/나빠요')나 근거 없는 의견은 pass 아님 — "
    "주장과 근거가 논리 연결어미로 묶여 한 편의 짧은 논증이 되어야 pass."
)

# 주 노드 질문 뱅크(영어 소스, 5개 · 같은 목표/난이도 · 화제·명사만 다름).
_N0_BANK = [
    "Where do you live these days?",
    "What kind of {N} do you like?",
    "What do you usually do on weekends?",
    "Do you work or study these days? Tell me about it.",
    "What is your favorite {N}, and what is it like?",
]
_N1_BANK = [
    "What did you do yesterday?",
    "What did you eat this morning?",
    "Where did you go last weekend?",
    "Who did you meet recently?",
    "What did you watch or play lately?",
]
_N2_BANK = [
    "What are you going to do this weekend, and why?",
    "What do you want to do next vacation, and why there?",
    "Is there any {N} you want to try soon? Why?",
    "What are your plans for tonight, and why?",
    "What do you want to learn next, and why that?",
]
_N3_BANK = [
    "Your friend told you something recently — what did they say?",
    "What did your family tell you to do this week?",
    "Did anyone give you advice lately? What did they say?",
    "What did your coworker or classmate ask you recently?",
    "Tell me about {N} someone recommended to you — what did they say about it?",
]
_N4_BANK = [
    "Compare living in a big city and in the countryside — which do you prefer and why?",
    "How is your hometown different from where you live now?",
    "It looks like the weather is changing — what do you guess is happening?",
    "Compare two {N} you know — how are they different?",
    "Your friend didn't answer your call — what do you guess is going on?",
]
_N5_BANK = [
    "Do you think working from home is a good idea? Give your reasons.",
    "Should students use AI for studying? Why or why not?",
    "What do you think about social media's effect on {N}? Explain your view.",
    "Is it better to save money or to spend it on experiences? Argue your case.",
    "Do you think cities should have fewer cars? Explain your reasoning.",
]

_N0_CUE = "Okay, now let's try that in Korean — go ahead!"
_N1_CUE = "Nice. Now say it in Korean for me."
_N2_CUE = "Great — now can you say that in Korean?"
_N3_CUE = "Let's hear that in Korean this time."
_N4_CUE = "Now try to explain that in Korean."
_N5_CUE = "Take your time and give me your answer in Korean."

# stage → 부모 노드 스펙(desc, bank, cue, 밴드 라벨). 교차확인 노드는 여기서 파생.
_STAGE_SPEC: dict[int, tuple[str, list[str], str, str]] = {
    0: (_N0_DESC, _N0_BANK, _N0_CUE, "생존·현재"),
    1: (_N1_DESC, _N1_BANK, _N1_CUE, "과거 서술"),
    2: (_N2_DESC, _N2_BANK, _N2_CUE, "계획과 이유"),
    3: (_N3_DESC, _N3_BANK, _N3_CUE, "전언·간접화법"),
    4: (_N4_DESC, _N4_BANK, _N4_CUE, "대조·추측 복문"),
    5: (_N5_DESC, _N5_BANK, _N5_CUE, "의견과 논증"),
}


def _build_nodes() -> dict[str, LadderNode]:
    nodes: dict[str, LadderNode] = {}
    for stage, (desc, bank, cue, label) in _STAGE_SPEC.items():
        main_id = f"N{stage}"
        nodes[main_id] = LadderNode(
            node_id=main_id,
            stage=stage,
            target_desc=desc,
            question_bank=list(bank),
            korean_cue=cue,
        )
        cross_id = f"N{stage}e"
        nodes[cross_id] = LadderNode(
            node_id=cross_id,
            stage=stage,
            target_desc=_cross_desc(label, desc),
            question_bank=list(bank),
            korean_cue=cue,
        )
    return nodes


# --------------------------------------------------------------------------- #
# 사다리
# --------------------------------------------------------------------------- #
class LevelLadder:
    """상태 없는 적응형 사다리. 분기는 전부 그래프 구조로 인코딩(엔진은 현재 노드만 보관).

    그래프 규약:
      주 노드 N{k}
        pass    → N{k+1}  (상위 계단; k=5 면 None=leaf, 이미 천장)
        unclear → N{k}e   (같은 목표 교차확인)
        fail    → N{k}e   (같은 목표 교차확인 = 더 쉬운 재확인 · '1차 실패')
      교차확인 노드 N{k}e
        pass    → N{k+1}  (목표 확정 → 상위 계단 재개; k=5 면 None)
        unclear → None    (확정 불가 → 보수적 종료, 천장 = k-1)
        fail    → None    (leaf; '2차 실패' → 천장 확정)
    → 주 노드 fail 후 교차확인 fail 이면 두 노드 연속 실패로 종료 = "2연속 실패=천장"이
      그래프 구조 자체로 표현된다. 상태 플래그 불필요.
    """

    def __init__(self, nodes: dict[str, LadderNode], root: str) -> None:
        self._nodes = nodes
        self._root = root

    # -- 조회 ---------------------------------------------------------------- #
    def root_id(self) -> str:
        return self._root

    def node(self, node_id: str) -> LadderNode:
        return self._nodes[node_id]

    def target_desc(self, node_id: str) -> str:
        return self._nodes[node_id].target_desc

    def pick_question(
        self,
        node_id: str,
        *,
        interests: list[str] | None = None,
        rng=None,
    ) -> str:
        """question_bank 에서 한 문항을 고르고 {N} 슬롯을 흥미 명사로 치환한다.

        rng 는 테스트 재현용(random.Random 인스턴스 주입 가능). 없으면 전역 random.
        interests 있으면 첫 명사, 없으면 _DEFAULT_NOUN 으로 치환.
        """
        node = self._nodes[node_id]
        chooser = rng if rng is not None else random
        question = chooser.choice(node.question_bank)
        if "{N}" in question:
            noun = interests[0] if interests else _DEFAULT_NOUN
            question = question.replace("{N}", noun)
        return question

    # -- 분기 ---------------------------------------------------------------- #
    def advance(self, node_id: str, verdict: str) -> str | None:
        """verdict(pass/fail/unclear)로 다음 노드 id 를 반환. leaf(종료)면 None."""
        if verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict: {verdict!r} (expected one of {_VERDICTS})")
        node = self._nodes[node_id]  # 미등록 노드면 KeyError
        stage = node.stage
        is_cross = node_id.endswith("e")
        nxt = f"N{stage + 1}" if stage + 1 < _STAGE_COUNT else None

        if not is_cross:
            if verdict == "pass":
                return nxt          # 상위 계단(천장이면 None)
            return f"N{stage}e"     # fail·unclear → 교차확인
        # 교차확인 노드
        if verdict == "pass":
            return nxt              # 목표 확정 → 상위 계단 재개
        return None                 # fail·unclear → 종료(천장 확정)

    # -- 결과 매핑 ----------------------------------------------------------- #
    def band_and_anchor(self, history: list[tuple[str, str]]) -> tuple[str, int]:
        """통과 이력으로 (band, anchor_level) 산출. 마지막 통과 노드의 stage 기준.

        history = [(node_id, verdict), ...]. verdict=='pass' 중 가장 마지막(=최고 계단;
        climb 이 stage 단조증가라 마지막 pass = 최고 pass) 노드의 stage 로 앵커를 정한다.
        통과가 하나도 없으면 survival(레벨 1).
        """
        last_pass_stage: int | None = None
        for node_id, verdict in history:
            if verdict == "pass" and node_id in self._nodes:
                last_pass_stage = self._nodes[node_id].stage
        if last_pass_stage is None:
            return _SURVIVAL_ANCHOR
        return _STAGE_ANCHOR[last_pass_stage]


LEVELTEST_LADDER = LevelLadder(_build_nodes(), root="N0")


# --------------------------------------------------------------------------- #
# 자체 검증 (python -m domains.learning.leveltest_ladder)
# --------------------------------------------------------------------------- #
def _selfcheck() -> None:
    L = LEVELTEST_LADDER
    node_ids = set(L._nodes)  # noqa: SLF001 (자체 검증 전용)

    # 1) 모든 노드 × 모든 verdict → 유효 노드 or None.
    for nid in node_ids:
        for v in _VERDICTS:
            nxt = L.advance(nid, v)
            assert nxt is None or nxt in node_ids, f"invalid edge {nid}/{v} -> {nxt}"

    # 2) root 부터 pass 만 하면 N5 를 거쳐 leaf 도달.
    path, cur = [], L.root_id()
    while cur is not None:
        path.append(cur)
        cur = L.advance(cur, "pass")
    assert path == ["N0", "N1", "N2", "N3", "N4", "N5"], path
    assert L.advance("N5", "pass") is None

    # 3) fail 만 하면 빠르게 leaf(주 노드 fail → 교차확인 fail → None, 2스텝).
    n0e = L.advance(L.root_id(), "fail")
    assert n0e == "N0e", n0e
    assert L.advance(n0e, "fail") is None

    # 4) unclear → 교차확인, 교차확인 pass → 상위 계단 재개.
    assert L.advance("N2", "unclear") == "N2e"
    assert L.advance("N2e", "pass") == "N3"
    assert L.advance("N2e", "unclear") is None

    # 5) band/anchor 정합 (_BAND_RANGE: beginner 2~5 / intermediate 6~9 / advanced 10~13).
    assert L.band_and_anchor([]) == ("survival", 1)
    assert L.band_and_anchor([("N0", "pass")]) == ("beginner", 2)
    assert L.band_and_anchor([("N0", "pass"), ("N1", "pass")]) == ("beginner", 3)
    assert L.band_and_anchor([("N2", "pass")]) == ("beginner", 4)
    assert L.band_and_anchor([("N2e", "pass")]) == ("beginner", 4)  # 교차확인=부모 앵커
    assert L.band_and_anchor([("N3", "pass")]) == ("intermediate", 6)
    assert L.band_and_anchor([("N4", "pass")]) == ("intermediate", 8)
    assert L.band_and_anchor([("N5", "pass")]) == ("advanced", 10)
    # fail 로 끝나도 마지막 pass 기준 유지.
    assert L.band_and_anchor(
        [("N0", "pass"), ("N1", "pass"), ("N2", "fail"), ("N2e", "fail")]
    ) == ("beginner", 3)

    print("leveltest_ladder self-check OK")
    print("nodes:", sorted(node_ids, key=lambda x: (int(x[1]), x)))


if __name__ == "__main__":
    _selfcheck()
