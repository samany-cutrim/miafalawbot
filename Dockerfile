FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpoppler-cpp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# 4 workers = até ~40 requisições simultâneas sem problema
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "asyncio", "--timeout-keep-alive", "30"]
