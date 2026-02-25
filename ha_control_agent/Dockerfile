FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache util-linux bash coreutils findutils git
RUN apk add --no-cache nodejs npm
RUN apk add --no-cache stunnel
RUN apk add --no-cache samba
RUN npm install -g --unsafe-perm @openai/codex
RUN pip install pipx

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY default_skills ./default_skills
COPY ha_control_cli_bundle ./ha_control_cli_bundle
COPY launcher.py ./launcher.py
RUN pip install ./ha_control_cli_bundle

EXPOSE 9123

CMD ["python", "launcher.py"]
