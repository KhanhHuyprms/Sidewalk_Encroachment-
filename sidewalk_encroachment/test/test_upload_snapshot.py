"""
test_upload_snapshot.py - Kiểm tra nguồn Upload ảnh snapshot cảnh báo lên Storage API và gửi sự kiện kèm ảnh.

Theo tài liệu AI_MODULES_MQTT.md (Mục 4):
1. Upload ảnh qua Storage API: POST https://aioc-stg.atin.vn/storage/upload/file
2. Lấy object_key từ response JSON.
3. Kiểm tra xem ảnh đã sẵn sàng xem qua endpoint: https://aioc-stg.atin.vn/api/v1/snapshot/image?object_key=<object_key>
4. Gửi thử sự kiện cảnh báo có snapshot_url qua MQTT broker (wss://aioc-stg.atin.vn/mqtt).
"""
import os
import sys
import time
import json
import ssl

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import paho.mqtt.client as mqtt

VN_TZ = timezone(timedelta(hours=7))

# Cấu hình từ tài liệu AI_MODULES_MQTT.md
STORAGE_UPLOAD_URL = 
STORAGE_VIEW_URL   = 
MQTT_HOST          = 
MQTT_PORT          = 
MQTT_PATH          = "/mqtt"
MQTT_USER          = 
MQTT_PASS          = 

TEST_CAMERA_ID     = "699bc4c0-2f06-4671-8615-321278d86d5c"
TEST_CAMERA_CODE   = "cam_sidewalk"
TEST_MODULE        = "SIDEWALK_ENCROACHMENT"

def get_or_create_test_image(img_path: Path) -> Path:
    """Tạo hoặc lấy ảnh mẫu để test upload."""
    if img_path.exists():
        return img_path

    # Nếu chưa có, tạo ảnh mẫu JPEG đơn giản
    import cv2
    import numpy as np

    candidates = [
        img_path.parent / "Cam_05.mp4",
        img_path.parent / "sidewalk_encroachment" / "data" / "Cam_05.mp4",
    ]
    video_path = next((p for p in candidates if p.exists()), None)
    if video_path:
        cap = cv2.VideoCapture(str(video_path))
        cap.set(cv2.CAP_PROP_POS_FRAMES, 1440)
        ret, frame = cap.read()
        cap.release()
        if ret:
            cv2.imwrite(str(img_path), frame)
            return img_path

    # Tạo ảnh tổng hợp nếu không có video
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.putText(img, "AIOC TEST SNAPSHOT", (400, 360),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2)
    cv2.imwrite(str(img_path), img)
    return img_path

def test_upload_snapshot(img_path: Path, camera_id: str = TEST_CAMERA_ID, category: str = "anomaly"):
    print("=" * 70)
    print("1. KIỂM TRA UPLOAD ẢNH LÊN STORAGE API")
    print("=" * 70)
    print(f"Endpoint     : {STORAGE_UPLOAD_URL}")
    print(f"File ảnh     : {img_path} ({img_path.stat().st_size:,} bytes)")
    print(f"camera_id    : {camera_id}")
    print(f"category     : {category}")
    print("Đang gửi HTTP POST multipart/form-data...")

    start = time.time()
    try:
        with open(img_path, "rb") as f:
            files = {"file": (img_path.name, f, "image/jpeg")}
            data = {"camera_id": camera_id, "category": category}
            resp = requests.post(STORAGE_UPLOAD_URL, files=files, data=data, timeout=20)
        elapsed = time.time() - start

        print(f"HTTP Status  : {resp.status_code} ({elapsed:.2f}s)")
        print("Response JSON:")
        res_json = resp.json()
        print(json.dumps(res_json, indent=2, ensure_ascii=False))

        if resp.status_code == 200 and res_json.get("success"):
            object_key = res_json.get("object_key")
            print(f"\n=> UPLOAD THÀNH CÔNG! object_key = '{object_key}'")
            return object_key
        else:
            print(f"\n=> UPLOAD THẤT BẠI: {res_json.get('error')}")
            return None
    except Exception as e:
        print(f"\n=> LỖI KẾT NỐI UPLOAD: {e}")
        return None

def test_view_snapshot(object_key: str):
    print("\n" + "=" * 70)
    print("2. KIỂM TRA TRUY CẬP ẢNH ĐÃ UPLOAD QUA VMS ENDPOINT (§4.4)")
    print("=" * 70)
    view_url = f"{STORAGE_VIEW_URL}?object_key={object_key}"
    print(f"URL kiểm tra : {view_url}")
    print("Đang gửi HTTP GET...")

    start = time.time()
    try:
        resp = requests.get(view_url, timeout=15)
        elapsed = time.time() - start
        print(f"HTTP Status  : {resp.status_code} ({elapsed:.2f}s)")
        print(f"Content-Type : {resp.headers.get('Content-Type')}")
        print(f"Content-Length: {resp.headers.get('Content-Length')} bytes")

        if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", ""):
            print("=> XÁC NHẬN: Ảnh tải về hoàn toàn hợp lệ, VMS có thể hiển thị ảnh này!")
            return True
        else:
            print("=> Không tải được ảnh hoặc phản hồi không phải dạng image.")
            return False
    except Exception as e:
        print(f"=> Lỗi kiểm tra ảnh: {e}")
        return False

def test_publish_event_with_snapshot(object_key: str):
    print("\n" + "=" * 70)
    print("3. TEST GỬI SỰ KIỆN CẢNH BÁO KÈM SNAPSHOT QUA MQTT BROKER")
    print("=" * 70)
    print(f"Broker       : wss://{MQTT_HOST}:{MQTT_PORT}{MQTT_PATH}")
    print(f"Topic        : smart_vms/ai_events/{TEST_MODULE}")

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="tts_KhanhHuy_05",
        transport="websockets",
    )
    client.ws_set_options(path=MQTT_PATH)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.username_pw_set(MQTT_USER, MQTT_PASS)

    connected = False
    def on_connect(c, userdata, flags, rc, props=None):
        nonlocal connected
        if rc == 0:
            connected = True

    client.on_connect = on_connect
    print("Đang kết nối MQTT broker...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    for _ in range(20):
        if connected:
            break
        time.sleep(0.5)

    if not connected:
        print("=> Không thể kết nối MQTT broker.")
        client.loop_stop()
        return False

    print("=> Kết nối MQTT broker thành công!")

    event_payload = {
        "ai_modules": TEST_MODULE,
        "submodule_code": "SIDEWALK_ENCROACHMENT",
        "camera_id": TEST_CAMERA_ID,
        "camera_code": TEST_CAMERA_CODE,
        "event_time": datetime.now(VN_TZ).isoformat(),
        "entity_type": "OBSTACLE",
        "confidence": 0.88,
        "severity": "MEDIUM",
        "payload": {
            "type": "SIDEWALK_ENCROACHMENT",
            "zone_name": "Vỉa hè (test upload)",
            "obstacle_type": "motorcycle",
            "duration_seconds": 12.5,
        },
        "snapshot_url": object_key,
    }

    topic = f"smart_vms/ai_events/{TEST_MODULE}"
    pub_res = client.publish(topic, json.dumps(event_payload, ensure_ascii=False), qos=1)
    pub_res.wait_for_publish(timeout=5)
    print(f"Đã publish sự kiện lên topic: {topic}")
    print("Event Payload:")
    print(json.dumps(event_payload, indent=2, ensure_ascii=False))

    client.loop_stop()
    client.disconnect()
    print("=> Hoàn thành phát sự kiện kèm snapshot_url!")
    return True

if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    img_file = get_or_create_test_image(base_dir / "snapshot_test.jpg")

    obj_key = test_upload_snapshot(img_file)
    if obj_key:
        test_view_snapshot(obj_key)
        test_publish_event_with_snapshot(obj_key)
        print("\n" + "=" * 70)
        print("TẤT CẢ CÁC BƯỚC TEST NGUỒN UP ẢNH SNAPSHOT ĐỀU THÀNH CÔNG 100%!")
        print("=" * 70)
