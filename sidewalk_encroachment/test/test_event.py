"""
test_event.py - Gửi thử sự kiện SIDEWALK_ENCROACHMENT lên camera cam_sidewalk.

Dùng để kiểm tra kết nối MQTT và payload trước khi chạy pipeline thật.
"""
import json
import ssl
import time
import logging
from datetime import datetime, timezone, timedelta

import paho.mqtt.client as mqtt

from config import (
    MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD,
    AI_MODULE, CAMERA_CODE, CAMERA_ID,
)

logger = logging.getLogger("test_event")

VN_TZ = timezone(timedelta(hours=7))

# Camera mục tiêu
TEST_CAMERA_ID   = CAMERA_ID
TEST_CAMERA_CODE = CAMERA_CODE


def send_test_event():
    """Gửi 1 event test + 1 bbox lên camera cam_sidewalk."""
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="tts_KhanhHuy_05",
        transport="websockets",
    )
    client.ws_set_options(path="/mqtt")
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    connected = False

    def on_connect(c, userdata, flags, rc, props=None):
        nonlocal connected
        if rc == 0:
            connected = True
            logger.info("✅ Connected to broker")
        else:
            logger.error("Connection failed: rc=%s", rc)

    client.on_connect = on_connect
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
    client.loop_start()

    # Chờ kết nối
    for _ in range(30):
        if connected:
            break
        time.sleep(0.5)

    if not connected:
        logger.error("Cannot connect to broker!")
        return

    event_time = datetime.now(VN_TZ).isoformat()

    # ── 1. Publish event ─────────────────────────────────────────
    event = {
        "ai_modules": AI_MODULE,
        "submodule_code": "SIDEWALK_ENCROACHMENT",
        "camera_id": TEST_CAMERA_ID,
        "camera_code": TEST_CAMERA_CODE,
        "event_time": event_time,
        "entity_type": "OBSTACLE",
        "confidence": 0.84,
        "severity": "MEDIUM",
        "payload": {
            "type": "SIDEWALK_ENCROACHMENT",
            "zone_name": "Vỉa hè đường số 1",
            "obstacle_type": "motorcycle",
            "duration_seconds": 15.2,
        },
    }

    topic_event = f"smart_vms/ai_events/{AI_MODULE}"
    result = client.publish(topic_event, json.dumps(event, ensure_ascii=False), qos=1)
    result.wait_for_publish(timeout=5)
    logger.info("📤 Event published → %s", topic_event)
    logger.info("   Payload: %s", json.dumps(event, ensure_ascii=False, indent=2))

    time.sleep(0.5)

    # ── 2. Publish bbox liên tục trong 10s để hiển thị trên Web VMS ──────
    topic_bbox = f"smart_vms/ai/bbox/{TEST_CAMERA_CODE}"
    logger.info("📡 Đang truyền luồng BBox lên topic %s (duy trì 10s, low latency)...", topic_bbox)

    for i in range(100):
        bbox = {
            "camera_code": TEST_CAMERA_CODE,
            "camera_id": TEST_CAMERA_ID,
            "organization_id": 103,
            "ai_modules": AI_MODULE,
            "timestamp": time.time(),
            "detections": [
                {
                    "id": "obj_1",
                    "class": "motorbike",
                    "confidence": 0.84,
                    "bbox": [0.0001, 0.6013, 0.1023, 0.7152],
                    "track_id": 1,
                },
                {
                    "id": "obj_2",
                    "class": "object",
                    "confidence": 0.77,
                    "bbox": [0.0044, 0.2810, 0.2126, 0.5370],
                    "track_id": 2,
                },
            ],
            "hud": {
                "text": "LẤN CHIẾM VỈA HÈ: 1 XE MÁY, 1 Ô DÙ",
                "display_ms": 500,
                "color": "#ef4444",
            },
        }
        client.publish(topic_bbox, json.dumps(bbox, ensure_ascii=False), qos=0)
        time.sleep(0.1)

    logger.info("📤 Hoàn tất truyền chuỗi BBox realtime!")

    time.sleep(1)

    # Cleanup
    client.loop_stop()
    client.disconnect()
    logger.info("✅ Test complete! Check Web VMS → Lấn chiếm vỉa hè → Lịch sử")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    send_test_event()
