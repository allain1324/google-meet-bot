#!/usr/bin/env bash
set -euo pipefail

# Start Xvfb (màn hình ảo)
X_RES="${REC_WIDTH:-1366}"
Y_RES="${REC_HEIGHT:-768}"
Xvfb :99 -screen 0 ${X_RES}x${Y_RES}x24 -ac &
sleep 0.5

# Start PulseAudio (để ffmpeg có đầu vào audio nếu bạn cấu hình)
pulseaudio --start --exit-idle-time=-1 || true

# In phiên bản Chrome để debug
google-chrome --version || true
python --version

# Chạy Django
exec python manage.py runserver 0.0.0.0:8000
