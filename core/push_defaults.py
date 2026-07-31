"""수신통화 푸시의 표시 기본값.

세 곳(dispatch_service·fcm·apns)이 각자 폴백 문구를 들고 있으면 하나만 고쳤을 때
조용히 어긋난다 — 여기 한 곳에서만 정한다.

⚠ **한국어를 넣지 말 것.** 앱은 30개 로케일을 지원하고, 이 문구는 사용자 단말의
잠금화면(CallKit/전화 UI)에 그대로 뜬다. 한국어를 모르는 학습자에게 한글 발신자명이
뜨는 건 그 자체가 버그다. 서버는 사용자의 로케일을 푸시 시점에 모르므로(단말이
어떤 언어로 켜져 있는지 알 수 없다) **로케일 중립인 브랜드명**으로 둔다.

TODO: 앱이 캐릭터 이름 없는 페이로드를 받았을 때 자기 로케일 문구로 폴백하도록
바꾸면, 서버는 이름을 비워 보내고 이 상수를 지울 수 있다.
"""

from __future__ import annotations

# 캐릭터를 특정하지 못했을 때 쓰는 발신자명. 실제로 나올 일은 거의 없다 —
# character.name 은 NOT NULL 이고 alarm.character_id 도 NOT NULL(RESTRICT) 이라
# 3중 방어의 마지막 겹이다.
DEFAULT_CALLER_NAME = "BeaverTalk"

# CallKit 통화 목록에 뜨는 부제(handle). 통화 상대의 "번호" 자리에 들어간다.
DEFAULT_CALL_HANDLE = "BeaverTalk"
