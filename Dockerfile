# syntax=docker/dockerfile:1
# GridWise Scheduler - single-process API image. No secrets are baked in: pass keys at runtime.

FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir uv==0.11.26
WORKDIR /srv
# Dependencies first: this layer is rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runner
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    PATH="/srv/.venv/bin:$PATH"
RUN useradd --create-home --uid 10001 app
WORKDIR /srv
COPY --from=builder --chown=app:app /srv/.venv /srv/.venv
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=2).status == 200 else 1)"
# Binds 0.0.0.0:$PORT, one worker (see app/__main__.py).
CMD ["python", "-m", "app"]
