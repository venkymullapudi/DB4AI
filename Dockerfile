FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY db4ai ./db4ai
RUN pip install --no-cache-dir ".[s3]"
ENV DB4AI_BLOB_URL=file:///data/blobs
EXPOSE 8000
CMD ["db4ai", "serve"]
