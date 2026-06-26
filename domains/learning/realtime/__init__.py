"""normalcall realtime — 실시간 음성통화 WS(유일한 async 섬).

ws_router(transport/인증) · call_session(오케스트레이션) · protocol(WS 메시지).
DB 접근은 normalcall_service(동기) 를 run_db 로 감싸 호출한다.
"""
