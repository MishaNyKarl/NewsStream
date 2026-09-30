FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_PROGRESS_BAR=off
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt && pip install -r /tmp/requirements.txt && useradd --uid 10001 --create-home newswatch
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
USER newswatch
CMD ["python", "-m", "app.main", "bot"]
