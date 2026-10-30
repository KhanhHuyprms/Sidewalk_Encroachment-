"""
config.py - Cấu hình tập trung cho module Lấn chiếm vỉa hè.
Đọc từ biến môi trường / file .env.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Tìm và nạp file .env ở thư mục hiện tại hoặc config/.env hoặc thư mục gốc dự án
_env_locations = [
    Path.cwd() / ".env",
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent.parent.parent / ".env",
]
for p in _env_locations:
    if p.exists():
        load_dotenv(p)
        break
else:
    load_dotenv()

# ── MQTT ──────────────────────────────────────────────────────────
MQTT_BROKER      = os.getenv("MQTT_BROKER", "aioc-stg.atin.vn")
MQTT_PORT        = int(os.getenv("MQTT_PORT", "443"))
MQTT_USERNAME    = os.getenv("MQTT_USERNAME", "atin")
MQTT_PASSWORD    = os.getenv("MQTT_PASSWORD", "Atin@2026#")
MQTT_CLIENT_ID   = os.getenv("MQTT_CLIENT_ID", "tts_KhanhHuy_05")

# ── VMS / Organization ───────────────────────────────────────────
ORGANIZATION_ID  = int(os.getenv("ORGANIZATION_ID", "103"))
ZONE_ID          = int(os.getenv("ZONE_ID", "51"))

# Camera mục tiêu duy nhất của dự án
CAMERA_CODE      = os.getenv("CAMERA_CODE", "cam_sidewalk")
CAMERA_ID        = os.getenv("CAMERA_ID", "699bc4c0-2f06-4671-8615-321278d86d5c")

# ── Storage API ──────────────────────────────────────────────────
STORAGE_API_URL  = os.getenv("STORAGE_API_URL",
                             "https://aioc-stg.atin.vn/storage/upload/file")

# ── AI Module ────────────────────────────────────────────────────
AI_MODULE        = "SIDEWALK_ENCROACHMENT"

# ── Detection settings ──────────────────────────────────────────
# Tự động ưu tiên best.pt nếu có trong models/best.pt hoặc best.pt
_default_model = "yolov8n.pt"
for candidate in [
    Path(__file__).resolve().parent.parent / "models" / "best.pt",
    Path(__file__).resolve().parent.parent / "best.pt",
    Path.cwd() / "models" / "best.pt",
    Path.cwd() / "best.pt",
]:
    if candidate.exists():
        _default_model = str(candidate)
        break

YOLO_MODEL              = os.getenv("YOLO_MODEL", _default_model)
CONFIDENCE_THRESHOLD    = float(os.getenv("CONFIDENCE_THRESHOLD", "0.50"))
YOLO_IMGSZ              = int(os.getenv("YOLO_IMGSZ", "640"))

# ── Thiết bị phần cứng suy luận (CUDA / GPU) ─────────────────────
try:
    import torch
    _cuda_available = torch.cuda.is_available() and torch.cuda.device_count() > 0
except Exception:
    _cuda_available = False

DEFAULT_DEVICE          = "0" if _cuda_available else "cpu"
INFERENCE_DEVICE        = os.getenv("INFERENCE_DEVICE", DEFAULT_DEVICE)
USE_HALF_PRECISION      = os.getenv("USE_HALF_PRECISION", "true").lower() in ("true", "1", "yes")

# Thời gian (giây) vật cản phải nằm trong vùng vỉa hè mới tính là lấn chiếm
ENCROACHMENT_TIME_THRESHOLD = int(os.getenv("ENCROACHMENT_TIME_THRESHOLD", "10"))

# Cooldown (giây) giữa hai sự kiện cùng loại cho cùng camera
EVENT_COOLDOWN          = int(os.getenv("EVENT_COOLDOWN", "60"))

# Khoảng cách gửi bbox (giây) - tối ưu 0.08s (12 FPS) để triệt tiêu độ trễ hiển thị
BBOX_PUBLISH_INTERVAL   = float(os.getenv("BBOX_PUBLISH_INTERVAL", "0.08"))

# Chỉ hiển thị BBox cho các vật thể trong vùng vỉa hè (loại bỏ khung ảo xe chạy ngoài đường bị trôi/trễ)
BBOX_ONLY_IN_ZONE       = os.getenv("BBOX_ONLY_IN_ZONE", "true").lower() in ("true", "1", "yes")

# ── MQTT Topics ──────────────────────────────────────────────────
TOPIC_CAMERAS_ZONE      = f"smart_vms/cameras/zone/{ZONE_ID}"
TOPIC_CAMERAS_GETDEVICE = "smart_vms/cameras/getdevice"
TOPIC_AI_EVENTS         = f"smart_vms/ai_events/{AI_MODULE}"
TOPIC_AI_ZONES_SUFFIX   = f"ai_zones/{AI_MODULE}"

def topic_bbox(camera_code: str) -> str:
    return f"smart_vms/ai/bbox/{camera_code}"

def topic_ai_zones(camera_code: str) -> str:
    return f"smart_vms/cameras/{camera_code}/ai_zones/{AI_MODULE}"

# ── YOLO classes quan tâm ──────────────────────────────────────────
# Các lớp đối tượng mục tiêu giám sát lòng đường / vỉa hè:
TARGET_OBSTACLE_CLASSES = {
    "ad_board",     # Biển quảng cáo / bảng hiệu
    "bin",          # Thùng rác
    "car",          # Xe ô tô (bao gồm car, bus, truck)
    "motorbike",    # Xe máy / xe đạp
    "table_chair",  # Bàn ghế (quán ăn, cà phê vỉa hè)
    "umbrella",     # Dù, ô che nắng
    "vendor_cart",  # Xe đẩy, gánh hàng rong
    "object",       # Đồ vật / chướng ngại vật (model best.pt)
    "bicycle",      # Xe đạp (model best.pt)
}

# Tập hợp tất cả các lớp coi là vật cản lấn chiếm (bao gồm target + alias COCO)
OBSTACLE_CLASSES = TARGET_OBSTACLE_CLASSES | {
    "motorcycle", "bicycle", "truck", "bus", "bench", "chair",
    "dining table", "couch", "potted plant", "suitcase", "backpack", "handbag",
    "object",
}

# Cấu hình lớp cho model chuyên biệt Sidewalk (best.pt)
# 0: bicycle, 1: car, 2: motorbike, 3: object, 4: parking_zone, 5: person, 6: road, 7: sidewalk
# Nhận diện: [0, 1, 2, 3, 5] (bỏ qua 4: parking_zone, 6: road, 7: sidewalk)
# Trong khu vực (vỉa hè), lấn chiếm gồm xe (bicycle, car, motorbike) và object
SIDEWALK_MODEL_CLASSES = {
    0: "bicycle",
    1: "car",
    2: "motorbike",
    3: "object",
    4: "parking_zone",
    5: "person",
    6: "road",
    7: "sidewalk",
}
SIDEWALK_ACTIVE_CLASSES = [0, 1, 2, 3, 5]


# Mapping COCO class id -> tên lớp chuẩn hoá (khi dùng model COCO mặc định)
COCO_CLASS_MAPPING = {
    0: "person",          # Người (hiển thị bbox, không tính lấn chiếm)
    1: "motorbike",       # bicycle -> motorbike
    2: "car",             # car -> car
    3: "motorbike",       # motorcycle -> motorbike
    5: "car",             # bus -> car
    7: "car",             # truck -> car
    13: "table_chair",    # bench -> table_chair
    24: "backpack",       # ba lô
    25: "umbrella",       # dù / ô
    26: "handbag",        # túi xách
    28: "suitcase",       # vali
    56: "table_chair",    # chair -> table_chair
    57: "table_chair",    # couch -> table_chair
    58: "potted plant",   # chậu cây
    60: "table_chair",    # dining table -> table_chair
}

# Giữ tương thích ngược với code cũ
COCO_OBSTACLE_IDS = COCO_CLASS_MAPPING

