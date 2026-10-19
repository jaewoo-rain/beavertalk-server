"""음성 클립 통화 시험(PM-DEC-856 · exp/clip-call 전용) — 반복되는 비버 말(웃음·짧은 반응·표현학습 출제)을 녹음 클립으로 보낸다.

요청서: 52 하네스 `_output/2026-10-14_통화흐름_공동설계/57_공유_클립통화시험요청.md` · 자산: `assets/clips/<캐릭터>/`(WAV PCM16 24kHz 모노).

- T1 반응 클립: 이미 블로킹으로 부르는 `set_face` 에 선택 인자 `reaction` 을 얹는다(새 도구 없음 = 응답 횟수 그대로 · SV-15·16).
  서버는 받은 즉시 클립 PCM 을 학습자에게 보내고, 모델 음성은 그 뒤에 이어 붙는다(클라 재생 큐가 순서대로 튼다 — 겹침 없음).
  호출 시점에 비버 턴이 아직 안 열렸으면(모델은 대개 말하기 전에 부른다) 턴이 열리는 순간 모델 오디오 **앞**에 보낸다.
- T2 표현학습 출제 클립: 추적기(ExpressionPractice)가 «다시 말하기» 단계에 있으면(다음 학습자 답 뒤 반드시 다음 표현으로 간다)
  그 학습자 턴 안내에 «반응만 하고 멈춰라 · 다음 출제는 서버가 붙인다» 를 얹고, 다음 비버 턴 끝(turn_end 앞)에 출제 클립을 붙인다.
- 꺼짐(env CLIP_CALL 비어 있음)이면 아무것도 안 만든다 — 도구 선언·지시문·와이어 모두 종전과 같다.
"""
from __future__ import annotations

import json
import logging
import math
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from domains.learning.service import quiz_judge

logger = logging.getLogger(__name__)

CLIP_ROOT = Path(__file__).resolve().parents[3] / "assets" / "clips"
_CHUNK = 9600   # 0.2초(24kHz·16bit·mono = 48,000 B/s) — Live 조각보다 크지만 클라 큐에는 같다

# 운영 voice 표와 같은 이름(요청서 §2) — 캐릭터 이름 대신 voice 로 고른다(setup["voice"] 가 늘 있다).
VOICE_BANK = {"Fenrir": "Baba", "Leda": "Bibi"}

# 반응 묶음: 이름 → (클립 id들, 모델에게 보이는 설명, 학습자 모국어 조건). 같은 묶음 안에서 변형을 돌린다.
#   ⚠ 「사람 확인 필요」 클립(웃음·감탄 전부 · 비비 A_ko_4·A_en_2)도 넣었다 — T4 에서 귀로 판정한다(요청서 §2).
REACTIONS: dict[str, dict[str, tuple[tuple[str, ...], str, str]]] = {
    "Baba": {
        "laugh": (("A_laugh_1", "A_laugh_2", "A_laugh_3", "A_laugh_4", "A_laugh_5", "A_laugh_6"), "웃음 소리(크큭·풉·흥·하!)", ""),
        "comfort": (("A_ko_1", "A_ko_2", "A_ko_3"), "「괜찮아」 「괜찮아, 모를 수도 있지」 「뭐, 그럴 수 있지」", ""),
        "curious": (("A_ko_4", "A_ko_5"), "「무슨 일 있었어?」 「그래서?」", ""),
        "try_it": (("A_en_1", "A_en_2"), "「Try it.」 「Try saying that.」", "en"),
        "not_quite": (("A_en_3",), "「Not quite.」", "en"),
        "got_it": (("A_en_4",), "「Fine, you got that one.」", "en"),
        "hint": (("A_en_5",), "「No worries. Here's a hint.」(바로 뒤에 힌트를 말한다)", "en"),
    },
    "Bibi": {
        "laugh": (("A_laugh_1", "A_laugh_2", "A_laugh_3", "A_laugh_4", "A_laugh_5"), "웃음·감탄 소리(헐!·와!·하하·Wow!)", ""),
        "comfort": (("A_ko_1", "A_ko_2"), "「괜찮아, 괜찮아」 「괜찮아, 그럴 수 있지!」", ""),
        "curious": (("A_ko_3", "A_ko_4", "A_ko_5"), "「얘기 좀 해 줘!」 「오, 그래?」 「무슨 일 있었어?」", ""),
        "nice": (("A_en_2", "A_en_3"), "「Okay.」 「Nice.」", "en"),
        "almost": (("A_en_4",), "「Almost!」", "en"),
        "no_worries": (("A_en_5",), "「No worries, it's alright!」", "en"),
    },
}


@dataclass(slots=True)
class Clip:
    id: str
    text: str
    pcm: bytes
    item: str = ""        # 출제·따라 말하기 클립의 표현(정규화 전 원문)
    slot: str = ""


def _read_pcm(path: Path, gain_db: float) -> bytes:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (24000, 1, 2):
            raise ValueError(f"클립 형식이 Live 출력(24kHz·모노·16bit)과 다르다: {path.name}")
        raw = w.readframes(w.getnframes())
    if not gain_db:
        return raw
    g = math.pow(10.0, gain_db / 20.0)
    a = array("h", raw)
    for i, x in enumerate(a):
        a[i] = max(-32768, min(32767, int(x * g)))
    return a.tobytes()


_BANKS: dict[tuple[str, float], dict[str, Clip]] = {}


def load_bank(name: str, gain_db: float = 0.0) -> dict[str, Clip]:
    """캐릭터 클립 묶음을 한 번만 읽는다(프로세스 캐시). manifest.json 의 file 칸이 정본이다."""
    key = (name, gain_db)
    if key not in _BANKS:
        d = CLIP_ROOT / name
        rows = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        _BANKS[key] = {
            r["id"]: Clip(id=r["id"], text=r.get("text") or "", pcm=_read_pcm(d / r["file"], gain_db),
                          item=r.get("item") or "", slot=r.get("slot") or "")
            for r in rows
        }
    return _BANKS[key]


@dataclass
class ClipCall:
    """통화 하나의 클립 상태 — 재생 이력(같은 클립 연속 금지)·대기 클립·출제 예약."""

    bank_name: str
    bank: dict[str, Clip]
    locale: str
    reactions: dict[str, tuple[tuple[str, ...], str]]
    ask: bool = True
    last_id: str = ""
    turn: dict[str, int] = field(default_factory=dict)       # 묶음별 다음 변형 순번
    pending: list[Clip] = field(default_factory=list)        # 턴이 열리면 모델 오디오 앞에 보낼 클립
    ask_armed: Optional[int] = None                          # 다음 비버 턴 끝에 출제 클립을 붙일 항목 번호(0부터)
    ask_variant: int = 0
    played: list[str] = field(default_factory=list)          # 보낸 클립 id(통화 끝 로그)
    played_s: float = 0.0
    skipped: int = 0

    # ── 선언·지시문 ──
    def reaction_names(self) -> list[str]:
        return list(self.reactions)

    def reaction_description(self) -> str:
        parts = [f"{k}={desc}" for k, (_ids, desc) in self.reactions.items()]
        return ("넣으면 서버가 네 목소리로 녹음된 그 소리를 이 자리에서 들려준다(같은 말을 다시 하지 마라). "
                "웃음·짧은 반응을 할 때만 넣는다. 값과 들리는 말: " + " · ".join(parts))

    def prompt_rule(self) -> str:
        parts = [f"{k}={desc}" for k, (_ids, desc) in self.reactions.items()]
        lines = [
            "",
            "[반응 소리]",
            "- 웃음이나 아래 짧은 반응을 할 때는 그 말을 직접 소리 내지 말고, `set_face` 를 부를 때 `reaction` 에 그 이름을 넣어라."
            " 서버가 네 목소리로 녹음된 그 소리를 바로 그 자리에서 들려준다.",
            "- reaction 을 넣었으면 같은 말을 다시 하지 말고 그 다음 말부터 이어서 해라. 반응 소리만 내고 입을 다물지 마라.",
            "- 값과 들리는 말: " + " · ".join(parts),
        ]
        return "\n".join(lines)

    # ── 선택 ──
    def pick_reaction(self, name: str) -> Optional[Clip]:
        spec = self.reactions.get(name)
        if not spec:
            return None
        ids = [i for i in spec[0] if i in self.bank]
        if not ids:
            return None
        n = self.turn.get(name, 0)
        for k in range(len(ids)):
            cid = ids[(n + k) % len(ids)]
            if cid != self.last_id:
                self.turn[name] = (n + k + 1) % len(ids)
                return self.bank[cid]
        self.skipped += 1          # 변형이 하나뿐인데 직전과 같다 → 연속 금지(요청서 T1)
        return None

    def ask_clip_for(self, obj: str) -> Optional[Clip]:
        """그 표현의 출제 클립(변형 순환). 학습자 모국어가 en 일 때만(클립이 영어다)."""
        if not self.ask or not self.locale.startswith("en"):
            return None
        want = quiz_judge.normalize(obj or "", "ko")
        if not want:
            return None
        cands = [c for c in self.bank.values()
                 if c.id.startswith("B_ask_") and quiz_judge.normalize(c.item, "ko") == want]
        if not cands:
            return None
        cands.sort(key=lambda c: c.id)
        clip = cands[self.ask_variant % len(cands)]
        if clip.id == self.last_id and len(cands) > 1:
            clip = cands[(self.ask_variant + 1) % len(cands)]
        self.ask_variant += 1
        return clip

    def note_played(self, clip: Clip) -> None:
        self.last_id = clip.id
        self.played.append(clip.id)
        self.played_s += len(clip.pcm) / 48000.0

    def summary(self) -> str:
        return f"클립 {len(self.played)}개 {self.played_s:.1f}초 · 연속금지 건너뜀 {self.skipped} · {','.join(self.played[-12:])}"


def for_call(voice: Optional[str], locale: str, *, enabled: str, ask: bool = True, gain_db: float = 0.0) -> Optional[ClipCall]:
    """이 통화에 클립을 쓸지. enabled = env CLIP_CALL — 빈 값이면 끔 · "on" 이면 클립이 있는 캐릭터 전부 · 아니면 쉼표로 고른 묶음(Baba,Bibi)."""
    if not enabled:
        return None
    name = VOICE_BANK.get(voice or "")
    if not name or name not in REACTIONS:
        return None
    if enabled != "on" and name not in {s.strip() for s in enabled.split(",")}:
        return None
    try:
        bank = load_bank(name, gain_db)
    except Exception as exc:  # noqa: BLE001 — 클립이 없으면 종전 통화로(R5)
        logger.warning("clipcall: 클립 묶음 %s 읽기 실패 — 끈 채로 진행: %s", name, exc)
        return None
    loc = (locale or "").lower()
    reactions = {k: (ids, desc) for k, (ids, desc, lang) in REACTIONS[name].items()
                 if (not lang or loc.startswith(lang)) and any(i in bank for i in ids)}
    return ClipCall(bank_name=name, bank=bank, locale=loc, reactions=reactions, ask=ask)


def chunks(pcm: bytes):
    for i in range(0, len(pcm), _CHUNK):
        yield pcm[i:i + _CHUNK]
