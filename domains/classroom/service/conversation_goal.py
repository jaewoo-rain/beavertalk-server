"""회화 목표 항목 산정 — **단일 출처**.

과제 생성·챕터 미리보기·통화 귀속 셋이 같은 정의를 써야 한다. 갈리면 교사가 화면에서
센 수와 학습자가 받는 목표 수가 달라진다.

## 왜 `is_core` 를 쓰지 않는가

`is_core` 는 **급수 단위로 상위 100~120개**를 뽑는 축이다(`priority_score` 내림차순,
`scripts/curriculum/assign_levels.py` 의 `CORE_TARGET`). 챕터는 **`seq_no` 순 40개씩**
자르는 다른 축이다. 두 축이 서로를 모르므로 챕터당 핵심 수가 0~20 으로 흔들리고
**0 이 나오는 챕터가 생긴다**. 그 챕터에서는 회화 과제가 `0 / 0` 이 된다.

정렬을 어떻게 바꿔도 0 은 계속 생긴다 — **산정 방식 자체가 결함이었다.**

## 확정안

목표 = 그 항목 집합 안에서 `priority_rank` 가 앞선 N개. `min(N, 항목 수)` 이므로
챕터가 40 고정인 한 언제나 성립하고 0 이 나오지 않는다.

⛔ `is_core` 전역 플래그는 **그대로 둔다** — 게이트·복습 선별·grandfathering 이 계속 쓴다.
   회화 목표 산정에서만 뗀다.

★ 대상은 「챕터 전체」가 아니라 **과제의 목표 항목**이다. 교사가 뺀 문장을 회화 목표로
  주면 「빼라고 했는데 왜 시키나」가 된다. 40 중 몇 개를 빼도 남은 것에서 N 개를 고른다.

근거: `docs/서버작업_인수인계_2026-09-01.md` §2 (콘솔 저장소).
"""

from __future__ import annotations

from typing import Iterable

from domains.learning.models.learning_item import LearningItem

# 챕터가 40 고정이라 N=10 은 언제나 성립한다. 현행 실측 중앙값과도 가깝다.
CONVERSATION_TARGET_N = 10


def _rank_key(item: LearningItem) -> tuple[int, int, int, int]:
    """정렬 키. `priority_rank` 는 nullable 이라 빈 값을 **뒤로** 민다.

    동점·빈 값이 있어도 순서가 실행마다 흔들리면 안 된다 — 같은 과제를 두 번 열었을 때
    목표가 달라 보인다. `seq_no` · `item_id` 로 끝까지 결정한다.
    """
    rank = item.priority_rank
    return (1 if rank is None else 0, rank or 0, item.seq_no or 0, item.item_id)


def conversation_target_ids(
    items: Iterable[LearningItem], n: int = CONVERSATION_TARGET_N
) -> list[int]:
    """회화 목표 항목 id — 우선순위 앞선 N개. 항목이 N 보다 적으면 전부."""
    return [i.item_id for i in sorted(items, key=_rank_key)[:n]]
