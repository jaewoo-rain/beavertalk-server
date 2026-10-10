# -*- coding: utf-8 -*-
"""voice.openai_name — 엔진별 음색 이름 칸을 따로 둔다 (2026-10-10 사장님 지시)

## 왜 필요했나 — 실측

DB 캐릭터 음색은 **Gemini 프리빌트 이름**(`Fenrir`·`Leda`·`Vindemiatrix`·`Sulafat`·
`Sadachbia`)인데, OpenAI Realtime 이 받는 음색은 10종(`alloy`·`ash`·`ballad`·`cedar`·
`coral`·`echo`·`marin`·`sage`·`shimmer`·`verse`)이고 **겹치는 이름이 하나도 없다.**

그래서 GPT 통화에서 어댑터가 매번 기본값으로 떨어뜨렸다:

    normalcall OpenAI 음색 대체: 'Fenrir' 은 이 엔진이 받지 않는다 → 'marin'

⇒ **다섯 캐릭터가 전부 같은 목소리로 나왔다.** 로그엔 보였지만 사용자에겐 그냥
「다 같은 목소리」였다.

## 왜 코드 표가 아니라 DB 칸인가 (사장님 결정)

「DB에 컬럼 하나 더 추가해서 gemini용 캐릭터이름, openai전용 캐릭터이름 이렇게 넣게하자.」
어댑터에 하드코딩 표를 뒀다가 지웠다 — 두 곳이 같은 표를 들면 갈라지고, 캐릭터가 늘 때
둘을 맞춰야 한다. 음색은 **데이터**다.

## 안전성

- `NULL` 허용이라 **Gemini 경로는 바이트 무영향**이다(그쪽은 `voice.name` 만 읽는다).
- 이 DB 는 실서비스와 **공유**다(`app-api`·`demo-api`·`gpt-api` 가 같은 Supabase).
  칸을 더하는 것뿐이고 기존 읽기 경로를 건드리지 않는다.
- 칸이 비면 어댑터가 종전대로 기본값으로 떨어지고 **WARNING 을 남긴다** — 새 캐릭터를
  넣고 안 채우면 그 로그가 신호다(조용히 틀리지 않는다).

## 배정 근거

`voice.description`(성격) × `character.gender` 로 맞췄다. ⛔ 성별을 뒤집지 마라 —
사용자가 듣는 변화다.

| 캐릭터 | 성별 | Gemini | 성격 | → OpenAI |
|---|---|---|---|---|
| Baba | 남 | Fenrir | 활기찬·흥분한(Excitable) | `verse` |
| Dudu | 남 | Sadachbia | 생기있는(Lively) | `cedar` |
| Bibi | 여 | Leda | 젊은(Youthful) | `shimmer` |
| Popo | 여 | Vindemiatrix | 온화한(Gentle) | `sage` |
| Rara | 여 | Sulafat | 따뜻한(Warm) | `coral` |

나머지 25종은 **NULL 로 둔다** — 쓰는 캐릭터가 없고, 추측으로 채우면 나중에 그 캐릭터가
생길 때 「누가 정한 값인지」를 모른다. 그때 로그를 보고 채운다.

⚠ `marin` 은 비워 둔다 — 그게 폴백 기본값이라, 어떤 캐릭터에 배정하면 「배정된 것」과
「떨어진 것」을 로그로 못 가른다.
"""

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f40506"
down_revision = "bcb8a018d734"
branch_labels = None
depends_on = None


# Gemini 음색명 → OpenAI Realtime 음색명 (위 표 그대로)
_MAP = {
    "Fenrir": "verse",
    "Sadachbia": "cedar",
    "Leda": "shimmer",
    "Vindemiatrix": "sage",
    "Sulafat": "coral",
}


def upgrade() -> None:
    op.add_column(
        "voice",
        sa.Column(
            "openai_name",
            sa.Text(),
            nullable=True,
            comment="OpenAI Realtime 음색명(예: verse, coral). NULL 이면 기본값으로 떨어진다",
        ),
    )
    # 쓰는 캐릭터의 다섯 줄만 채운다(나머지는 NULL — 위 독스트링).
    conn = op.get_bind()
    for gem, oa in _MAP.items():
        conn.execute(
            sa.text("UPDATE voice SET openai_name = :oa WHERE name = :gem"),
            {"oa": oa, "gem": gem},
        )


def downgrade() -> None:
    # ⚠ 칸을 지우면 배정이 사라진다 — GPT 통화가 다시 전부 기본값 한 목소리가 된다.
    op.drop_column("voice", "openai_name")
