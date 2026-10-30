"""
pipeline.py - Pipeline chính cho module Lấn chiếm vỉa hè (SIDEWALK_ENCROACHMENT).

Tính năng:
1. Kết nối MQTT broker (WSS TLS) qua MQTTClient.
2. Nhận danh sách camera từ topic smart_vms/cameras/zone/51 (chỉ xử lý camera bật SIDEWALK_ENCROACHMENT).
3. Đăng ký & Nhận tọa độ vùng vỉa hè từ topic:
   smart_vms/cameras/{camera_code}/ai_zones/SIDEWALK_ENCROACHMENT
   - Cập nhật nóng (Hot-reload in-memory) tức thì vào detector, KHÔNG cần khởi động lại.
   - Chịu được payload rỗng (xóa polygon khi VMS gửi "" hoặc ai_zones: []).
   - Tự động lưu cấu hình vùng vỉa hè ra file: data/{camera_code}_ai_zones.json.
4. Đọc luồng RTSP (hoặc video test), áp dụng tối ưu độ trễ thấp (CAP_PROP_BUFFERSIZE=1, low_delay).
5. Chạy mô hình YOLOv8 phát hiện vật cản, kiểm tra Point-in-Polygon vỉa hè.
6. Publish BBox realtime (0.1s/lần) lên smart_vms/ai/bbox/{camera_code} (QoS 0).
7. Khi vật cản chiếm dụng > 10s: Vẽ overlay BBox lên ảnh snapshot, upload Storage API và bắn Event lên smart_vms/ai_events/SIDEWALK_ENCROACHMENT.
"""
import os
import sys
import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import cv2
import numpy as np

# Đảm bảo đường dẫn import
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR / "config") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "config"))
if str(ROOT_DIR / "utils") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "utils"))

from config import (
    AI_MODULE, ZONE_ID, ORGANIZATION_ID,
    CAMERA_CODE, CAMERA_ID,
    TOPIC_AI_EVENTS, BBOX_PUBLISH_INTERVAL,
    EVENT_COOLDOWN, ENCROACHMENT_TIME_THRESHOLD,
    BBOX_ONLY_IN_ZONE,
    topic_bbox, topic_ai_zones,
)
from mqtt_client import MQTTClient
from detector import SidewalkDetector, Detection, EncroachmentEvent
from storage import upload_snapshot

logger = logging.getLogger("pipeline")
VN_TZ = timezone(timedelta(hours=7))

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class CameraWorker:
    """Worker xử lý cho từng camera cụ thể."""

    def __init__(
        self,
        camera_id: str,
        camera_code: str,
        rtsp_url: str,
        mqtt: MQTTClient,
    ):
        self.camera_id = camera_id
        self.camera_code = camera_code
        self.rtsp_url = rtsp_url
        self.mqtt = mqtt
        self.detector = SidewalkDetector()
        self.ai_zones: list[dict] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_event_time: float = 0
        self._last_bbox_time: float = 0
        self._lock = threading.Lock()

        # Nạp sẵn polygon đã lưu trước đó nếu có (Fallback)
        self._load_cached_zones()

    def _load_cached_zones(self):
        """Tải polygon đã lưu từ trước trong thư mục data nếu có."""
        cache_file = DATA_DIR / f"{self.camera_code}_ai_zones.json"
        result_json = DATA_DIR / "Cam_05_sidewalk_result.json"
        
        target_file = cache_file if cache_file.exists() else (result_json if result_json.exists() else None)
        if target_file and target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "ai_zones" in data:
                    self.ai_zones = data.get("ai_zones", [])
                    logger.info("[%s] Đã nạp %d polygon từ cache: %s", self.camera_code, len(self.ai_zones), target_file.name)
                elif "sidewalk_zone" in data:
                    pts = data["sidewalk_zone"].get("points_normalized", [])
                    if pts:
                        self.ai_zones = [{
                            "id": "cached_zone_01",
                            "code": "SIDEWALK",
                            "name": data["sidewalk_zone"].get("name", "Vỉa hè"),
                            "is_active": True,
                            "points": pts
                        }]
                        logger.info("[%s] Đã chuyển đổi polygon từ kết quả Cam_05: %d điểm", self.camera_code, len(pts))
            except Exception as e:
                logger.warning("[%s] Không thể đọc cache polygon: %s", self.camera_code, e)

    def set_ai_zones(self, zones: list[dict]):
        """
        Cập nhật nóng (Hot-reload) polygon vùng vỉa hè từ VMS.
        - Chịu được payload rỗng (khi VMS gửi [] hoặc "" -> xóa vùng).
        - Tự động lưu cấu hình ra file JSON để duy trì bền vững.
        - Cập nhật ngay vào detector mà không cần khởi động lại worker.
        """
        with self._lock:
            self.ai_zones = zones or []
            
            # Lưu cấu hình mới ra file JSON
            zone_save_path = DATA_DIR / f"{self.camera_code}_ai_zones.json"
            try:
                save_payload = {
                    "camera_code": self.camera_code,
                    "camera_id": self.camera_id,
                    "updated_at": datetime.now(VN_TZ).isoformat(),
                    "total_zones": len(self.ai_zones),
                    "ai_zones": self.ai_zones,
                }
                with open(zone_save_path, "w", encoding="utf-8") as f:
                    json.dump(save_payload, f, indent=2, ensure_ascii=False)
                logger.info("💾 [%s] Đã lưu cập nhật vùng vỉa hè ra file: %s", self.camera_code, zone_save_path.name)
            except Exception as e:
                logger.warning("Không thể lưu cấu hình vùng ra file: %s", e)

            # Nếu worker đang chạy và đã có kích thước frame, cập nhật trực tiếp vào detector
            if self.detector._frame_w > 0 and self.detector._frame_h > 0:
                self.detector.set_polygons(self.ai_zones, self.detector._frame_w, self.detector._frame_h)
                logger.info(
                    "🔄 [%s] HOT-RELOAD THÀNH CÔNG: Đã áp dụng %d polygon mới vào Detector mà không cần restart!",
                    self.camera_code, len(self.ai_zones),
                )
            else:
                logger.info("[%s] Đã lưu %d ai_zone(s), sẽ áp dụng khi nhận frame đầu tiên.", self.camera_code, len(self.ai_zones))

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name=f"worker-{self.camera_code}", daemon=True,
        )
        self._thread.start()
        logger.info("[%s] Camera Worker started", self.camera_code)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[%s] Camera Worker stopped", self.camera_code)

    def _run(self):
        """Vòng lặp xử lý video thời gian thực (Low Latency)."""
        # Cấu hình FFMPEG giảm độ trễ tối đa cho RTSP (nobuffer, low_delay)
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000"

        # Nếu RTSP không kết nối được hoặc là video test, tự động fallback sang Cam_05.mp4
        local_video = DATA_DIR / "Cam_05.mp4"
        stream_source = self.rtsp_url

        while self._running:
            logger.info("[%s] Đang mở luồng video: %s", self.camera_code, stream_source)
            cap = cv2.VideoCapture(stream_source, cv2.CAP_FFMPEG if "rtsp://" in str(stream_source) else cv2.CAP_ANY)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Buffer 1 frame để không bị tích lũy delay

            if not cap.isOpened():
                if stream_source != str(local_video) and local_video.exists():
                    logger.warning("[%s] Không mở được RTSP (%s), chuyển sang file video nội bộ: %s", self.camera_code, stream_source, local_video.name)
                    stream_source = str(local_video)
                    continue
                else:
                    logger.warning("[%s] Không thể mở luồng video, thử lại sau 5s...", self.camera_code)
                    time.sleep(5)
                    continue

            logger.info("[%s] Luồng video đã kết nối thành công (Low Latency Mode)", self.camera_code)
            frame_count = 0

            try:
                while self._running and cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        # Nếu là video file thì tua lại đầu để chạy liên tục
                        if stream_source == str(local_video):
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            continue
                        logger.warning("[%s] Mất tín hiệu luồng video, đang kết nối lại...", self.camera_code)
                        break

                    frame_count += 1
                    h, w = frame.shape[:2]

                    # Áp dụng polygons nếu chưa set hoặc frame size thay đổi
                    if (not self.detector.polygons or self.detector._frame_w != w) and self.ai_zones:
                        with self._lock:
                            self.detector.set_polygons(self.ai_zones, w, h)

                    # Xử lý mỗi 2 frame để giảm tải CPU và tăng độ mượt
                    if frame_count % 2 != 0:
                        continue

                    # 1. Chạy YOLO Detection
                    detections = self.detector.detect(frame)

                    # 2. Publish BBox realtime lên VMS (QoS 0, Low Latency ~10 FPS)
                    now = time.time()
                    if now - self._last_bbox_time >= BBOX_PUBLISH_INTERVAL:
                        self._publish_bbox(detections, now)
                        self._last_bbox_time = now

                    # 3. Cập nhật Tracker & Kiểm tra sự kiện lấn chiếm vỉa hè
                    events = self.detector.update_tracker(detections, frame, current_time=now)
                    for evt in events:
                        self._handle_event(evt)

            except Exception:
                logger.exception("[%s] Lỗi trong worker loop", self.camera_code)
            finally:
                cap.release()

            if self._running:
                time.sleep(2)

    def _publish_bbox(self, detections: list[Detection], timestamp: float):
        """Bắn BBox realtime lên topic smart_vms/ai/bbox/{camera_code}."""
        if not detections:
            return

        det_list = []
        violation_count = 0
        for det in detections:
            # Nếu bật BBOX_ONLY_IN_ZONE, chỉ vẽ box cho các vật thể nằm trong vùng vỉa hè
            # Triệt tiêu hoàn toàn hiện tượng xe chạy ngoài đường bị lệch/trễ BBox
            if BBOX_ONLY_IN_ZONE and not det.in_polygon:
                continue

            d = {
                "id": f"obj_{det.track_id}",
                "class": det.class_name,
                "confidence": round(det.confidence, 3),
                "bbox": det.bbox_norm,
                "track_id": det.track_id,
            }
            if det.in_polygon and det.class_name != "person":
                violation_count += 1
            det_list.append(d)

        payload = {
            "camera_code": self.camera_code,
            "camera_id": self.camera_id,
            "organization_id": ORGANIZATION_ID,
            "ai_modules": AI_MODULE,
            "timestamp": timestamp,
            "detections": det_list,
            "hud": {
                "text": f"LẤN CHIẾM VỈA HÈ: {violation_count} VẬT THỂ" if violation_count > 0 else "TRẬT TỰ ĐÔ THỊ: BÌNH THƯỜNG",
                "display_ms": int(BBOX_PUBLISH_INTERVAL * 2500),
                "color": "#ef4444" if violation_count > 0 else "#22c55e",
            }
        }

        topic = topic_bbox(self.camera_code)
        self.mqtt.publish_bbox(topic, payload)

    def _handle_event(self, evt: EncroachmentEvent):
        """Xử lý sự kiện vi phạm: upload ảnh bằng chứng có vẽ BBox -> publish MQTT event."""
        now = time.time()
        if now - self._last_event_time < EVENT_COOLDOWN:
            logger.info("[%s] Đang trong thời gian Cooldown (%ds), bỏ qua sự kiện trùng.", self.camera_code, EVENT_COOLDOWN)
            return

        self._last_event_time = now

        # Upload ảnh snapshot (đã được vẽ BBox & Polygon sẵn trong detector)
        snapshot_url = None
        if evt.frame is not None:
            _, buffer = cv2.imencode(".jpg", evt.frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if buffer is not None:
                snapshot_url = upload_snapshot(
                    image_bytes=buffer.tobytes(),
                    camera_id=self.camera_id,
                    category="anomaly",
                )

        # Build payload sự kiện vi phạm Anomaly
        event_time = datetime.now(VN_TZ).isoformat()
        zone_name = self.detector.polygon_names[0] if self.detector.polygon_names else "Vỉa hè"
        event_payload = {
            "ai_modules": AI_MODULE,
            "submodule_code": "SIDEWALK_ENCROACHMENT",
            "camera_id": self.camera_id,
            "camera_code": self.camera_code,
            "event_time": event_time,
            "entity_type": "OBSTACLE",
            "confidence": round(evt.confidence, 3),
            "severity": "HIGH",
            "payload": {
                "type": "SIDEWALK_ENCROACHMENT",
                "zone_name": zone_name,
                "obstacle_type": evt.class_name,
                "duration_seconds": round(evt.duration, 1),
                "description": f"Phát hiện {evt.class_name} lấn chiếm vỉa hè {evt.duration:.1f}s",
            },
        }

        if snapshot_url:
            event_payload["snapshot_url"] = snapshot_url

        self.mqtt.publish(TOPIC_AI_EVENTS, event_payload, qos=1)
        logger.info(
            "🚨 [%s] CẢNH BÁO VI PHẠM ĐÃ GỬI LÊN VMS: %s (duration=%.1fs, snapshot=%s)",
            self.camera_code, evt.class_name, evt.duration, snapshot_url or "none",
        )


class SidewalkPipeline:
    """Pipeline tổng: quản lý kết nối MQTT + điều phối các CameraWorker."""

    def __init__(self):
        self.mqtt = MQTTClient()
        self.workers: dict[str, CameraWorker] = {}
        self._lock = threading.Lock()

    def start(self):
        """Khởi chạy toàn bộ hệ thống."""
        logger.info("=" * 70)
        logger.info("  KHỞI CHẠY PIPELINE GIÁM SÁT LẤN CHIẾM VỈA HÈ")
        logger.info("  Module: %s | Zone: %d | Org: %d | Client: %s", AI_MODULE, ZONE_ID, ORGANIZATION_ID, self.mqtt._client._client_id.decode())
        logger.info("=" * 70)

        # Đăng ký callbacks từ MQTT
        self.mqtt.on_cameras(self._on_cameras_received)
        self.mqtt.on_ai_zones(self._on_ai_zones_received)

        # Kết nối Broker
        self.mqtt.connect()
        if not self.mqtt.wait_connected(timeout=15):
            logger.error("Không thể kết nối tới MQTT Broker VMS!")
            return False

        logger.info("✅ Đã kết nối MQTT Broker thành công.")
        return True

    def stop(self):
        """Dừng an toàn pipeline."""
        logger.info("Đang dừng Pipeline...")
        with self._lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()
        self.mqtt.disconnect()
        logger.info("Pipeline đã dừng.")

    def run_forever(self):
        """Chạy giữ tiến trình chính."""
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Dừng tiến trình bởi người dùng (Ctrl+C).")
            self.stop()

    def _on_cameras_received(self, data: dict):
        """Nhận danh sách camera từ topic smart_vms/cameras/zone/{zone_id}."""
        cameras = data.get("cameras", [])
        logger.info("Nhận được %d camera từ Zone %d", len(cameras), ZONE_ID)

        eligible_cameras = []
        for cam in cameras:
            code = cam.get("code")
            # Nếu đã chỉ định CAMERA_CODE (cam_sidewalk), chỉ chạy camera đó
            if CAMERA_CODE and code != CAMERA_CODE:
                continue

            ai_modules = cam.get("ai_modules", [])
            # Chỉ xử lý camera ĐÃ BẬT module SIDEWALK_ENCROACHMENT
            if AI_MODULE in ai_modules or "ANOMALY" in ai_modules:
                eligible_cameras.append(cam)
                logger.info("  ✅ Camera hợp lệ: %s (id=%s) - Modules: %s", code, cam.get("id"), ai_modules)
            else:
                logger.info("  ⏭️ Bỏ qua camera %s (chưa bật module %s)", code, AI_MODULE)

        with self._lock:
            current_codes = {cam["code"] for cam in eligible_cameras}

            # Dừng worker của camera bị tắt module
            for code in list(self.workers.keys()):
                if code not in current_codes:
                    logger.info("🛑 [%s] Module bị tắt trên VMS -> Dừng Camera Worker.", code)
                    self.workers[code].stop()
                    del self.workers[code]

            # Khởi tạo hoặc cập nhật worker cho camera đủ điều kiện
            camera_codes_to_sub = []
            for cam in eligible_cameras:
                code = cam["code"]
                camera_codes_to_sub.append(code)
                if code not in self.workers:
                    worker = CameraWorker(
                        camera_id=cam["id"],
                        camera_code=code,
                        rtsp_url=cam.get("rtsp_url", ""),
                        mqtt=self.mqtt,
                    )
                    self.workers[code] = worker
                    worker.start()

            # Subscribe nhận vùng vỉa hè cho các camera này
            if camera_codes_to_sub:
                self.mqtt.subscribe_camera_zones(camera_codes_to_sub)

    def _on_ai_zones_received(self, topic: str, data: dict):
        """
        Nhận bản tin cấu hình vùng vỉa hè từ topic:
        smart_vms/cameras/{camera_code}/ai_zones/SIDEWALK_ENCROACHMENT
        - Hot-reload ngay lập tức vào Worker mà không cần restart tiến trình.
        - Chịu được payload rỗng khi VMS xóa vùng hoặc tắt module.
        """
        parts = topic.split("/")
        if len(parts) >= 3:
            camera_code = parts[2]
        else:
            logger.warning("Không thể trích xuất camera_code từ topic: %s", topic)
            return

        # Lấy danh sách polygon (chịu được payload rỗng, payload="")
        ai_zones = []
        if isinstance(data, dict):
            ai_zones = data.get("ai_zones", [])
        
        logger.info(
            "📥 [%s] Nhận cấu hình vùng từ topic %s: %d Polygon vỉa hè.",
            camera_code, topic, len(ai_zones),
        )

        with self._lock:
            if camera_code in self.workers:
                # Cập nhật nóng vào Worker
                self.workers[camera_code].set_ai_zones(ai_zones)
            else:
                logger.info("[%s] Worker chưa khởi tạo, lưu cấu hình vào cache.", camera_code)
                # Tự động lưu cache để khi worker khởi tạo có thể dùng ngay
                save_path = DATA_DIR / f"{camera_code}_ai_zones.json"
                try:
                    with open(save_path, "w", encoding="utf-8") as f:
                        json.dump({"camera_code": camera_code, "ai_zones": ai_zones}, f, indent=2)
                except Exception:
                    pass

