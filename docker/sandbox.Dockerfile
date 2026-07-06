FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace

RUN python -m pip install --no-cache-dir pytest
