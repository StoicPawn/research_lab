FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 runner && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin runner

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

RUN mkdir -p /data/workspaces && chmod 0755 /data /data/workspaces

EXPOSE 8300
CMD ["research-lab"]
