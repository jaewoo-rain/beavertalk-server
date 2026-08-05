"""call 에 Live usage 계기판 컬럼 8개 추가 (원가 계기판 2단계)

Cloud Logging 보존이 30일이라 그 뒤엔 원가 근거가 사라진다. 무제한 플랜(Pro·Max)을 여는
이상 원가 추이를 계속 봐야 하므로 통화 행에 남긴다.

설계 근거: docs/20260805_1950_원가계기판-2단계-영속화-설계.md
- 집계에 자주 쓰는 값 7개만 정수 컬럼으로 승격. 특히 모달리티 4항은 **단가가 서로 다르다**
  (입력 오디오 $3 / 입력 텍스트 $0.5 / 출력 오디오 $12 / 출력 텍스트 $2) — 합쳐 두면
  원가를 계산할 수가 없다.
- 나머지는 usage_json 에. ⛔ JSONB 가 아니라 JSON 이다 — 프로젝트 규약(테스트가 sqlite
  인메모리라 JSONB 금지). 집계는 위 컬럼이 담당하므로 JSONB 의 인덱싱 이점이 필요 없다.
- ⛔ 원가(달러) 컬럼은 일부러 없다. 단가는 바뀐다 — 토큰은 사실, 원가는 파생 계산.

전부 NULL 허용이라 **기존 행은 손대지 않는다**(백필 없음). NULL = "계측 안 됨"이고
0 = "정말 0 토큰"이라, 둘이 구별돼야 표본을 신뢰할 수 있다.

⚠ 이 파일은 **손으로 작성했다.** 작업 워크트리에 .env 가 없어(gitignore) autogenerate 가
  DB 에 접속할 수 없었다. 컬럼명·타입은 모델(domains/learning/models/call.py)과 1:1 대조했다.

Revision ID: b7e1c2d3f4a5
Revises: a1b2c3d4e5f6
Create Date: 2026-08-05 19:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e1c2d3f4a5'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — 전부 nullable 추가라 잠금이 짧고 기존 행 재작성이 없다."""
    op.add_column('call', sa.Column(
        'usage_msgs', sa.Integer(), nullable=True,
        comment='Live usage 관측 메시지 수(0/NULL = 미수신)'))
    op.add_column('call', sa.Column(
        'usage_in_audio', sa.BigInteger(), nullable=True,
        comment='입력 오디오 토큰 합($3.00/1M)'))
    op.add_column('call', sa.Column(
        'usage_in_text', sa.BigInteger(), nullable=True,
        comment='입력 텍스트 토큰 합($0.50/1M)'))
    op.add_column('call', sa.Column(
        'usage_out_audio', sa.BigInteger(), nullable=True,
        comment='출력 오디오 토큰 합($12.00/1M)'))
    op.add_column('call', sa.Column(
        'usage_out_text', sa.BigInteger(), nullable=True,
        comment='출력 텍스트 토큰 합($2.00/1M)'))
    op.add_column('call', sa.Column(
        'usage_total', sa.BigInteger(), nullable=True,
        comment='API 총 토큰 합(모달리티 4항의 합이 아니다 — thoughts·cached 포함)'))
    op.add_column('call', sa.Column(
        'usage_peak_prompt', sa.Integer(), nullable=True,
        comment='이 통화가 도달한 최대 컨텍스트(압축·트리거 튜닝의 핵심 지표)'))
    op.add_column('call', sa.Column(
        'usage_json', sa.JSON(), nullable=True,
        comment='usage 요약 원본(dropped/monotonic/last/thoughts/기타 모달리티/재연결·압축 수)'))


def downgrade() -> None:
    """Downgrade schema — 컬럼 8개 제거.

    ⚠ 이 통화들의 usage 계측값은 **소실된다.** 원본은 Cloud Logging 의 `normalcall usage:`
      줄이지만 보존이 30일이라, 30일이 지난 통화는 복원 수단이 없다. 되돌리기 전에
      필요한 기간의 값을 먼저 내보내라(SELECT 로 CSV 덤프면 충분하다).
    스키마 자체는 완전히 원상 복구된다(전부 nullable 추가였으므로 다른 컬럼·인덱스 무영향).
    """
    op.drop_column('call', 'usage_json')
    op.drop_column('call', 'usage_peak_prompt')
    op.drop_column('call', 'usage_total')
    op.drop_column('call', 'usage_out_text')
    op.drop_column('call', 'usage_out_audio')
    op.drop_column('call', 'usage_in_text')
    op.drop_column('call', 'usage_in_audio')
    op.drop_column('call', 'usage_msgs')
