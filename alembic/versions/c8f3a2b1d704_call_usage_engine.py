"""call 에 usage_engine 컬럼 추가 (원가 계기판 3단계 — 엔진 구분)

usage 8컬럼에는 **어느 엔진이 쓴 토큰인지**가 없다. 캐스케이드가 이 상태로 실사용에 들어가면
`SELECT AVG(...) FROM call` 에 Live 와 캐스케이드가 섞이고, 그러면 캐스케이드 프로젝트의
유일한 목적인 **"정말 싼가"를 데이터로 증명할 수 없게 된다.** 나중에 되짚을 수단도 없다 —
그 행들이 어느 엔진이었는지 기록 자체가 없으므로 백필할 근거가 남지 않는다.

형식(계약 — cascade-impl 과 공유. 임의 변경 금지):
    '<모드>:<구성요소를 + 로 연결>'
    'live:gemini-native-audio'
    'cascade:google-stt-v2+gemini-2.5-flash+cloud-tts-chirp3-hd'
STT/TTS 조합까지 문자열에 박아, 나중에 Whisper 로 바꿔도 **같은 컬럼에서 갈라지게** 한다.

⛔ 달러 컬럼은 여전히 만들지 않는다 — 단가는 벤더가 바꾼다. 토큰·초·문자는 사실이고 원가는
   파생 계산이다(산식은 normalcall_service 한 곳). b7e1c2d3f4a5 와 같은 이유.

nullable 추가 1개라 **기존 796행을 손대지 않는다**(백필 없음, 짧은 잠금, 행 재작성 없음).
NULL = "엔진 미기록"(이 마이그레이션 이전 통화 전부)이다.

설계 근거: docs/20260807_0028_엔진구분-usage_engine-과-peak-수정-계획.md

⚠ 이 파일도 **손으로 작성했다** — 작업 워크트리에 .env 가 없어(gitignore) autogenerate 가
  DB 에 접속할 수 없다. 컬럼명·타입·주석은 모델(domains/learning/models/call.py)과 1:1 대조했다.

Revision ID: c8f3a2b1d704
Revises: b7e1c2d3f4a5
Create Date: 2026-08-07 00:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f3a2b1d704'
down_revision: Union[str, Sequence[str], None] = 'b7e1c2d3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — nullable TEXT 1개 추가 + usage_peak_prompt 주석 정정."""
    op.add_column('call', sa.Column(
        'usage_engine', sa.Text(), nullable=True,
        comment="usage 를 만든 엔진('live:...' / 'cascade:stt+llm+tts'). NULL = 미기록"))
    # 데이터는 그대로 두고 **주석만** 고친다(DDL 은 COMMENT ON COLUMN 1줄 — 잠금 무시할 수준).
    # 이 컬럼에는 압축마다 리셋되는 사이클 peak 가 들어가고 있었다(call 909: 13,355 저장 vs
    # 실제 최대 15,904). 코드는 이제 통화 전체 최대치를 넣는다 — 의미가 바뀌었으니 주석도 바꾼다.
    # ⚠ 이 마이그레이션 **이전에 쌓인 행의 값은 여전히 사이클 peak** 다. 백필은 불가능하다
    #   (원본 시계열이 Cloud Logging 에만 있고 보존 30일). 트리거 튜닝 표본은 이 시점 이후 통화로.
    op.alter_column(
        'call', 'usage_peak_prompt',
        existing_type=sa.Integer(), existing_nullable=True,
        comment='이 통화가 도달한 최대 컨텍스트(단조증가 — 압축·트리거 튜닝의 핵심 지표)',
        existing_comment='이 통화가 도달한 최대 컨텍스트(압축·트리거 튜닝의 핵심 지표)',
    )


def downgrade() -> None:
    """Downgrade schema — usage_engine 제거.

    ⚠ 되돌리면 **어느 행이 어느 엔진이었는지가 소실된다.** 토큰 수는 남지만 그걸 Live 단가로
      계산할지 캐스케이드 단가로 계산할지 알 수 없게 되므로, 원가 비교가 통째로 무의미해진다.
      되돌리기 전에 `SELECT id, usage_engine FROM call WHERE usage_engine IS NOT NULL` 정도는
      먼저 덤프해 둬라(복원 시 UPDATE 로 되돌릴 수 있다).
    스키마는 완전히 원상 복구된다(nullable 추가 1개였으므로 다른 컬럼·인덱스 무영향).
    """
    op.alter_column(
        'call', 'usage_peak_prompt',
        existing_type=sa.Integer(), existing_nullable=True,
        comment='이 통화가 도달한 최대 컨텍스트(압축·트리거 튜닝의 핵심 지표)',
        existing_comment='이 통화가 도달한 최대 컨텍스트(단조증가 — 압축·트리거 튜닝의 핵심 지표)',
    )
    op.drop_column('call', 'usage_engine')
