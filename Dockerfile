FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성 먼저 복사 → 레이어 캐시로 빌드 빨라짐
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사
COPY . .

# Cloud Run 은 $PORT(기본 8080)로 트래픽을 보낸다. 반드시 0.0.0.0 바인딩.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}