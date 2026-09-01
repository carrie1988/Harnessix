FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HARNESSIX_HOST=0.0.0.0 \
    HARNESSIX_PORT=8787 \
    HARNESSIX_DATABASE_PATH=/data/harnessix.db \
    HARNESSIX_DEMO_DATABASE_PATH=/data/demo-external.db

RUN useradd --create-home --uid 10001 harnessix
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir /data && chown harnessix:harnessix /data
USER harnessix
EXPOSE 8787
VOLUME ["/data"]
ENTRYPOINT ["harnessix"]
CMD ["serve"]
