FROM python:3.11-slim

RUN pip install --no-cache-dir boto3

WORKDIR /app
COPY athena_query.py .

# Non-root for good measure.
USER 1000

ENTRYPOINT ["python", "athena_query.py"]
