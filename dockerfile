# Builder Stage (Install Dependencies)
FROM python:3.11 AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Final Image (Copy Dependencies & App Code)
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY ./src /app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
