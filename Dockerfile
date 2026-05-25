FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs runtime \
    && useradd -m appuser \
    && chown -R appuser:appuser /app

# Create the directory
RUN mkdir -p /app/logs

# Change ownership of the app directory (or just the logs directory) to your user
# Replace 'appuser:appuser' with whatever username/group you created
RUN chown -R appuser:appuser /app/logs 

USER appuser
