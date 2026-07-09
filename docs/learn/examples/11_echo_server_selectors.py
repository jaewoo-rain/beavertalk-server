"""11 (b) 한 스레드가 selectors 로 여러 소켓을 '동시에' 다루는 미니 에코 서버.

이벤트 루프(2장)의 심장이 이것이다. 서버 스레드는 딱 하나. 그 한 스레드가
`selector.select()` 한 번으로 "지금 읽을 게 준비된 소켓들"을 한꺼번에 받아
차례로 처리한다. 스레드를 클라 수만큼 만들지 않는다 — 소켓(fd) 여러 개를
selector 한 명이 감시한다.

구성(모두 한 프로세스 안):
  - 서버: 백그라운드 스레드 1개가 selector 루프를 돈다(논블로킹 소켓).
  - 클라: 메인 스레드가 소켓 3개를 붙여, 서로 다른 타이밍에 메시지를 보낸다.
서버 로그의 fd 번호가 클라들 사이를 오가는 걸 보면, 한 루프가 여러 연결을
번갈아(문맥교환 없이!) 처리하는 게 보인다.

Windows 라 백엔드는 select 지만, 코드는 그대로 리눅스에서 epoll 로 돈다.

실행: python 11_echo_server_selectors.py
"""

from __future__ import annotations

import selectors
import socket
import threading
import time

HOST = "127.0.0.1"
N_CLIENTS = 3
MSGS_PER_CLIENT = 3


def server(ready: threading.Event, port_box: dict, stop: threading.Event) -> None:
    sel = selectors.DefaultSelector()
    print(f"[server] selector 백엔드 = {type(sel).__name__}")

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((HOST, 0))
    lsock.listen()
    lsock.setblocking(False)
    port_box["port"] = lsock.getsockname()[1]
    sel.register(lsock, selectors.EVENT_READ, data=None)  # data=None → 리슨 소켓 표시
    ready.set()

    handled = 0
    open_conns = 0
    while not stop.is_set():
        events = sel.select(timeout=0.2)  # 준비된 fd 만 돌려준다(없으면 0.2s 대기)
        for key, _mask in events:
            if key.data is None:
                # 리슨 소켓: 새 연결 수락
                conn, addr = lsock.accept()
                conn.setblocking(False)
                sel.register(conn, selectors.EVENT_READ, data=addr)
                open_conns += 1
                print(f"[server] accept fd={conn.fileno():<3d} (열린 연결 {open_conns}개)")
            else:
                conn = key.fileobj
                try:
                    data = conn.recv(1024)
                except OSError:
                    data = b""
                if data:
                    handled += 1
                    print(f"[server]   loop 처리: fd={conn.fileno():<3d} 에코 {data!r}")
                    conn.sendall(data)  # 에코
                else:
                    print(f"[server] close  fd={conn.fileno():<3d}")
                    sel.unregister(conn)
                    conn.close()
                    open_conns -= 1
    lsock.close()
    sel.close()
    print(f"[server] 종료. 한 스레드가 처리한 메시지 총 {handled}건")


def client(cid: int, port: int) -> None:
    s = socket.create_connection((HOST, port))
    for i in range(MSGS_PER_CLIENT):
        # 클라마다 다른 리듬으로 보내 서로 겹치게(interleave) 만든다
        time.sleep(0.05 * (cid + 1))
        msg = f"c{cid}-m{i}".encode()
        s.sendall(msg)
        reply = s.recv(1024)
        print(f"[client {cid}] 보냄 {msg!r} → 받음 {reply!r}")
    s.close()


def main() -> None:
    ready = threading.Event()
    stop = threading.Event()
    port_box: dict = {}
    th = threading.Thread(target=server, args=(ready, port_box, stop), daemon=True)
    th.start()
    ready.wait()
    port = port_box["port"]
    print(f"[main] 서버 준비됨 port={port}, 클라 {N_CLIENTS}개 접속\n")

    cts = [threading.Thread(target=client, args=(c, port)) for c in range(N_CLIENTS)]
    for t in cts:
        t.start()
    for t in cts:
        t.join()

    time.sleep(0.3)  # 서버가 close 이벤트까지 처리할 시간
    stop.set()
    th.join(timeout=2)


if __name__ == "__main__":
    main()
