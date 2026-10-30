# =============================================================================
# DOCKERFILE - AI Box: Sidewalk Encroachment (SIDEWALK_ENCROACHMENT)
# Base Image: PyTorch 2.5.1 + CUDA 12.1 + cuDNN 9 (NVIDIA GPU Acceleration)
# =============================================================================

FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

# Thiết lập thư mục làm việc bên trong container
WORKDIR /app

# Biến môi trường hệ thống
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Ho_Chi_Minh \
    INFERENCE_DEVICE=cuda:0 \
    USE_HALF_PRECISION=true

# Cài đặt các gói hệ thống cần thiết cho OpenCV, FFMPEG và video streaming
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt thư viện Python (PyTorch + CUDA đã tích hợp sẵn trong Base Image)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ mã nguồn dự án vào container
COPY . .

# Tạo các thư mục lưu trữ dữ liệu
RUN mkdir -p /app/data /app/models /app/logs

# Lệnh khởi chạy chính
CMD ["python", "main.py"]
