FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg: 표현 TTS(PCM→MP3)·복습 녹음(WAV→MP3) 인코딩용. 없으면 WAV 로 폴백되지만
# MP3 로 저장하려면 필요. 의존성 레이어 앞에 둬서 캐시 효율 유지.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 복사 → 레이어 캐시로 빌드 빨라짐
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사
COPY . .

# Cloud Run 은 $PORT(기본 8080)로 트래픽을 보낸다. 반드시 0.0.0.0 바인딩.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}