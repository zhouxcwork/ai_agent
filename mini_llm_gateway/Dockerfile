FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_CONFIG=/app/config.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mini_llm_gateway ./mini_llm_gateway
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R 10001:10001 /app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "mini_llm_gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
