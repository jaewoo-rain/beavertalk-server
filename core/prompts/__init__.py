"""core/prompts — 통화 코스별 지시문 조립.

common 이 공유 자산을 소유하고, expression/freetalk 이 각자의 학습 플로우를 얹는다.
⛔ 여기서 심볼을 재수출하지 않는다 — 순환 import 를 만들지 않기 위해서다
  (core.persona_prompt 가 common 을 import 한다).
"""
