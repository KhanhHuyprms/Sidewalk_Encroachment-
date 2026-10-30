
## 1. LUỒNG HOẠT ĐỘNG TỔNG THỂ CỦA PROJECT

Luồng giao tiếp giữa **AI Edge Box** và **Smart VMS** diễn ra theo 4 giai đoạn khép kín:

```mermaid
sequenceDiagram
    autonumber
    participant AI as AI Edge Box (Worker)
    participant MQTT as MQTT Broker (WSS 443)
    participant VMS as Smart VMS Backend (Zone 51)
    participant Storage as MinIO Storage API
    participant Web as Web VMS Live View

    Note over AI,MQTT: GIAI ĐOẠN 1: KHỞI TẠO KẾT NỐI & SUBSCRIBE
    AI->>MQTT: Kết nối WSS TLS (Client ID: tts_KhanhHuy_05)
    AI->>MQTT: Subscribe: smart_vms/cameras/zone/51
    AI->>MQTT: Subscribe: smart_vms/cameras/cam_sidewalk/ai_zones/SIDEWALK_ENCROACHMENT

    Note over AI,VMS: GIAI ĐOẠN 2: LẤY DANH SÁCH CAMERA & ĐỒNG BỘ ĐA GIÁC
    AI->>MQTT: Publish: smart_vms/cameras/getdevice (zone_id: 51, org: 103)
    VMS-->>AI: Payload: Danh sách camera (cam_sidewalk kích hoạt SIDEWALK_ENCROACHMENT)
    VMS-->>AI: Payload: Tọa độ đa giác vỉa hè 10 điểm (Hot-reload in-memory & Cache JSON)

    Note over AI,Web: GIAI ĐOẠN 3: XỬ LÝ VIDEO & BẮN BBOX LIVE VIEW
    loop Chu kỳ 0.08 giây (~12 FPS)
        AI->>AI: Phát hiện vật cản (xe máy, ô dù, quầy hàng)
        AI->>MQTT: Publish BBox: smart_vms/ai/bbox/cam_sidewalk (QoS 0)
        MQTT-->>Web: Vẽ viền BBox trực tiếp lên màn hình Live View
    end

    Note over AI,Storage: GIAI ĐOẠN 4: XÁC LẬP VI PHẠM (>10s) & BẮN CẢNH BÁO
    AI->>AI: Vật cản chiếm dụng vỉa hè liên tục > 10.0 giây
    AI->>AI: Chụp và vẽ overlay Snapshot (viền đỏ, banner tiêu đề)
    AI->>Storage: POST /storage/upload/file (image/jpeg)
    Storage-->>AI: Trả về snapshot_url
    AI->>MQTT: Publish Event: smart_vms/ai_events/SIDEWALK_ENCROACHMENT (QoS 1)
    MQTT-->>Web: Kích hoạt chuông báo động, hiển thị popup đỏ kèm ảnh bằng chứng
```

---

## 2. BẢNG TỔNG HỢP CÁC TOPIC MQTT ĐƯỢC GỌI VÀ SỬ DỤNG

| STT | Tên Topic MQTT | Chiều | QoS | Chu kỳ / Thời điểm | Mục đích sử dụng |
| :---: | :--- | :---: | :---: | :---: | :--- |
| **1** | `smart_vms/cameras/getdevice` | AI $\to$ VMS | 1 | 1 lần sau khi kết nối / retry | Gửi bản tin yêu cầu VMS cấp danh mục camera Zone `51`. |
| **2** | `smart_vms/cameras/zone/51` | VMS $\to$ AI | 1 | Khi khởi động & khi VMS đổi trạng thái | Tiếp nhận danh sách camera, kiểm tra cờ bật module `SIDEWALK_ENCROACHMENT`. |
| **3** | `smart_vms/cameras/cam_sidewalk/ai_zones/SIDEWALK_ENCROACHMENT` | VMS $\to$ AI | 1 | Khi vẽ / sửa vùng trên VMS | Nhận tọa độ đa giác vỉa hè, nạp nóng (*Hot-reload*) và chịu tải payload rỗng. |
| **4** | `smart_vms/ai/bbox/cam_sidewalk` | AI $\to$ VMS | 0 | Định kỳ \(0.08\,\text{s}\) (~12 FPS) | Gửi tọa độ BBox để Web VMS vẽ khung bao lên Live View. Áp dụng `BBOX_ONLY_IN_ZONE=true`. |
| **5** | `smart_vms/ai_events/SIDEWALK_ENCROACHMENT` | AI $\to$ VMS | 1 | Khi vi phạm chiếm dụng \(> 10\,\text{s}\) | Phát bản tin cảnh báo vi phạm Anomaly kèm URL ảnh snapshot từ Storage API. |

---

## 3. ĐẶC TẢ CHI TIẾT TỪNG TOPIC & CẤU TRÚC PAYLOAD JSON THỰC TẾ

### Topic 1: Yêu cầu danh sách thiết bị (`smart_vms/cameras/getdevice`)
- **Hành động**: Publish (QoS 1).
- **Mục đích**: Kích hoạt VMS gửi lại danh sách thiết bị thuộc phân quyền quản lý.
```json
{
  "zone_id": 51,
  "organization_id": 103,
  "client_id": "tts_KhanhHuy_05"
}
```

---

### Topic 2: Nhận danh sách camera thuộc Zone (`smart_vms/cameras/zone/51`)
- **Hành động**: Subscribe (QoS 1, bản tin Retained từ VMS).
- **Mục đích**: Kiểm tra camera mục tiêu `cam_sidewalk` có bật `SIDEWALK_ENCROACHMENT` hay không.
```json
{
  "zone_id": 51,
  "cameras": [
    {
      "id": "699bc4c0-2f06-4671-8615-321278d86d5c",
      "code": "cam_sidewalk",
      "status": "ONLINE",
      "ai_modules": [
        "SIDEWALK_ENCROACHMENT"
      ],
      "rtsp_url": "rtsp://admin:prometheus123@192.168.1.250:18554/c103_cam_sidewalk",
      "zone_id": 51
    }
  ]
}
```

---

### Topic 3: Đồng bộ tọa độ đa giác vỉa hè (`smart_vms/cameras/cam_sidewalk/ai_zones/SIDEWALK_ENCROACHMENT`)
- **Hành động**: Subscribe (QoS 1).
- **Cơ chế chịu tải và tối ưu**:
  1. **Hot-reload in-memory**: Cập nhật tức thời tọa độ mới vào detector mà không cần dừng/khởi động lại chương trình.
  2. **Chịu tải payload rỗng**: Nhận `""` hoặc `{"ai_zones": []}` an toàn, tự động giải phóng vùng cũ và chờ vùng mới.
  3. **Tự động lưu cache**: Ghi nhận tọa độ vào file `data/cam_sidewalk_ai_zones.json` để tự động phục hồi khi mất mạng.
```json
{
  "camera_code": "cam_sidewalk",
  "camera_id": "699bc4c0-2f06-4671-8615-321278d86d5c",
  "ai_zones": [
    {
      "id": "ca445f4c-ebd5-47b0-ac03-4eb320cc05dd",
      "code": "SIDEWALK",
      "name": "SIDEWALK",
      "zone_type": "MONITORED",
      "points": [
        {"x": 0.111979, "y": 0.967462},
        {"x": 0.143880, "y": 0.893171},
        {"x": 0.218099, "y": 0.684013},
        {"x": 0.370443, "y": 0.187977},
        {"x": 0.406250, "y": 0.066826},
        {"x": 0.234375, "y": 0.038252},
        {"x": 0.001953, "y": 0.373134},
        {"x": 0.000000, "y": 0.522859},
        {"x": 0.003906, "y": 0.998322},
        {"x": 0.064453, "y": 0.998322}
      ]
    }
  ]
}
```

---

### Topic 4: Truyền tải Bounding Box thời gian thực (`smart_vms/ai/bbox/cam_sidewalk`)
- **Hành động**: Publish (QoS 0).
- **Chu kỳ**: \(0.08\,\text{s}\) (~12 FPS).
- **Cơ chế lọc vùng `BBOX_ONLY_IN_ZONE=true`**: Chỉ gửi BBox của các vật thể nằm trong phạm vi vỉa hè (1 xe máy đỗ + 1 quầy dù), không vẽ xe đang chạy ngoài đường để triệt tiêu độ lệch pha video buffer.
```json
{
  "camera_code": "cam_sidewalk",
  "camera_id": "699bc4c0-2f06-4671-8615-321278d86d5c",
  "organization_id": 103,
  "ai_modules": "SIDEWALK_ENCROACHMENT",
  "timestamp": 1793354274.152,
  "detections": [
    {
      "id": "obj_1",
      "class": "object",
      "confidence": 0.55,
      "bbox": [0.0078, 0.2882, 0.2447, 0.5361],
      "track_id": 1
    },
    {
      "id": "obj_2",
      "class": "motorbike",
      "confidence": 0.73,
      "bbox": [0.0026, 0.6125, 0.1041, 0.7513],
      "track_id": 2
    }
  ],
  "hud": {
    "text": "LẤN CHIẾM VỈA HÈ: 2 VẬT THỂ",
    "display_ms": 200,
    "color": "#ef4444"
  }
}
```

---

### Topic 5: Bắn sự kiện cảnh báo vi phạm Anomaly (`smart_vms/ai_events/SIDEWALK_ENCROACHMENT`)
- **Hành động**: Publish (QoS 1).
- **Điều kiện**: Thời gian vật cản lấn chiếm liên tục \(> 10.0\,\text{s}\) và vượt ngưỡng Cooldown (\(60\,\text{s}\)).
```json
{
  "ai_modules": "SIDEWALK_ENCROACHMENT",
  "submodule_code": "SIDEWALK_ENCROACHMENT",
  "camera_id": "699bc4c0-2f06-4671-8615-321278d86d5c",
  "camera_code": "cam_sidewalk",
  "event_time": "2026-10-30T17:17:54.120+07:00",
  "entity_type": "OBSTACLE",
  "confidence": 0.73,
  "severity": "HIGH",
  "snapshot_url": "https://aioc-stg.atin.vn/storage/files/anomaly/cam_sidewalk_20261030_171754.jpg",
  "payload": {
    "type": "SIDEWALK_ENCROACHMENT",
    "zone_name": "SIDEWALK",
    "obstacle_type": "motorbike",
    "duration_seconds": 18.5,
    "description": "Phát hiện motorbike lấn chiếm vỉa hè 18.5s"
  }
}
```

---

## 4. HÌNH ẢNH KẾT QUẢ VÀ SNAPSHOT BẰNG CHỨNG THỰC TẾ TRÊN SMART VMS

### 4.1 Giao diện Popup chi tiết cảnh báo trên Web VMS
Giao diện tiếp nhận trực tiếp từ VMS Staging khi AI Box phát sự kiện lên topic `smart_vms/ai_events/SIDEWALK_ENCROACHMENT`:

![Chi tiết cảnh báo sự kiện trên Web VMS](file:///C:/Users/LAPTOP%20247/.gemini/antigravity/brain/817b9ae2-d78c-49ac-8fca-54c0f2658199/.user_uploaded/media_1793355796493_3eb4053e.jpg)

*Hình 1: Popup "Chi tiết cảnh báo sự kiện" trên Smart VMS Staging. Thời gian: 30/10/2026 17:17:54, Camera: Camera Đô Thị, Module: Sidewalk Encroachment, Mức độ: HIGH.*

---

### 4.2 Ảnh Snapshot bằng chứng vi phạm độ phân giải cao
Ảnh hiện trường độ phân giải cao trích xuất từ Storage API được AI Edge Box tự động tạo và tải lên:

![Ảnh Snapshot bằng chứng vi phạm](file:///C:/Users/LAPTOP%20247/.gemini/antigravity/brain/817b9ae2-d78c-49ac-8fca-54c0f2658199/.user_uploaded/media_1793355820757_83788410.jpg)

*Hình 2: Ảnh Snapshot bằng chứng do AI Edge Box tự động tải lên Storage API. Đa giác vỉa hè vàng (VIA HE VMS) và 2 vật thể vi phạm được đóng viền đỏ: quầy dù (`[VI PHAM] OBJECT 0.55`) và xe máy đỗ trái phép (`[VI PHAM] MOTORBIKE 0.73`).*

---

### 4.3 Bảng thống kê kết quả nhận diện từ Snapshot

| Đối tượng vi phạm | Nhãn nhận diện | Độ tin cậy | Thời gian chiếm dụng | Trạng thái ghi nhận |
| :--- | :--- | :---: | :---: | :--- |
| **Quầy hàng / Ô dù bán hàng** | `object` | \(0.55\) | \(12.0\,\text{s}\) | Vi phạm chiếm dụng vỉa hè (\(> 10\,\text{s}\)), xuất cảnh báo thành công. |
| **Xe máy dựng lề vỉa hè** | `motorbike` | \(0.73\) | \(18.5\,\text{s}\) | Đỗ trái quy định trong vỉa hè (\(> 10\,\text{s}\)), lọc sạch box trùng lặp. |
| **Phương tiện lưu thông ngoài đường** | `car, motorbike` | \(> 0.70\) | -- | Nằm ngoài vỉa hè, cơ chế `BBOX_ONLY_IN_ZONE=true` tự động loại bỏ BBox. |
