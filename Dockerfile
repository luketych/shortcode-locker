FROM ghcr.io/astral-sh/uv:python3.12-alpine

WORKDIR /app

COPY pyproject.toml uv.lock .python-version README.md app.py /app/
COPY data /app/data

RUN uv sync --frozen --no-dev \
  && mkdir -p /data \
  && cp /app/data/codes.json /data/codes.json \
  && cp /app/data/config.json /data/config.json

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    SHORTCODE_LOCKER_DATA=/data/codes.json

EXPOSE 8765
CMD ["shortcode-locker", "serve"]
