"""원문/현지인 복창 진행. 전사 증거만 사용하며 퀴즈 통과/DB 진도를 쓰지 않는다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from domains.learning.service import quiz_judge

_QUOTED = re.compile(r'["“「«]([^"”」»\n]{1,240})["”」»]')
_SCRIPT = {
    'ko': r'[가-힣]', 'ja': r'[ぁ-んァ-ン一-龥]', 'zh': r'[一-龥]',
    'en': r'[A-Za-z]', 'fr': r'[A-Za-zÀ-ÿ]', 'vi': r'[A-Za-zÀ-ỹ]',
}
_NATIVE_KO = re.compile(r'(?:자연스럽게(?:는)?|현지인(?:들은|은)?(?:\s*(?:보통|자연스럽게))?(?:는|도)?)\s*[:,]?\s*(.+?)(?:이라고|라고)\s*(?:해요|말해요|합니다|하죠|해|말해)')
_TARGET_RUN = {'ko': r'[가-힣][가-힣\s]*[가-힣]', 'ja': r'[ぁ-んァ-ン一-龥][ぁ-んァ-ン一-龥\sー]*[ぁ-んァ-ン一-龥]'}
_NATIVE_CONTEXT = re.compile(r'naturally|native\s+(?:speaker|expression)|현지인|자연스럽|自然', re.I)


@dataclass
class ExpressionPractice:
    items: list[dict]
    language: str = 'ko'
    index: int = 0
    phase: str = 'introduce'
    original_attempts: int = 0
    native_attempts: int = 0
    native_phrase: str = ''
    completed: set[int] = field(default_factory=set)
    unresolved_native: set[int] = field(default_factory=set)

    @property
    def item(self) -> dict | None:
        return self.items[self.index] if self.index < len(self.items) else None

    @property
    def pending(self) -> bool:
        return self.phase in ('original', 'native_intro', 'native')

    def _original(self) -> str:
        it = self.item or {}
        return str((it.get('ex') if it.get('role') == 'grammar' else it.get('obj')) or it.get('obj') or '')

    def _matches(self, text: str, phrase: str) -> bool:
        return bool(phrase and quiz_judge.normalize(text, self.language) == quiz_judge.normalize(phrase, self.language))

    def observe_beaver(self, text: str) -> None:
        if not self.item or not text.strip():
            return
        if self.phase == 'introduce':
            if quiz_judge.item_mentioned(text, str(self.item.get('obj') or ''), self.item.get('ex'), language=self.language):
                self.phase = 'original'
            return
        if self.phase != 'native_intro':
            return
        # 명시적인 현지인 소개라면 후속 항목에도 있는 표현을 인정한다.
        # 목록 이동은 전체 원문 목록과 대조하여 현지인 복창으로 오인하지 않는다.
        source_items = [self.item] if _NATIVE_CONTEXT.search(text) else self.items
        originals = [str(it.get(k) or '') for it in source_items for k in ('obj', 'ex', 'native', 'des')]
        candidates = _QUOTED.findall(text)
        if not candidates:
            candidates = _NATIVE_KO.findall(text)
        foreign_script = r'[A-Za-zぁ-んァ-ン]' if self.language == 'ko' else r'[A-Za-z가-힣]'
        if not candidates and self.language in _TARGET_RUN and re.search(foreign_script, text):
            # 모국어와 목표어가 다른 전사는 목표어 문자 구간으로 구절을 찾는다.
            # 같은 문자권의 설명/요청은 문장으로 추측하지 않고 아래 모호성 가드에 맡긴다.
            candidates = re.findall(_TARGET_RUN[self.language], text)
        candidates = [s.strip() for s in candidates if re.search(_SCRIPT.get(self.language, r'\w'), s)]
        candidates = [s for s in candidates if not any(self._matches(s, p) for p in originals)]
        candidates = list(dict.fromkeys(candidates))
        # 여러 문장은 어느 문장을 복창할지 불명확하므로 상태를 확정하지 않는다.
        if len(candidates) == 1:
            self.native_phrase = candidates[0]
            self.phase = 'native'

    def observe_user(self, text: str) -> None:
        if not self.item or not text.strip():
            return  # 무음/전사 누락/안내문은 복창 성공이나 시도가 아니다.
        if self.phase == 'original':
            self.original_attempts += 1
            options = [self._original()]
            if self.item.get('role') != 'grammar' and self.item.get('ex'):
                options.append(str(self.item['ex']))
            if any(self._matches(text, phrase) for phrase in options):
                self.phase = 'native_intro'
            elif self.original_attempts >= 2:
                self._advance()
        elif self.phase == 'native':
            self.native_attempts += 1
            if self._matches(text, self.native_phrase) or self.native_attempts >= 2:
                self._advance()
        elif self.phase == 'native_intro':
            # 전사에서 구절을 확정할 수 없어도 이미 요청된 복창을 무한 대기하지 않는다.
            # 두 실제 응답 이후 소진으로 진행하며 구절/정답/퀴즈 통과는 추정하지 않는다.
            self.native_attempts += 1
            if self.native_attempts >= 2:
                self.unresolved_native.add(self.index + 1)
                self._advance()

    def _advance(self) -> None:
        self.completed.add(self.index + 1)
        self.index += 1
        self.phase = 'introduce' if self.item else 'complete'
        self.original_attempts = self.native_attempts = 0
        self.native_phrase = ''

    def replay(self, turns: list[tuple[str, str]]) -> None:
        for role, text in turns:
            if role == 'beaver':
                self.observe_beaver(text)
            elif role == 'user':
                self.observe_user(text)

    def brief(self) -> str:
        if self.item is None:
            return '복창 단계는 모두 정리됨. 서버의 원문 퀴즈 안내와 기존 재료 활용을 따른다.'
        if self.phase == 'native_intro':
            action = '원문 정답 뒤 현지인 표현 한 문장을 학습 언어로 알려준다. 전사에서 큰따옴표로 구별하고 복창 요청 뒤 기다린다.'
        elif self.phase == 'native':
            action = f'현지인 문장 «{self.native_phrase}» 복창 대기. 시도 {self.native_attempts}/2이며 같은 문장을 유지하고 답을 기다린다.'
        elif self.phase == 'original':
            action = f'원문 «{self._original()}» 복창 대기. 시도 {self.original_attempts}/2이며 정답은 현지인 문장 소개로 이어간다.'
        else:
            action = f'다음 원문 «{self._original()}»의 뜻·쓰임·문장·예문을 알려주고 복창을 기다린다.'
        return '[현재 복창 단계] ' + action + ' 현지인 문장은 원문 항목으로 추가 집계하지 않는다. 미완료 단계 중 다음 원문이나 퀴즈로 건너뛰지 않는다. 안내문은 낭독하지 않는다.'
