FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create temp directory for videos
RUN mkdir -p temp_videos

# Expose port
EXPOSE 8000

# Run the application
CMD gunicorn --bind 0.0.0.0:$PORT app:app
