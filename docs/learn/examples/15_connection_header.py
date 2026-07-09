"""15 (b) keep-alive 를 끄는 두 가지 방법 — 같은 Client 를 재사용해도 느려진다.

15_keepalive_reuse.py 는 'Client 를 재사용하면 빠르다'를 봤다. 여기서는
'Client 를 재사용해도 keep-alive 가 꺼지면 도로 느려진다'를 두 경우로 확인한다.

  (1) 기본 재사용                → 연결 1개로 keep-alive. 빠름(기준선).
  (2) Connection: close 헤더      → 서버가 응답 후 매번 연결을 닫음 → 매 요청 새 TLS.
  (3) Limits(max_keepalive=0)     → httpx 풀이 유휴 연결을 안 남김 → 매 요청 새 TLS.

즉 keep-alive 이득은 '클라가 연결을 재사용할 수 있어야' 나온다. 헤더/설정 하나로
쉽게 무력화된다. (프록시 뒤 keep-alive 불일치도 같은 원리 — 6절 함정.)

⚠️ 대상은 반드시 로컬(127.0.0.1). 원격/프로덕션 금지.

실행:
    uv run --with fastapi --with uvicorn --with httpx --with cryptography python 15_connection_header.py
"""

from __future__ import annotations

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

PORT = 8156
BASE = f"https://127.0.0.1:{PORT}"
N = 150

_APP_SRC = """
from fastapi import FastAPI
app = FastAPI()

@app.get("/ping")
def ping():
    return {"ok": True}
"""


def make_self_signed_cert(dst_dir: Path) -> tuple[Path, Path]:
    """127.0.0.1 용 자체서명 인증서/키 생성."""
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


def bench(client: httpx.Client, headers: dict | None = None) -> float:
    """같은 client 로 N회 순차 요청. 시간(초)."""
    t0 = time.perf_counter()
    for _ in range(N):
        client.get(f"{BASE}/ping", headers=headers).raise_for_status()
    return time.perf_counter() - t0


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ka2_"))
    (tmp / "ka_app.py").write_text(_APP_SRC, encoding="utf-8")
    cert_path, key_path = make_self_signed_cert(tmp)
    verify = str(cert_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ka_app:app", "--port", str(PORT),
         "--ssl-certfile", str(cert_path), "--ssl-keyfile", str(key_path),
         "--log-level", "warning"],
        cwd=str(tmp),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        for _ in range(150):
            try:
                httpx.get(f"{BASE}/ping", verify=verify, timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)
        else:
            print("서버 기동 실패")
            return 1

        print(f"작업: 같은 서버에 GET /ping {N}회 순차. 로컬 HTTPS.\n")

        # (1) 기본 재사용 = keep-alive 켜짐
        with httpx.Client(verify=verify) as c:
            c.get(f"{BASE}/ping")  # 워밍업(첫 핸드셰이크)
            t_keep = bench(c)

        # (2) 같은 client 재사용이지만 매 요청 Connection: close
        with httpx.Client(verify=verify) as c:
            c.get(f"{BASE}/ping")
            t_close = bench(c, headers={"Connection": "close"})

        # (3) keep-alive 연결을 풀에 남기지 않도록 Limits 로 강제
        no_keep = httpx.Limits(max_keepalive_connections=0)
        with httpx.Client(verify=verify, limits=no_keep) as c:
            c.get(f"{BASE}/ping")
            t_nolimit = bench(c)

        print(f"{'(1) 기본 재사용 (keep-alive)':>36} : {t_keep*1000:8.1f} ms")
        print(f"{'(2) Connection: close 헤더':>36} : {t_close*1000:8.1f} ms")
        print(f"{'(3) Limits(max_keepalive=0)':>36} : {t_nolimit*1000:8.1f} ms")
        print(f"\nkeep-alive 를 끄면 재사용 Client 라도 (2)/(3) 는 (1) 의 "
              f"약 {t_close/t_keep:.0f}~{t_nolimit/t_keep:.0f}배로 느려진다.")
        print("=> 이득의 열쇠는 'Client 객체'가 아니라 '연결을 실제로 재사용하는가'다.")
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
