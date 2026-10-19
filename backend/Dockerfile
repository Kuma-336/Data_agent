FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir --retries 10 --timeout 120 .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
COPY tests ./tests
RUN pip install --no-cache-dir --retries 10 --timeout 120 ".[dev]"
CMD ["pytest", "-q"]

# Editable, so a mounted ./backend/app is the code that actually runs and the
# source does not have to be rebuilt into the image on every edit.
FROM runtime AS dev
COPY tests ./tests
RUN pip install --no-cache-dir --retries 10 --timeout 120 -e ".[dev]"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/app"]

# Keep the production Uvicorn command as the default Docker build target.
FROM runtime AS production
