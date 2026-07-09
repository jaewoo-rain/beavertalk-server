"""15 (a) Keep-Alive 실측 — httpx.Client 를 매번 새로 만들기 vs 재사용.

로컬에 작은 FastAPI 앱을 '진짜 TCP 포트'로 띄운다(uvicorn 하위 프로세스).
in-process ASGITransport 는 TCP 가 없어 이 데모엔 부적합하므로 실제 소켓을 쓴다.

핵심: 평문 HTTP 는 로컬 TCP 핸드셰이크가 너무 싸서 차이가 잘 안 보인다.
그래서 서버를 HTTPS(자체서명 인증서)로 띄운다 → 새 연결마다 'TLS 핸드셰이크'라는
진짜 왕복 비용이 붙는다. 이게 원격 https 서버(Supabase/Resend 등)에 매 요청
새 연결을 여는 상황을 정직하게 흉내 낸다.

  (1) 매 요청 새 httpx.Client()  → 매번 연결을 닫으므로 매번 TLS 핸드셰이크
  (2) httpx.Client() 하나 재사용 → 첫 요청만 핸드셰이크, 나머지는 keep-alive 재사용

⚠️ 대상은 반드시 로컬(127.0.0.1). 원격/프로덕션 금지.

실행:
    uv run --with fastapi --with uvicorn --with httpx --with cryptography python 15_keepalive_reuse.py
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

# Windows 콘솔(cp949)에서도 한글/기호가 깨지지 않게 UTF-8 로 출력
with __import__("contextlib").suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")

PORT = 8155
BASE = f"https://127.0.0.1:{PORT}"
N = 200  # 순차 요청 횟수


def make_self_signed_cert(dst_dir: Path) -> tuple[Path, Path]:
    """127.0.0.1 용 자체서명 인증서/키를 만들어 파일 경로를 돌려준다."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    key_path = dst_dir / "key.pem"
    cert_path = dst_dir / "cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


# ── 작은 FastAPI 앱을 파일로 써서 uvicorn 이 import 하게 한다 ──────────────
_APP_SRC = """
from fastapi import FastAPI
app = FastAPI()

@app.get("/ping")
def ping():
    return {"ok": True}
"""


def bench(reuse: bool, verify: str) -> tuple[float, int]:
    """N회 순차 GET. reuse=True 면 Client 하나 재사용, False 면 매번 새로."""
    latencies_total = 0.0
    if reuse:
        with httpx.Client(verify=verify) as client:
            t0 = time.perf_counter()
            for _ in range(N):
                client.get(f"{BASE}/ping").raise_for_status()
            latencies_total = time.perf_counter() - t0
        conns = 1  # keep-alive: 첫 연결 하나로 끝까지
    else:
        t0 = time.perf_counter()
        for _ in range(N):
            # with 블록을 매 요청 새로 열고 닫는다 = 연결도 매번 새로
            with httpx.Client(verify=verify) as client:
                client.get(f"{BASE}/ping").raise_for_status()
        latencies_total = time.perf_counter() - t0
        conns = N  # 매 요청 새 TLS 연결
    return latencies_total, conns


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ka_"))
    (tmp / "ka_app.py").write_text(_APP_SRC, encoding="utf-8")
    cert_path, key_path = make_self_signed_cert(tmp)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "ka_app:app",
            "--port", str(PORT),
            "--ssl-certfile", str(cert_path),
            "--ssl-keyfile", str(key_path),
            "--log-level", "warning",
        ],
        cwd=str(tmp),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    verify = str(cert_path)  # 자체서명 인증서를 신뢰(로컬 전용)
    try:
        # 서버가 뜰 때까지 폴링
        for _ in range(150):
            try:
                httpx.get(f"{BASE}/ping", verify=verify, timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)
        else:
            print("서버 기동 실패")
            return 1

        print(f"작업: GET /ping 를 {N}회 순차. 로컬 HTTPS(자체서명) → 새 연결마다 TLS 핸드셰이크\n")

        # 워밍업(콜드스타트 제외)
        with httpx.Client(verify=verify) as c:
            for _ in range(5):
                c.get(f"{BASE}/ping")

        t_new, c_new = bench(reuse=False, verify=verify)
        t_reuse, c_reuse = bench(reuse=True, verify=verify)

        print(f"{'매 요청 새 Client (재사용 X)':>34} : {t_new*1000:8.1f} ms   (TLS 핸드셰이크 {c_new}회)")
        print(f"{'Client 하나 재사용 (keep-alive)':>34} : {t_reuse*1000:8.1f} ms   (TLS 핸드셰이크 {c_reuse}회)")
        print(f"\n재사용이 약 {t_new/t_reuse:.1f}배 빠름 — 차이는 순수하게 '연결 재수립(TLS) 비용'.")
        print("SQL 없이 SELECT 1 만 비교한 7장 (a) 의 HTTP 판본이다.")
        return 0
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
