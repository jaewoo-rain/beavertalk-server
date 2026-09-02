"""교사 콘솔 DTO.

응답에 **넣지 않는 것**을 먼저 정한다(`04_학습자관리.md` §5 개인정보 경계).
교사는 학습자의 이메일·앱 이름·국적·모국어·이 반 밖의 학습 기록을 볼 수 없다.
그래서 `RosterMemberOut` 에는 `member_id` 조차 넣지 않는다 — 콘솔이 다른 API 로
학습자를 조회할 통로를 애초에 열지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Activity = Literal["speaking", "conversation", "workbook"]


# ── 반 ──
class ClassroomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_grade: int = Field(ge=1, le=6)
    term: Optional[str] = Field(default=None, max_length=60)
    institution: Optional[str] = Field(default=None, max_length=120)
    teacher_display_name: Optional[str] = Field(default=None, max_length=60)
    capacity: int = Field(default=30, ge=1, le=200)


class ClassroomUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_grade: Optional[int] = Field(default=None, ge=1, le=6)
    term: Optional[str] = None
    institution: Optional[str] = None
    teacher_display_name: Optional[str] = None
    capacity: Optional[int] = Field(default=None, ge=1, le=200)
    code_expires_on: Optional[date] = None


class ClassroomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    classroom_id: int
    name: str
    target_grade: int
    term: Optional[str]
    join_code: str
    capacity: int
    code_expires_on: Optional[date]
    institution: Optional[str]
    teacher_display_name: Optional[str]
    archived_at: Optional[datetime]
    learner_count: int = 0
    assignment_count: int = 0


# ── 명단 ──
class RosterMemberOut(BaseModel):
    """콘솔 명단 행. 🔴 member_id·이메일·국적·모국어는 넣지 않는다."""

    model_config = ConfigDict(from_attributes=True)

    classroom_member_id: int
    roster_name: str
    student_no: Optional[str]
    teacher_alias: Optional[str]
    joined_at: datetime
    confirmed_at: Optional[datetime]
    last_seen_days: Optional[int] = None
    missed: int = 0
    cum_speaking: Optional[int] = None
    cum_conversation: Optional[int] = None


class RosterMemberUpdate(BaseModel):
    """교사가 고칠 수 있는 것만. `roster_name` 은 학습자가 적은 값이지만
    출석부와 대조해 교정할 수 있어야 한다(`04` §7)."""

    roster_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    student_no: Optional[str] = Field(default=None, max_length=40)
    teacher_alias: Optional[str] = Field(default=None, max_length=80)
    confirmed: Optional[bool] = None


# ── 과제 ──
class AssignmentCreate(BaseModel):
    grade: int = Field(ge=1, le=6)
    chapter: int = Field(ge=1)
    activities: list[Activity] = Field(min_length=1)
    due_at: datetime
    excluded_item_ids: list[int] = Field(default_factory=list)

    @field_validator("activities")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for a in v:
            if a not in seen:
                seen.append(a)
        return seen


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    assignment_id: int
    classroom_id: int
    grade: int
    chapter: int
    chapter_range: str = ""
    activities: list[Activity]
    due_at: datetime
    closed_at: Optional[datetime]
    completed: int = 0
    total: int = 0
    avg_speaking: Optional[float] = None
    avg_conversation: Optional[float] = None
    speaking_total: int = 0
    conversation_total: int = 0


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    classroom_member_id: int
    roster_name: str
    status: str
    speaking_passed: Optional[int]
    speaking_total: Optional[int]
    conversation_met: Optional[int]
    conversation_total: Optional[int]
    completed_at: Optional[datetime]


class WeakItemOut(BaseModel):
    """반 단위 취약 항목 — 다시 가르쳐야 할 문장 / 덜 쓰인 표현."""

    item_id: int
    surface: str
    example: Optional[str]
    kind: str
    hit: int
    of: int


class AssignmentResultOut(BaseModel):
    assignment: AssignmentOut
    submissions: list[SubmissionOut]
    reteach: list[WeakItemOut]
    least_used: list[WeakItemOut]


# ── 학습자 참여(앱 쪽) ──
class JoinPreviewOut(BaseModel):
    """A2 반 확인 — 코드만으로 보여줄 수 있는 최소 정보."""

    classroom_id: int
    name: str
    institution: Optional[str]
    teacher_display_name: Optional[str]
    target_grade: int
    term: Optional[str]
    learner_count: int
    capacity: int


class JoinIn(BaseModel):
    join_code: str = Field(min_length=6, max_length=6)
    roster_name: str = Field(min_length=1, max_length=80)
    student_no: Optional[str] = Field(default=None, max_length=40)
    share_consent: bool = Field(description="수행 여부·결과를 교사에게 공유하는 데 동의")
