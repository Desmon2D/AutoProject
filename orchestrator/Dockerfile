FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLUGIN_ROOT=/app/plugins \
    SKILL_ROOT=/app/skills \
    CAPABILITY_ROOT=/app/capabilities \
    IMAGE_ROOT=/app/images \
    SCENARIO_ROOT=/app/scenarios \
    JOBS_ROOT=/data/jobs

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --editable .

COPY plugins ./plugins
COPY skills ./skills
COPY capabilities ./capabilities
COPY images ./images
COPY scenarios ./scenarios
COPY tests ./tests

RUN groupadd --gid 10001 orchestrator \
    && useradd --uid 10001 --gid orchestrator --create-home orchestrator \
    && mkdir -p /data/jobs \
    && chown -R orchestrator:orchestrator /data

USER orchestrator

EXPOSE 8080
CMD ["uvicorn", "automation_orchestrator.api:app", "--host", "0.0.0.0", "--port", "8080"]
