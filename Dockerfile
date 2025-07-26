# Use a slim Python image based on Debian Bookworm (current stable)
FROM python:3.11-slim-bookworm

# Install system dependencies needed for ffmpeg and yt-dlp
# Installing 'ffmpeg' should pull in its necessary encoder libraries like libx264, libx265, libopus, libvpx.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    build-essential \
    curl \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* # Clean up apt cache to keep image size small

# Set the working directory inside the container
WORKDIR /app

# Copy your Python application file into the container
COPY app.py /app/app.py

# Install Python dependencies: FastAPI, Uvicorn, python-telegram-bot, yt-dlp, aiofiles, uvloop
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-telegram-bot==20.* \
    yt-dlp \
    aiofiles \
    uvloop \
    && python -m pip install --upgrade pip

# Set environment variables for non-buffered Python output, important for logging
ENV PYTHONUNBUFFERED=1

# Command to run the FastAPI application using Uvicorn.
# CRITICAL FIX: Listen on the PORT environment variable provided by Cloud Run.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "${PORT}", "--log-level", "info"]
