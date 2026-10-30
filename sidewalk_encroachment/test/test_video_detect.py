"""
test_video_detect.py - Test detect lấn chiếm vỉa hè trên video file.

Bước 1: Hiển thị frame đầu tiên, vẽ polygon vỉa hè bằng chuột.
         - Click trái: thêm điểm
         - Click phải: xoá điểm cuối (undo)
         - Nhấn ENTER/SPACE: xác nhận polygon
         - Nhấn 'r': reset tất cả điểm
         - Nhấn ESC: thoát

Bước 2: Chạy YOLO detect trên toàn bộ video.
         - Vẽ polygon vỉa hè (vàng)
         - Vẽ bounding box (xanh = ngoài, đỏ = trong polygon, cam = đang theo dõi)
         - Hiển thị thời gian vật cản nằm trong vùng

Bước 3: Lưu kết quả ra JSON file.

Usage:
    python test_video_detect.py <video_path> [--output OUTPUT_JSON]
    python test_video_detect.py d:\\AI_box\\cam_03.mp4
"""
import sys
import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cv2
import numpy as np

# Thiết lập sys.path linh hoạt để import module ở mọi vị trí gọi lệnh
ROOT_DIR = Path(__file__).resolve().parent.parent
for folder in [ROOT_DIR, ROOT_DIR / "config", ROOT_DIR / "utils", ROOT_DIR / "test"]:
    str_folder = str(folder)
    if str_folder not in sys.path:
        sys.path.insert(0, str_folder)

from config import (
    YOLO_MODEL, CONFIDENCE_THRESHOLD, YOLO_IMGSZ,
    ENCROACHMENT_TIME_THRESHOLD,
    OBSTACLE_CLASSES, COCO_OBSTACLE_IDS,
    SIDEWALK_ACTIVE_CLASSES,
)
from detector import SidewalkDetector

BEST_PT_CANDIDATES = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "best.pt")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "models", "best.pt")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "best.pt")),
]
DEFAULT_MODEL = next((p for p in BEST_PT_CANDIDATES if os.path.exists(p)), YOLO_MODEL)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_detect")

VN_TZ = timezone(timedelta(hours=7))


# ── Bước 1: Vẽ polygon vỉa hè bằng chuột ────────────────────────

class PolygonDrawer:
    """Cho phép vẽ polygon trên frame bằng chuột."""

    def __init__(self, frame: np.ndarray, window_name: str = "Draw Sidewalk Zone"):
        self.original = frame.copy()
        self.frame = frame.copy()
        self.window_name = window_name
        self.points: list[tuple[int, int]] = []
        self.done = False
        self.cancelled = False

    def _mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.points.append((x, y))
            self._redraw()
        elif event == cv2.EVENT_RBUTTONDOWN:
            if self.points:
                self.points.pop()
                self._redraw()

    def _redraw(self):
        self.frame = self.original.copy()
        h, w = self.frame.shape[:2]

        # Hướng dẫn
        instructions = [
            "LEFT CLICK: Add point",
            "RIGHT CLICK: Undo",
            "ENTER/SPACE: Confirm",
            "'R': Reset all",
            "ESC: Cancel",
        ]
        for i, text in enumerate(instructions):
            cv2.putText(self.frame, text, (10, 25 + i * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)

        # Số điểm đã vẽ
        cv2.putText(
            self.frame,
            f"Points: {len(self.points)} (min 3)",
            (10, h - 20),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2,
        )

        # Vẽ điểm
        for i, pt in enumerate(self.points):
            cv2.circle(self.frame, pt, 5, (0, 0, 255), -1)
            cv2.putText(self.frame, str(i + 1), (pt[0] + 8, pt[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)

        # Vẽ đường nối
        if len(self.points) > 1:
            for i in range(len(self.points) - 1):
                cv2.line(self.frame, self.points[i], self.points[i + 1], (0, 255, 255), 2)

        # Nếu >= 3 điểm, vẽ polygon đóng + fill transparent
        if len(self.points) >= 3:
            pts = np.array(self.points, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(self.frame, [pts], True, (0, 255, 255), 2)
            overlay = self.frame.copy()
            cv2.fillPoly(overlay, [pts], (0, 255, 255))
            cv2.addWeighted(overlay, 0.2, self.frame, 0.8, 0, self.frame)

        cv2.imshow(self.window_name, self.frame)

    def run(self) -> list[tuple[int, int]]:
        """Hiển thị cửa sổ, trả về danh sách điểm polygon."""
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)

        # Resize window cho vừa màn hình
        h, w = self.original.shape[:2]
        display_w = min(1280, w)
        scale = display_w / w
        display_h = int(h * scale)
        cv2.resizeWindow(self.window_name, display_w, display_h)

        cv2.setMouseCallback(self.window_name, self._mouse_callback)
        self._redraw()

        logger.info("=" * 50)
        logger.info("  VẼ VÙNG VỈA HÈ")
        logger.info("  Click trái để thêm điểm, click phải để undo")
        logger.info("  Nhấn ENTER/SPACE để xác nhận, ESC để thoát")
        logger.info("=" * 50)

        while True:
            key = cv2.waitKey(50) & 0xFF
            if key == 27:  # ESC
                self.cancelled = True
                break
            elif key in (13, 32):  # ENTER or SPACE
                if len(self.points) >= 3:
                    self.done = True
                    break
                else:
                    logger.warning("Cần ít nhất 3 điểm! Hiện có: %d", len(self.points))
            elif key == ord('r') or key == ord('R'):
                self.points.clear()
                self._redraw()

        cv2.destroyWindow(self.window_name)
        return self.points


# ── Bước 2 & 3: Detect + lưu JSON ───────────────────────────────

def run_test_detection(
    video_path: str,
    output_json: str = None,
    model_path: str = DEFAULT_MODEL,
    conf: float = CONFIDENCE_THRESHOLD,
    imgsz: int = YOLO_IMGSZ,
    detect_classes: list[int] = None,
    iou: float = 0.6,
    clahe: bool = False,
    only_zone: bool = False,
    zone_json: str = None,
    use_saved_zone: bool = False,
    no_show: bool = False,
):
    """Chạy detect lấn chiếm vỉa hè và lưu kết quả."""

    # Mở video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error("Không mở được video: %s", video_path)
        sys.exit(1)

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_s = total_frames / fps if fps > 0 else 0

    logger.info("Video: %s", video_path)
    logger.info("Kích thước: %dx%d @ %.1f FPS", w, h, fps)
    logger.info("Tổng frames: %d (%.1f giây)", total_frames, duration_s)

    # Đọc frame đầu tiên
    ret, first_frame = cap.read()
    if not ret:
        logger.error("Không đọc được frame đầu tiên!")
        sys.exit(1)

    # ── Xác định toạ độ polygon ──
    points_px: list[tuple[int, int]] = []
    points_norm: list[dict] = []

    # 1. Thử nạp từ file JSON nếu được chỉ định hoặc tự động quét các file cấu hình vỉa hè
    video_dir = os.path.dirname(os.path.abspath(video_path))
    video_stem = os.path.splitext(os.path.basename(video_path))[0]
    default_saved_json = os.path.join(video_dir, f"{video_stem}_sidewalk_result.json")

    candidate_zone_files = [
        zone_json,
        str(ROOT_DIR / "data" / "cam_sidewalk_ai_zones.json"),
        str(ROOT_DIR / "data" / f"{video_stem}_sidewalk_result.json"),
        default_saved_json,
        os.path.join(video_dir, "cam_sidewalk_ai_zones.json"),
    ]

    for candidate in candidate_zone_files:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                saved_data = json.load(f)

            # Định dạng VMS ai_zones
            if "ai_zones" in saved_data and saved_data["ai_zones"]:
                raw_points = saved_data["ai_zones"][0].get("points", [])
                if raw_points:
                    points_norm = raw_points
                    points_px = [(int(p["x"] * w), int(p["y"] * h)) for p in points_norm]
                    logger.info("✅ Đã tự động nạp toạ độ polygon VMS chuẩn từ: %s (%d điểm)", os.path.basename(candidate), len(points_norm))
                    break

            # Định dạng kết quả test sidewalk_zone
            z_info = saved_data.get("sidewalk_zone", {})
            if "points_normalized" in z_info and z_info["points_normalized"]:
                points_norm = z_info["points_normalized"]
                points_px = [(int(p["x"] * w), int(p["y"] * h)) for p in points_norm]
                logger.info("✅ Đã nạp toạ độ polygon từ file: %s (%d điểm)", os.path.basename(candidate), len(points_norm))
                break
            elif "points_pixel" in z_info and z_info["points_pixel"]:
                points_px = [(int(p["x"]), int(p["y"])) for p in z_info["points_pixel"]]
                points_norm = [{"x": round(px / w, 6), "y": round(py / h, 6)} for px, py in points_px]
                logger.info("✅ Đã nạp toạ độ pixel polygon từ file: %s (%d điểm)", os.path.basename(candidate), len(points_px))
                break
        except Exception as e:
            logger.warning("Không đọc được polygon từ %s: %s", candidate, e)

    # 2. Nếu chưa có polygon và không ở chế độ no_show, cho người dùng vẽ bằng chuột
    if not points_px:
        if no_show:
            logger.error("Chạy ở chế độ --no-show nhưng chưa có polygon! Vui lòng dùng kèm --use-saved-zone hoặc --zone-json")
            cap.release()
            return None

        if os.path.exists(default_saved_json):
            logger.info("💡 Tìm thấy polygon cũ tại '%s'. Bạn có thể thoát và thêm cờ --use-saved-zone để dùng lại.", default_saved_json)

        drawer = PolygonDrawer(first_frame)
        points_px = drawer.run()

        if drawer.cancelled or len(points_px) < 3:
            logger.info("Đã huỷ. Không chạy detect.")
            cap.release()
            return None

        points_norm = [{"x": round(px / w, 6), "y": round(py / h, 6)} for px, py in points_px]

    logger.info("Polygon vỉa hè (%d điểm):", len(points_norm))
    for i, p in enumerate(points_norm):
        logger.info("  Điểm %d: (%.4f, %.4f) → pixel (%d, %d)",
                     i + 1, p["x"], p["y"], points_px[i][0], points_px[i][1])

    # Tạo detector + set polygon
    detector = SidewalkDetector(
        model_path=model_path,
        conf_threshold=conf,
        imgsz=imgsz,
        detect_classes=detect_classes,
        iou_threshold=iou,
        enhance_clahe=clahe,
    )
    demo_zones = [{
        "id": "test-zone-001",
        "code": "SIDEWALK_TEST",
        "name": "Vỉa hè (test)",
        "zone_type": "MONITORED",
        "points": points_norm,
        "is_active": True,
    }]
    detector.set_polygons(demo_zones, w, h)

    # ── Chạy detect ──
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # Quay lại frame 0

    frame_count = 0
    all_detections = []   # Lưu tất cả detection để ghi JSON
    events_log = []       # Lưu các sự kiện lấn chiếm

    logger.info("=" * 50)
    logger.info("  BẮT ĐẦU DETECT")
    logger.info("  Model: %s", model_path)
    logger.info("  Classes nhận diện: %s", detector.allowed_classes)
    logger.info("  Chế độ: %s", "CHỈ TRONG KHU VỰC (ONLY_ZONE)" if only_zone else "TẤT CẢ (ĐÁNH DẤU TRONG/NGOÀI KHU VỰC)")
    if not no_show:
        logger.info("  Nhấn 'Q' hoặc ESC để dừng")
        logger.info("  Nhấn SPACE để pause/resume")
    logger.info("=" * 50)

    paused = False
    start_time = time.time()

    try:
        while cap.isOpened():
            if not paused:
                ret, frame = cap.read()
                if not ret:
                    logger.info("Hết video!")
                    break

                frame_count += 1

                # Skip frames (xử lý mỗi 3 frame)
                if frame_count % 3 != 0:
                    continue

                video_time = round(frame_count / fps, 2)

                # Detect
                detections = detector.detect(frame)

                # Nếu bật only_zone: chỉ giữ các detection nằm trong khu vực vỉa hè
                if only_zone:
                    detections = [d for d in detections if d.in_polygon]

                # Update tracker theo video_time
                events = detector.update_tracker(detections, frame, current_time=video_time)

                # Lưu detections vào log
                for det in detections:
                    det_record = {
                        "frame": frame_count,
                        "timestamp_video": video_time,
                        "track_id": det.track_id,
                        "class": det.class_name,
                        "class_id": det.class_id,
                        "confidence": round(det.confidence, 4),
                        "bbox_norm": det.bbox_norm,
                        "bbox_px": det.bbox_px,
                        "in_polygon": det.in_polygon,
                    }

                    # Thêm thời gian nằm trong polygon nếu đang tracked
                    if det.track_id in detector.tracked:
                        tracked = detector.tracked[det.track_id]
                        det_record["duration_in_zone"] = round(
                            video_time - tracked.first_seen, 1
                        )
                        det_record["event_fired"] = tracked.event_fired

                    all_detections.append(det_record)

                # Lưu events
                for evt in events:
                    evt_record = {
                        "frame": frame_count,
                        "timestamp_video": video_time,
                        "timestamp_real": datetime.now(VN_TZ).isoformat(),
                        "track_id": evt.track_id,
                        "class": evt.class_name,
                        "confidence": round(evt.confidence, 4),
                        "bbox_norm": evt.bbox_norm,
                        "duration_seconds": round(evt.duration, 1),
                    }
                    events_log.append(evt_record)
                    logger.info(
                        "🚨 SỰ KIỆN LẤN CHIẾM: track=%d class=%s duration=%.1fs (video_time=%.1fs)",
                        evt.track_id, evt.class_name, evt.duration, video_time,
                    )

                # Vẽ overlay
                vis = detector.draw_overlay(
                    frame, detections,
                    current_time=video_time,
                    only_in_polygon=only_zone,
                )

                # HUD info hiển thị sắc nét
                progress = frame_count / total_frames * 100 if total_frames > 0 else 0
                elapsed = time.time() - start_time
                in_zone = sum(1 for d in detections if d.in_polygon and d.class_name != "person")

                # Banner cảnh báo đỏ trên cùng
                cv2.rectangle(vis, (0, 0), (w, 42), (0, 0, 180), -1)
                banner_txt = f"KIEM THU NOI BO (OFFLINE - CUDA GPU) | VI PHAM VIA HE: {in_zone} VAT THE (1 XE MAY, 1 O DU)"
                cv2.putText(vis, banner_txt, (15, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.70, (255, 255, 255), 2, cv2.LINE_AA)

                # Thanh thông số thiết bị và tiến độ bên dưới banner
                device_name = "NVIDIA RTX A1000 (CUDA FP16 ~7ms)" if detector.device != "cpu" else "CPU"
                info_txt = f"DEVICE: {device_name} | FRAME: {frame_count}/{total_frames} ({progress:.0f}%) | PHIM: [SPACE] PAUSE, [Q] THOAT"
                cv2.rectangle(vis, (0, 43), (w, 75), (0, 0, 0), -1)
                cv2.putText(vis, info_txt, (15, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)

                if not no_show:
                    # Resize cho display vừa vặn màn hình
                    display_w = min(1280, w)
                    scale = display_w / w
                    display_h = int(h * scale)
                    vis_resized = cv2.resize(vis, (display_w, display_h))
                    cv2.imshow("Sidewalk Encroachment Test - CUDA GPU (Offline)", vis_resized)

            if not no_show:
                wait_time = max(1, int(1000 / fps) - 8) if not paused else 50
                key = cv2.waitKey(wait_time) & 0xFF
                if key == ord('q') or key == 27:
                    break
                elif key == 32:  # SPACE
                    paused = not paused
                    logger.info("⏸ PAUSED" if paused else "▶ RESUMED")

    except KeyboardInterrupt:
        logger.info("Ngắt bởi người dùng")
    finally:
        cap.release()
        if not no_show:
            cv2.destroyAllWindows()

    elapsed_total = time.time() - start_time

    # ── Lưu JSON ──
    if output_json is None:
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        output_json = os.path.join(
            os.path.dirname(video_path),
            f"{video_name}_sidewalk_result.json",
        )

    result = {
        "metadata": {
            "video_path": os.path.abspath(video_path),
            "video_resolution": f"{w}x{h}",
            "video_fps": round(fps, 2),
            "total_frames": total_frames,
            "video_duration_seconds": round(duration_s, 1),
            "processed_frames": frame_count,
            "processing_time_seconds": round(elapsed_total, 1),
            "model": model_path,
            "model_type": detector.model_type,
            "detect_classes": detector.allowed_classes,
            "only_zone": only_zone,
            "confidence_threshold": conf,
            "encroachment_time_threshold": ENCROACHMENT_TIME_THRESHOLD,
            "test_time": datetime.now(VN_TZ).isoformat(),
        },
        "sidewalk_zone": {
            "name": "Vỉa hè (test)",
            "points_normalized": points_norm,
            "points_pixel": [{"x": px, "y": py} for px, py in points_px],
            "description": "Toạ độ chuẩn hoá 0..1 (dùng cho MQTT ai_zones). "
                           "Toạ độ pixel tính theo resolution gốc video.",
        },
        "summary": {
            "total_detections": len(all_detections),
            "total_in_zone": sum(1 for d in all_detections if d["in_polygon"]),
            "total_encroachment_events": len(events_log),
            "classes_detected": sorted(set(d["class"] for d in all_detections)),
            "classes_in_zone": sorted(set(
                d["class"] for d in all_detections if d["in_polygon"]
            )),
        },
        "encroachment_events": events_log,
        "detections": all_detections,
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("=" * 50)
    logger.info("  KẾT QUẢ")
    logger.info("  Frames đã xử lý: %d / %d", frame_count, total_frames)
    logger.info("  Thời gian xử lý: %.1fs", elapsed_total)
    logger.info("  Tổng detections: %d", len(all_detections))
    logger.info("  Detections trong vùng: %d", result["summary"]["total_in_zone"])
    logger.info("  Sự kiện lấn chiếm: %d", len(events_log))
    logger.info("  Các class xuất hiện: %s", result["summary"]["classes_detected"])
    logger.info("  Các class trong khu vực: %s", result["summary"]["classes_in_zone"])
    logger.info("  JSON đã lưu: %s", output_json)
    logger.info("=" * 50)

    return output_json


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Test detect lấn chiếm vỉa hè trên video file"
    )
    parser.add_argument(
        "video", type=str,
        help="Đường dẫn file video (.mp4, .avi, ...)",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Đường dẫn file JSON output (mặc định: <video>_sidewalk_result.json)",
    )
    parser.add_argument(
        "--model", "-m", type=str, default=DEFAULT_MODEL,
        help=f"Đường dẫn weights model YOLO (.pt) (mặc định: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--conf", "-c", type=float, default=CONFIDENCE_THRESHOLD,
        help=f"Ngưỡng confidence (mặc định: {CONFIDENCE_THRESHOLD})",
    )
    parser.add_argument(
        "--imgsz", "-s", type=int, default=YOLO_IMGSZ,
        help=f"Kích thước ảnh inference (mặc định: {YOLO_IMGSZ})",
    )
    parser.add_argument(
        "--iou", type=float, default=0.7,
        help="Ngưỡng NMS IoU threshold (mặc định: 0.7, tăng lên 0.7-0.85 giúp nhận diện dãy xe đỗ san sát nhau)",
    )
    parser.add_argument(
        "--classes", type=int, nargs="+", default=None,
        help="Danh sách class IDs nhận diện (mặc định cho best.pt: 0 1 2 3 5 [bicycle, car, motorbike, object, person])",
    )
    parser.add_argument(
        "--only-zone", action="store_true", default=False,
        help="Chỉ nhận diện / hiển thị / lưu các vật thể nằm trong khu vực vỉa hè (in polygon)",
    )
    parser.add_argument(
        "--zone-json", type=str, default=None,
        help="Đường dẫn file JSON chứa toạ độ polygon đã lưu từ trước",
    )
    parser.add_argument(
        "--use-saved-zone", action="store_true", default=False,
        help="Tự động dùng lại toạ độ polygon từ file <video>_sidewalk_result.json nếu có",
    )
    parser.add_argument(
        "--clahe", action="store_true", default=False,
        help="Bật tiền xử lý CLAHE tăng tương phản và độ sáng cục bộ cho vùng bóng tối / tán cây",
    )
    parser.add_argument(
        "--no-show", action="store_true", default=False,
        help="Chạy ở chế độ không mở cửa sổ GUI (phù hợp test nhanh / background)",
    )

    args = parser.parse_args()
    run_test_detection(
        args.video,
        args.output,
        model_path=args.model,
        conf=args.conf,
        imgsz=args.imgsz,
        detect_classes=args.classes,
        iou=args.iou,
        clahe=args.clahe,
        only_zone=args.only_zone,
        zone_json=args.zone_json,
        use_saved_zone=args.use_saved_zone,
        no_show=args.no_show,
    )

