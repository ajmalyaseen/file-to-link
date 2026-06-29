# Dockerfile for Koyeb (and any container host).
# Includes ffmpeg/ffprobe, which buildpacks don't provide.
FROM python:3.12-slim

# ffmpeg + ffprobe for the merge feature.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb injects $PORT; the app reads it (config.HTTP_PORT) and binds 0.0.0.0.
EXPOSE 8080

CMD ["python", "bot.py"]
