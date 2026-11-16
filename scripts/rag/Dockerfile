# Изолированная подготовка PDF/OCR; локальная LLM не устанавливается.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'pymupdf>=1.24,<2'
COPY scripts/rag/extract.py /extract.py
ENTRYPOINT ["python", "-u", "/extract.py"]
