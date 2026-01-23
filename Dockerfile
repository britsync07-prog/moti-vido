FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# Install FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python Dependencies
# Combining them to reduce layers
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    python-multipart \
    faster-whisper \
    requests \
    streamlit \
    watchdog \
    playwright==1.49.0 \
    sqlalchemy \
    psycopg2-binary

# Copy project files
COPY . .

# Expose ports (Documentary only, Docker Compose handles mapping)
EXPOSE 8000 8501

# Default command (can be overridden)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
