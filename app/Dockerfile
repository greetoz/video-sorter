FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart smbprotocol pypsrp requests av numpy tzdata pillow opencv-python-headless
# face detector (OpenCV YuNet) used to vet the banner pictures; without it the built-in Haar cascade is used
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx', '/app/yunet.onnx')" || echo "YuNet download failed, Haar fallback will be used"
COPY sorter /app/sorter
COPY app /app/app
# caches + history of the manual run, copied into the data volume on first start only
COPY seed /seed
# state lives in the /data volume; the sorter modules expect ../cache and ../manifest
RUN mkdir -p /data/cache /data/manifest && ln -s /data/cache /app/cache && ln -s /data/manifest /app/manifest
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
