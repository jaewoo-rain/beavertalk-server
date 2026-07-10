"""15 (c) HTTP/1.1 vs HTTP/2 멀티플렉싱 — 한 연결에 여러 요청을 동시에.

HTTP/2 는 하나의 TCP(+TLS) 연결 위에서 여러 요청을 '스트림'으로 동시에 실어
나른다(멀티플렉싱). HTTP/1.1 은 한 연결에 한 요청씩이라, 동시 처리를 하려면
연결을 여러 개 열어야 한다(연결마다 TLS 핸드셰이크).

로컬에서 진짜로 확인한다:
  - 서버: hypercorn 이 TLS(ALPN)로 HTTP/2 를 제공(uvicorn 은 h2 미지원).
  - /slow 는 0.1s 비동기 대기(동시성이 드러나게).
  - 클라 A: httpx http2=False → HTTP/1.1. 동시 50요청이 소수 연결로 직렬화.
  - 클라 B: httpx http2=True  → HTTP/2. 동시 50요청이 '한 연결'에서 멀티플렉싱.

⚠️ 대상은 반드시 로컬(127.0.0.1). 원격/프로덕션 금지.

실행:
    uv run --with fastapi --with hypercorn --with 'httpx[http2]' --with cryptography python 15_http2_multiplex.py
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")

PORT = 8157
BASE = f"https://127.0.0.1:{PORT}"
CONCURRENCY = 50
SLOW_S = 0.1

_APP_SRC = """
import asyncio
from fastapi import FastAPI
app = FastAPI()

@app.get("/slow")
async def slow():
    await asyncio.sleep(0.1)   # 비동기 대기 → 동시에 여러 요청 처리 가능
    return {"ok": True}
"""


def make_self_signed_cert(dst_dir: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = dst_dir / "key.pem", dst_dir / "cert.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


async def run_batch(http2: bool, verify: str) -> tuple[float, str]:
    """동시 CONCURRENCY 요청. 총 소요(초)와 협상된 HTTP 버전을 돌려준다."""
    # HTTP/1.1 은 연결당 1요청이라 동시성만큼 연결을 허용해줘야 공정 비교
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(http2=http2, verify=verify, limits=limits, timeout=30.0) as client:
        r0 = await client.get(f"{BASE}/slow")  # 워밍업 겸 버전 확인
        version = r0.http_version
        t0 = time.perf_counter()
        results = await asyncio.gather(*(client.get(f"{BASE}/slow") for _ in range(CONCURRENCY)))
        dt = time.perf_counter() - t0
        assert all(r.status_code == 200 for r in results)
    return dt, version


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h2_"))
    (tmp / "ka_app.py").write_text(_APP_SRC, encoding="utf-8")
    cert_path, key_path = make_self_signed_cert(tmp)
    verify = str(cert_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "hypercorn", "ka_app:app",
         "--bind", f"127.0.0.1:{PORT}",
         "--certfile", str(cert_path), "--keyfile", str(key_path),
         "--log-level", "warning"],
        cwd=str(tmp),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        for _ in range(150):
            try:
                httpx.get(f"{BASE}/slow", verify=verify, timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)
        else:
            print("서버 기동 실패")
            return 1

        print(f"작업: /slow(0.1s 대기) 를 동시에 {CONCURRENCY}개. 로컬 HTTPS(hypercorn).\n")

        t_h1, v_h1 = asyncio.run(run_batch(http2=False, verify=verify))
        t_h2, v_h2 = asyncio.run(run_batch(http2=True, verify=verify))

        print(f"{'HTTP/1.1 (연결 여러 개)':>28} : {t_h1*1000:8.1f} ms   협상버전={v_h1}")
        print(f"{'HTTP/2 (한 연결 멀티플렉싱)':>28} : {t_h2*1000:8.1f} ms   협상버전={v_h2}")
        print(f"\n이상적으로 둘 다 ~{SLOW_S*1000:.0f}ms 에 수렴(모두 동시). 차이는 "
              "'연결 수립 개수'에서 온다:")
        print(f"  HTTP/1.1 = {CONCURRENCY}개 연결(각 TLS 핸드셰이크) / HTTP/2 = 1개 연결에 {CONCURRENCY}개 스트림.")
        return 0
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
