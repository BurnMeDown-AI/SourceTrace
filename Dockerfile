# SourceTrace with everything it can use: poppler for PDFs, tesseract for scanned PDFs,
# and Python 3.14 for zstd-compressed archive pages.
#   docker build -t sourcetrace .
#   docker run --rm sourcetrace "47% of adults met the guidelines" https://example.com/article
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE sourcetrace.py ./
RUN pip install --no-cache-dir . && useradd --create-home tracer
USER tracer
ENTRYPOINT ["sourcetrace"]
