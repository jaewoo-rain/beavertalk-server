"""09 Locust 시나리오 — 파이썬 코드로 부하 시나리오를 쓴다.

각 가상 유저(HttpUser)는 wait_time 만큼 쉬었다가, @task 가중치에 따라
엔드포인트를 골라 때린다. Locust 가 응답시간을 모아 중앙값/P95/P99/RPS 를
자동 집계해 표로 뿌린다(=8장 지표를 실제로 생성).

⚠️ --host 는 반드시 로컬(127.0.0.1)로. 프로덕션에 걸지 말 것.

헤드리스 실행(웹 UI 없이 콘솔 통계만):
  uv run --with locust --with fastapi --with uvicorn locust \
      -f 09_locustfile.py --headless -u 50 -r 10 -t 15s \
      --host http://127.0.0.1:8009

  -u 50   가상 유저 50명
  -r 10   초당 10명씩 램프업(spawn rate)
  -t 15s  15초 동안
"""

from __future__ import annotations

from locust import HttpUser, between, task


class MixedUser(HttpUser):
    # 각 요청 사이 0.1~0.5초 랜덤 대기(현실적인 유저 think-time)
    wait_time = between(0.1, 0.5)

    @task(3)  # 가중치 3 — 제일 자주
    def hit_fast(self) -> None:
        self.client.get("/fast")

    @task(2)
    def hit_slow(self) -> None:
        self.client.get("/slow")

    @task(1)
    def hit_work(self) -> None:
        self.client.get("/work")
