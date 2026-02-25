ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache util-linux bash coreutils findutils

WORKDIR /app

COPY requirements.txt ./
RUN pip3 install --break-system-packages -r requirements.txt

COPY app ./app

EXPOSE 9123

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9123"]
