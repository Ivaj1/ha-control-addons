FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache util-linux bash coreutils findutils
RUN apk add --no-cache nodejs npm
RUN npm install -g --unsafe-perm @openai/codex

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY default_skills ./default_skills
COPY ha_control_cli_bundle ./ha_control_cli_bundle
RUN pip install ./ha_control_cli_bundle

EXPOSE 9123

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9123"]
