FROM python:3.12-slim
RUN apt-get update && apt-get install -y \
    ffmpeg curl rsync openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN mkdir -p /downloads /root/.ssh && chmod 700 /root/.ssh
EXPOSE 5000
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--timeout", "600", "--bind", "0.0.0.0:5000", "app:app"]
