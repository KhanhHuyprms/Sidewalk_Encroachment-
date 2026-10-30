from pathlib import Path
import argparse
import time
import sys
import json
import cv2
import numpy as np
from ultralytics import YOLO

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def enhance_frame_clahe(frame: np.ndarray, clip_limit: float = 3.0, grid_size: tuple = (8, 8)) -> np.ndarray:
    """Tăng sáng cục bộ bằng CLAHE trên kênh L (Luminance)."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=grid_size)
    l_enh = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l_enh, a, b]), cv2.COLOR_LAB2BGR)

def enhance_shadows_adaptive(frame: np.ndarray) -> np.ndarray:
    """Nâng sáng vùng bóng tối có chọn lọc (không làm cháy vùng sáng)."""
    ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycrcb)
    
    # Tạo mask vùng tối (y < 140)
    y_float = y.astype(np.float32)
    shadow_weight = np.clip((140.0 - y_float) / 140.0, 0, 1.0)
    y_boosted = np.clip(y_float + shadow_weight * 60.0, 0, 255.0).astype(np.uint8)
    
    # Áp dụng CLAHE nhẹ lên kênh Y đã tăng sáng
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    y_enh = clahe.apply(y_boosted)
    
    return cv2.cvtColor(cv2.merge([y_enh, cr, cb]), cv2.COLOR_YCrCb2BGR)

CLASS_COLORS = {
    "car": (255, 191, 0),         # Deep Sky Blue
    "motorbike": (0, 165, 255),    # Orange
    "person": (0, 255, 255),       # Yellow
    "bicycle": (0, 255, 0),        # Green
    "object": (255, 0, 255),       # Magenta
}

def load_polygon(json_path: Path, frame_w: int, frame_h: int) -> np.ndarray | None:
    """Tải toạ độ polygon từ file json kết quả."""
    if not json_path.exists():
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        zone = data.get("sidewalk_zone", {})
        pts_norm = zone.get("points_normalized", [])
        if pts_norm:
            return np.array([[int(p["x"] * frame_w), int(p["y"] * frame_h)] for p in pts_norm], dtype=np.int32)
        pts_px = zone.get("points_pixel", [])
        if pts_px:
            return np.array([[int(p["x"]), int(p["y"])] for p in pts_px], dtype=np.int32)
    except Exception as e:
        print("Lỗi đọc polygon:", e)
    return None

def draw_polygon_overlay(img: np.ndarray, poly: np.ndarray | None) -> np.ndarray:
    """Vẽ vùng polygon vỉa hè dạng viền vàng + phủ màu bán trong suốt."""
    if poly is None or len(poly) < 3:
        return img
    out = img.copy()
    overlay = img.copy()
    cv2.fillPoly(overlay, [poly], (0, 255, 255))
    cv2.addWeighted(overlay, 0.20, out, 0.80, 0, out)
    cv2.polylines(out, [poly], isClosed=True, color=(0, 255, 255), thickness=2)

    top_idx = int(np.argmin(poly[:, 1]))
    top_pt = poly[top_idx]
    label_x = max(10, min(top_pt[0] - 20, img.shape[1] - 220))
    label_y = max(35, top_pt[1] - 10)
    cv2.putText(out, "VỈA HÈ (SIDEWALK)", (label_x, label_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2, cv2.LINE_AA)
    return out

def is_box_in_poly(x1: int, y1: int, x2: int, y2: int, poly: np.ndarray | None) -> bool:
    """Kiểm tra bbox có nằm trong hoặc chạm vào polygon vỉa hè không."""
    if poly is None or len(poly) < 3:
        return False
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    b34 = y1 + 0.75 * (y2 - y1)
    pts = [(cx, cy), (cx, y2), (cx, b34), (x1, y2), (x2, y2)]
    for px, py in pts:
        if cv2.pointPolygonTest(poly, (float(px), float(py)), False) >= 0:
            return True
    return False

def draw_detections(img: np.ndarray, boxes, names: dict, poly: np.ndarray | None = None) -> np.ndarray:
    """Vẽ bounding boxes và labels lên ảnh. Đổi màu đỏ nếu nằm trong vỉa hè."""
    out = img.copy()
    for b in boxes:
        cls_id = int(b.cls[0])
        cls_name = names.get(cls_id, str(cls_id))
        conf = float(b.conf[0])
        x1, y1, x2, y2 = [int(x) for x in b.xyxy[0].tolist()]

        in_zone = is_box_in_poly(x1, y1, x2, y2, poly)
        if in_zone:
            color = (0, 0, 255)  # Đỏ = lấn chiếm / trong vỉa hè
            label = f"{cls_name} {conf:.2f} [VỈA HÈ]"
        else:
            color = CLASS_COLORS.get(cls_name, (0, 255, 0))
            label = f"{cls_name} {conf:.2f}"

        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        top = max(0, y1 - th - 6)
        cv2.rectangle(out, (x1, top), (x1 + tw + 6, max(th + 6, y1)), color, -1)
        cv2.putText(out, label, (x1 + 3, max(th + 2, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255) if in_zone else (0, 0, 0), 1, cv2.LINE_AA)
    return out

def run_single_frame_experiment(video_path: Path, model_path: Path, frame_idx: int = 1440, poly_path: Path = None):
    """Chạy so sánh 3 phương pháp trên frame chỉ định và lưu ảnh."""
    print(f"--- CHẾ ĐỘ EXPERIMENT (Frame {frame_idx}) ---")
    cap = cv2.VideoCapture(str(video_path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    if not ret:
        print(f"Cannot read frame {frame_idx} from {video_path}")
        return

    poly = load_polygon(poly_path, w, h) if poly_path else None
    if poly is not None:
        print(f"Đã nạp polygon vỉa hè: {len(poly)} điểm")

    f_clahe = enhance_frame_clahe(frame, clip_limit=3.5, grid_size=(8, 8))
    f_adaptive = enhance_shadows_adaptive(frame)

    base_dir = video_path.parent
    cv2.imwrite(str(base_dir / "frame_clahe.jpg"), f_clahe)
    cv2.imwrite(str(base_dir / "frame_adaptive.jpg"), f_adaptive)

    m = YOLO(str(model_path))
    for name, img in [("Original", frame), ("CLAHE", f_clahe), ("AdaptiveShadow", f_adaptive)]:
        res = m.predict(img, conf=0.15, imgsz=640, classes=[0, 1, 2, 3, 5], verbose=False)
        boxes = res[0].boxes
        in_zone_count = sum(1 for b in boxes if is_box_in_poly(int(b.xyxy[0][0]), int(b.xyxy[0][1]), int(b.xyxy[0][2]), int(b.xyxy[0][3]), poly))
        print(f"=== {name}: {len(boxes)} detections (trong vỉa hè: {in_zone_count}) ===")
        for b in boxes:
            cls_name = m.names[int(b.cls[0])]
            conf = float(b.conf[0])
            xyxy = [int(x) for x in b.xyxy[0].tolist()]
            in_z = is_box_in_poly(xyxy[0], xyxy[1], xyxy[2], xyxy[3], poly)
            print(f"  {cls_name} {conf:.2f} at {xyxy} {'[TRONG VỈA HÈ]' if in_z else ''}")

def run_realtime(video_path: Path, model_path: Path, start_frame: int = 0, conf: float = 0.25, imgsz: int = 640, poly_path: Path = None):
    """Chạy video real-time với phát hiện vật thể YOLO, vùng polygon vỉa hè và tăng sáng."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Lỗi: Không thể mở video tại {video_path}")
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    poly = load_polygon(poly_path, w, h) if poly_path else None
    if poly is not None:
        print(f"✅ Đã nạp toạ độ polygon vỉa hè ({len(poly)} điểm)")
    else:
        print("⚠️ Không tìm thấy file polygon JSON, chạy chế độ không polygon.")

    if start_frame > 0 and start_frame < total_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    print(f"Nạp model YOLO: {model_path} ...")
    model = YOLO(str(model_path))
    print(f"Video: {w}x{h} @ {video_fps:.1f} FPS, {total_frames} frames")
    print("=" * 60)
    print("  PHÍM ĐIỀU KHIỂN:")
    print("  [1] Ảnh gốc (Original)")
    print("  [2] CLAHE (Tăng tương phản Luminance)")
    print("  [3] AdaptiveShadow (Nâng sáng bóng tối tự thích ứng)")
    print("  [4] Side-by-Side (So sánh song song Gốc vs Tăng sáng)")
    print("  [P] Bật / Tắt hiển thị vùng polygon vỉa hè")
    print("  [Space] Tạm dừng / Tiếp tục")
    print("  [Q] hoặc [ESC] Thoát")
    print("=" * 60)

    mode = "adaptive"  # 'original', 'clahe', 'adaptive', 'split'
    show_poly = True
    paused = False
    prev_time = time.time()
    fps_display = video_fps
    frame_idx = start_frame

    window_name = "Realtime Video Detection - Sidewalk Encroachment"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    display_w = min(1280, w)
    display_h = int(display_w * h / w)
    cv2.resizeWindow(window_name, display_w, display_h)

    skip_interval = 1
    last_boxes = None

    while cap.isOpened():
        if not paused:
            ret, frame = cap.read()
            if not ret:
                print("\nĐã chạy hết video!")
                break
            frame_idx += 1

            if frame_idx % skip_interval == 0:
                if mode == "original":
                    proc_img = frame
                    mode_label = "ORIGINAL"
                elif mode == "clahe":
                    proc_img = enhance_frame_clahe(frame, clip_limit=3.0)
                    mode_label = "CLAHE"
                elif mode == "adaptive":
                    proc_img = enhance_shadows_adaptive(frame)
                    mode_label = "ADAPTIVE SHADOW"
                elif mode == "split":
                    proc_img = enhance_shadows_adaptive(frame)
                    mode_label = "SIDE-BY-SIDE (Gốc vs Tăng sáng)"

                res = model.predict(proc_img, conf=conf, imgsz=imgsz, classes=[0, 1, 2, 3, 5], verbose=False)
                last_boxes = res[0].boxes

            active_poly = poly if show_poly else None
            det_count = len(last_boxes) if last_boxes is not None else 0

            # 1. Vẽ polygon trước
            base_canvas = proc_img if mode != "original" else frame
            vis_img = draw_polygon_overlay(base_canvas, active_poly)

            # 2. Vẽ detection sau (để bounding box nằm đè lên polygon)
            vis_img = draw_detections(vis_img, last_boxes, model.names, active_poly)

            if mode == "split":
                vis_orig = draw_polygon_overlay(frame, active_poly)
                vis_orig = draw_detections(vis_orig, last_boxes, model.names, active_poly)

                half_w = display_w // 2
                half_h = display_h
                left = cv2.resize(vis_orig, (half_w, half_h))
                right = cv2.resize(vis_img, (half_w, half_h))
                cv2.putText(left, "Goc (Original)", (15, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                cv2.putText(right, "Adaptive Shadow", (15, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                display_frame = np.hstack([left, right])
            else:
                display_frame = cv2.resize(vis_img, (display_w, display_h))

            # FPS
            now = time.time()
            dt = now - prev_time
            prev_time = now
            if dt > 0:
                fps_display = 0.9 * fps_display + 0.1 * (1.0 / dt)

            # Đếm số vật trong vỉa hè
            in_zone_c = 0
            if last_boxes is not None and active_poly is not None:
                for b in last_boxes:
                    bx1, by1, bx2, by2 = [int(x) for x in b.xyxy[0].tolist()]
                    if is_box_in_poly(bx1, by1, bx2, by2, active_poly):
                        in_zone_c += 1

            # HUD
            hud_bg = display_frame.copy()
            cv2.rectangle(hud_bg, (0, 0), (display_frame.shape[1], 42), (0, 0, 0), -1)
            cv2.addWeighted(hud_bg, 0.65, display_frame, 0.35, 0, display_frame)

            poly_status = "ON" if show_poly else "OFF"
            hud_text = f"Frame {frame_idx}/{total_frames} | {fps_display:.1f} FPS | {mode_label} | Poly: {poly_status} | Objs: {det_count} (Vỉa hè: {in_zone_c})"
            cv2.putText(display_frame, hud_text, (10, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2, cv2.LINE_AA)

            cv2.imshow(window_name, display_frame)

        key = cv2.waitKey(1 if not paused else 30) & 0xFF
        if key == ord('q') or key == 27:
            break
        elif key == ord(' '):
            paused = not paused
            print("[PAUSE]" if paused else "[RESUME]")
        elif key == ord('1'):
            mode = "original"
            print("Chế độ: Gốc (Original)")
        elif key == ord('2'):
            mode = "clahe"
            print("Chế độ: CLAHE")
        elif key == ord('3'):
            mode = "adaptive"
            print("Chế độ: AdaptiveShadow")
        elif key == ord('4'):
            mode = "split"
            print("Chế độ: Side-by-Side (So sánh song song)")
        elif key == ord('p') or key == ord('P'):
            show_poly = not show_poly
            print(f"Vùng polygon vỉa hè: {'BẬT' if show_poly else 'TẮT'}")

    cap.release()
    cv2.destroyAllWindows()
    print("Đã đóng video.")

def run_experiment_demo(video_path: str = None):
    base_dir = Path(__file__).resolve().parent
    v_candidates = [
        Path(video_path) if video_path else None,
        base_dir / "Cam_05.mp4",
        base_dir.parent / "data" / "Cam_05.mp4",
        base_dir / "data" / "Cam_05.mp4",
    ]
    v_path = next((p for p in v_candidates if p and p.exists()), base_dir.parent / "data" / "Cam_05.mp4")

    m_candidates = [
        base_dir.parent / "models" / "best.pt",
        base_dir / "best.pt",
        base_dir.parent / "best.pt",
    ]
    m_path = next((p for p in m_candidates if p.exists()), base_dir.parent / "models" / "best.pt")

    z_candidates = [
        base_dir.parent / "data" / "Cam_05_sidewalk_result.json",
        base_dir / "Cam_05_sidewalk_result.json",
    ]
    z_path = next((p for p in z_candidates if p.exists()), None)

    run_realtime(v_path, m_path, start_frame=0, conf=0.25, poly_path=z_path)

if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent

    video_candidates = [
        base_dir / "Cam_05.mp4",
        base_dir / "sidewalk_encroachment" / "data" / "Cam_05.mp4",
        Path(r"d:\AI_box\Cam_05.mp4"),
    ]
    video_path = next((p for p in video_candidates if p.exists()), video_candidates[0])

    model_candidates = [
        base_dir / "best.pt",
        base_dir / "sidewalk_encroachment" / "models" / "best.pt",
        Path(r"d:\AI_box\best.pt"),
    ]
    model_path = next((p for p in model_candidates if p.exists()), model_candidates[0])

    json_candidates = [
        base_dir / "Cam_05_sidewalk_result.json",
        base_dir / "sidewalk_encroachment" / "data" / "Cam_05_sidewalk_result.json",
        video_path.parent / f"{video_path.stem}_sidewalk_result.json",
    ]
    json_path = next((p for p in json_candidates if p.exists()), None)

    parser = argparse.ArgumentParser(description="Chạy real-time video detection với Polygon vỉa hè & AdaptiveShadow")
    parser.add_argument("--mode", choices=["realtime", "single"], default="realtime",
                        help="Chế độ chạy: 'realtime' (mặc định) hoặc 'single' (thử nghiệm frame 1440)")
    parser.add_argument("--start-frame", type=int, default=0,
                        help="Frame bắt đầu (vd: 1300 để xem ngay đoạn tối)")
    parser.add_argument("--conf", type=float, default=0.25,
                        help="Ngưỡng confidence YOLO (mặc định: 0.25)")
    parser.add_argument("--video", type=str, default=str(video_path),
                        help="Đường dẫn file video")
    parser.add_argument("--model", type=str, default=str(model_path),
                        help="Đường dẫn file model YOLO (.pt)")
    parser.add_argument("--zone-json", type=str, default=str(json_path) if json_path.exists() else None,
                        help="Đường dẫn file JSON chứa toạ độ polygon vỉa hè")
    args = parser.parse_args()

    v_path = Path(args.video)
    m_path = Path(args.model)
    z_path = Path(args.zone_json) if args.zone_json else None

    if args.mode == "single":
        run_single_frame_experiment(v_path, m_path, frame_idx=args.start_frame or 1440, poly_path=z_path)
    else:
        run_realtime(v_path, m_path, start_frame=args.start_frame, conf=args.conf, poly_path=z_path)
