ARG PYTHON_BASE_IMAGE=python:3.12-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=5
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=5

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES} \
    REFACTOR_AGENT_RUN_ROOT=/data/runs \
    REFACTOR_AGENT_DATABASE=/data/refactor_agent.sqlite \
    REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT=/data/github-workspaces

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ".[dev,dashboard]" \
    && mkdir -p /data/runs /data/github-workspaces

VOLUME ["/data"]

ENTRYPOINT ["refactor-agent"]
CMD ["memories", "--limit", "20"]
