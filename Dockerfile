FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gridwise/ ./gridwise/
COPY frontend/ ./frontend/
COPY run_samples.py ./
COPY selftest.py ./
COPY README.md ./

# Documented, fixed port for the fallback image (README shows PORT can be remapped).
EXPOSE 8000

# Readiness of the interpretation layer is checked at request time; a missing
# GEMINI_API_KEY never fails container start, so /health can be probed
# immediately. No secrets are baked into the image; all configuration is
# supplied via environment variables at runtime.
CMD ["python", "-m", "gridwise"]
