# Dockerfile
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_ALLOWED_HOSTS='localhost 127.0.0.1 [::1]' \
    DISPLAY=:99

# Gói hệ thống cần thiết cho Chrome + Xvfb + ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg ca-certificates curl wget unzip fonts-liberation \
    libasound2 libnss3 libx11-6 libxkbcommon0 libxrandr2 libxdamage1 libxcomposite1 \
    libgbm1 libgtk-3-0 libatk1.0-0 libatk-bridge2.0-0 libxfixes3 libdrm2 \
    xvfb pulseaudio dbus-x11 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Cài Google Chrome stable
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# Tạo app dir
WORKDIR /var/app

# Copy & cài Python deps trước để tối ưu cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Thư mục lưu profile (persist qua volume)
RUN mkdir -p /var/app/profiles /var/app/recordings

# Entrypoint: khởi động Xvfb + PulseAudio rồi chạy Django
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9000
CMD ["/entrypoint.sh"]
