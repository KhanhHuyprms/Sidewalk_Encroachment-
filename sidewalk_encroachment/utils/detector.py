"""
detector.py - Phát hiện lấn chiếm vỉa hè bằng YOLOv8 + polygon checking.

Logic:
  1. YOLOv8 detect vật thể (xe, hàng hoá, vật cản) trong mỗi frame.
  2. Kiểm tra toạ độ đáy (bottom-center) của bbox có nằm trong polygon vỉa hè.
  3. Theo dõi thời gian vật cản nằm trong vùng vỉa hè (tracker đơn giản bằng track_id).
  4. Khi vượt ngưỡng thời gian → sinh sự kiện SIDEWALK_ENCROACHMENT.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
from ultralytics import YOLO

from config import (
    YOLO_MODEL,
    CONFIDENCE_THRESHOLD,
    YOLO_IMGSZ,
    ENCROACHMENT_TIME_THRESHOLD,
    OBSTACLE_CLASSES,
    TARGET_OBSTACLE_CLASSES,
    COCO_CLASS_MAPPING,
    COCO_OBSTACLE_IDS,
    INFERENCE_DEVICE,
    USE_HALF_PRECISION,
)

logger = logging.getLogger("detector")


@dataclass
class TrackedObject:
    """Theo dõi một vật thể trong vùng vỉa hè."""
    track_id: int
    class_name: str
    first_seen: float          # timestamp lần đầu phát hiện trong polygon
    last_seen: float           # timestamp lần cuối
    bbox_norm: list[float]     # [x1, y1, x2, y2] normalised 0..1
    confidence: float
    event_fired: bool = False  # đã sinh sự kiện chưa


@dataclass
class Detection:
    """Một detection từ YOLO."""
    track_id: int
    class_name: str
    class_id: int
    confidence: float
    bbox_norm: list[float]     # [x1, y1, x2, y2] normalised 0..1
    bbox_px: list[int]         # [x1, y1, x2, y2] pixel
    in_polygon: bool = False


@dataclass
class EncroachmentEvent:
    """Sự kiện lấn chiếm vỉa hè để publish."""
    track_id: int
    class_name: str
    confidence: float
    bbox_norm: list[float]
    duration: float            # bao lâu nằm trong vùng (s)
    frame: Optional[np.ndarray] = field(default=None, repr=False)


class SidewalkDetector:
    """Detect lấn chiếm vỉa hè."""

    def __init__(
        self,
        model_path: str = YOLO_MODEL,
        conf_threshold: float = CONFIDENCE_THRESHOLD,
        imgsz: int = YOLO_IMGSZ,
        detect_classes: Optional[list[int]] = None,
        iou_threshold: float = 0.40,
        enhance_clahe: bool = False,
    ):
        logger.info("Loading YOLO model: %s (conf=%.2f, imgsz=%d, iou=%.2f, clahe=%s)",
                    model_path, conf_threshold, imgsz, iou_threshold, enhance_clahe)
        self.model = YOLO(model_path)
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.imgsz = imgsz
        self.iou_threshold = iou_threshold
        self.enhance_clahe = enhance_clahe

        self.device = INFERENCE_DEVICE
        if self.device != "cpu":
            try:
                self.model.to("cuda")
                if USE_HALF_PRECISION:
                    self.model.model.half()
                    logger.info("⚡ Đã kích hoạt FP16 Tensor Cores cho GPU NVIDIA RTX A1000.")
                # Warm-up CUDA GPU để triệt tiêu độ trễ của khung hình đầu tiên
                dummy = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
                self.model.predict(dummy, device=self.device, verbose=False)
                logger.info("🚀 CUDA Warm-up hoàn tất! GPU NVIDIA RTX A1000 sẵn sàng xử lý siêu tốc (~7ms / ~142 FPS).")
            except Exception as e:
                logger.warning("CUDA Warm-up warning: %s", e)

        # Xác định loại model: sidewalk_custom (best.pt), custom (7 nhãn), oiv7, hoặc COCO
        self.model_type = "coco"
        self.oiv7_mapping: dict[int, str] = {}
        self.allowed_classes: Optional[list[int]] = detect_classes

        if hasattr(self.model, "names") and isinstance(self.model.names, dict):
            names_set = set(self.model.names.values())
            # Kiểm tra xem có phải model sidewalk custom (best.pt)
            # {0: 'bicycle', 1: 'car', 2: 'motorbike', 3: 'object', 4: 'parking_zone', 5: 'person', 6: 'road', 7: 'sidewalk'}
            if "sidewalk" in names_set or "parking_zone" in names_set or ("object" in names_set and "motorbike" in names_set):
                self.model_type = "sidewalk_custom"
                if self.allowed_classes is None:
                    # Mặc định chỉ detect [0, 1, 2, 3, 5] (xe đạp, car, xe máy, object, person)
                    # Bỏ qua 4: parking_zone, 6: road, 7: sidewalk
                    self.allowed_classes = [0, 1, 2, 3, 5]
                class_desc = [f"{cid}:{self.model.names.get(cid)}" for cid in self.allowed_classes if cid in self.model.names]
                logger.info(
                    "Phát hiện Sidewalk Custom Model (%s) có %d classes. Chế độ: chỉ nhận diện class IDs %s (%s)",
                    model_path, len(names_set), self.allowed_classes, ", ".join(class_desc),
                )
            elif any(c in names_set for c in ["ad_board", "bin", "vendor_cart", "table_chair"]):
                self.model_type = "custom"
                logger.info("Phát hiện Custom Encroachment Model với %d classes: %s", len(names_set), list(names_set))
            elif "Billboard" in names_set or "Cart" in names_set or len(names_set) > 300:
                self.model_type = "oiv7"
                self.oiv7_mapping = self._build_oiv7_mapping()
                if self.allowed_classes is None:
                    self.allowed_classes = list(self.oiv7_mapping.keys())
                logger.info(
                    "Phát hiện Open Images V7 Model (%d classes) -> map %d classes sang target classes",
                    len(names_set), len(self.oiv7_mapping),
                )
            else:
                self.model_type = "coco"
                if self.allowed_classes is None:
                    self.allowed_classes = list(COCO_CLASS_MAPPING.keys())
                logger.info("Sử dụng COCO model (%d classes) -> map sang target classes", len(names_set))

        # {track_id: TrackedObject}
        self.tracked: dict[int, TrackedObject] = {}
        # Polygons (list of np.array, mỗi array shape (N, 2) pixel coords)
        self.polygons: list[np.ndarray] = []
        self.polygon_names: list[str] = []
        # Frame dimensions
        self._frame_w = 0
        self._frame_h = 0

    # ── Polygon management ────────────────────────────────────────
    def set_polygons(self, ai_zones: list[dict], frame_w: int = 1920, frame_h: int = 1080):
        """
        Nhận polygon từ ai_zones payload (toạ độ 0..1).
        Chuyển sang pixel coords cho pointPolygonTest.
        """
        self.polygons.clear()
        self.polygon_names.clear()
        self._frame_w = frame_w
        self._frame_h = frame_h

        for zone in ai_zones:
            if not zone.get("is_active", True):
                continue
            points = zone.get("points", [])
            if len(points) < 3:
                continue

            poly_pts = np.array(
                [[p["x"] * frame_w, p["y"] * frame_h] for p in points],
                dtype=np.float32,
            )
            self.polygons.append(poly_pts)
            self.polygon_names.append(zone.get("name", zone.get("code", "unknown")))
            logger.info(
                "Polygon loaded: %s (%d points)",
                self.polygon_names[-1], len(points),
            )

        logger.info("Total polygons: %d", len(self.polygons))

    def _point_in_any_polygon(self, x_px: float, y_px: float) -> bool:
        """Kiểm tra điểm (pixel) có nằm trong bất kỳ polygon nào."""
        for poly in self.polygons:
            if cv2.pointPolygonTest(poly, (x_px, y_px), False) >= 0:
                return True
        return False

    def _box_in_any_polygon(self, x1: float, y1: float, x2: float, y2: float) -> bool:
        """
        Kiểm tra bbox có nằm trong hoặc giao cắt vùng polygon không.
        Kiểm tra nhiều điểm trọng yếu để tránh bị hụt khi mép polygon cắt qua thân xe/bánh xe:
          - Trọng tâm (center): (cx, cy)
          - Đáy giữa (bottom-center): (cx, y2)
          - Phần thân dưới 3/4: (cx, y1 + 0.75 * h)
          - Góc đáy trái/phải: (x1, y2), (x2, y2)
        """
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        b34_y = y1 + 0.75 * (y2 - y1)

        test_points = [
            (cx, cy),      # Center
            (cx, y2),      # Bottom-center
            (cx, b34_y),   # Lower body
            (x1, y2),      # Bottom-left
            (x2, y2),      # Bottom-right
        ]

        for poly in self.polygons:
            for px, py in test_points:
                if cv2.pointPolygonTest(poly, (float(px), float(py)), False) >= 0:
                    return True
        return False

    def _build_oiv7_mapping(self) -> dict[int, str]:
        """Mapping Open Images V7 (601 classes) sang các lớp mục tiêu."""
        mapping = {}
        for cid, name in self.model.names.items():
            ln = name.lower()
            if ln in ["person", "man", "woman", "boy", "girl"]:
                mapping[cid] = "person"
            elif ln in ["motorcycle", "bicycle"]:
                mapping[cid] = "motorbike"
            elif ln in ["car", "truck", "bus", "land vehicle", "van"]:
                mapping[cid] = "car"
            elif ln in [
                "chair", "stool", "table", "bench", "desk", "couch",
                "studio couch", "coffee table", "kitchen & dining room table",
                "box", "cabinetry", "cupboard", "barrel", "filing cabinet", "bathroom cabinet"
            ]:
                mapping[cid] = "table_chair"
            elif ln in ["cart", "golf cart"]:
                mapping[cid] = "vendor_cart"
            elif ln in ["billboard", "traffic sign", "stop sign"]:
                mapping[cid] = "ad_board"
            elif "waste container" in ln or "trash" in ln:
                mapping[cid] = "bin"
            elif ln in ["umbrella", "tent"]:
                mapping[cid] = "umbrella"
        return mapping

    @staticmethod
    def enhance_shadows(frame: np.ndarray, clip_limit: float = 2.5, tile_grid_size: tuple = (8, 8)) -> np.ndarray:
        """
        Tăng sáng và cân bằng độ tương phản cục bộ vùng tối (CLAHE).
        Giúp model nhận diện rõ vật thể và xe máy trong bóng râm / dưới tán cây.
        """
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
        l_enh = clahe.apply(l)
        return cv2.cvtColor(cv2.merge([l_enh, a, b]), cv2.COLOR_LAB2BGR)

    # ── Detection ────────────────────────────────────────────────
    def detect(self, frame: np.ndarray) -> list[Detection]:
        """
        Chạy YOLO trên frame, trả danh sách Detection.
        Chỉ giữ các class quan tâm (bao gồm cả person để hiển thị).
        """
        h, w = frame.shape[:2]
        self._frame_w = w
        self._frame_h = h

        # Tiền xử lý CLAHE nếu bật
        infer_frame = self.enhance_shadows(frame) if self.enhance_clahe else frame

        track_kwargs = {
            "persist": True,
            "conf": self.conf_threshold,
            "imgsz": self.imgsz,
            "iou": self.iou_threshold,
            "device": self.device,
            "verbose": False,
        }
        if self.allowed_classes is not None:
            track_kwargs["classes"] = self.allowed_classes

        results = self.model.track(infer_frame, **track_kwargs)

        detections: list[Detection] = []

        if not results or results[0].boxes is None:
            return detections

        boxes = results[0].boxes
        for box in boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])

            if self.model_type == "sidewalk_custom":
                if self.allowed_classes is not None and cls_id not in self.allowed_classes:
                    continue
                class_name = self.model.names.get(cls_id, f"obj_{cls_id}")
            elif self.model_type == "custom":
                class_name = self.model.names.get(cls_id, f"obj_{cls_id}")
                if class_name not in OBSTACLE_CLASSES and class_name != "person":
                    continue
            elif self.model_type == "oiv7":
                if cls_id not in self.oiv7_mapping:
                    continue
                class_name = self.oiv7_mapping[cls_id]
            else:  # coco
                if cls_id not in COCO_CLASS_MAPPING:
                    continue
                class_name = COCO_CLASS_MAPPING[cls_id]

            x1, y1, x2, y2 = box.xyxy[0].tolist()

            # Track ID
            tid = int(box.id[0]) if box.id is not None else -1

            # Normalised coords
            x1n, y1n = x1 / w, y1 / h
            x2n, y2n = x2 / w, y2 / h

            # Kiểm tra đa điểm trong polygon
            in_poly = self._box_in_any_polygon(x1, y1, x2, y2)

            det = Detection(
                track_id=tid,
                class_name=class_name,
                class_id=cls_id,
                confidence=conf,
                bbox_norm=[round(x1n, 6), round(y1n, 6), round(x2n, 6), round(y2n, 6)],
                bbox_px=[int(x1), int(y1), int(x2), int(y2)],
                in_polygon=in_poly,
            )
            detections.append(det)

        return self._suppress_overlapping(detections)

    def _suppress_overlapping(
        self,
        detections: list[Detection],
        iou_thresh: float = 0.40,
        iomin_thresh: float = 0.50,
    ) -> list[Detection]:
        """
        Khử triệt để hiện tượng trùng lặp bounding box / hộp lồng nhau (Nested Boxes).
        Đặc biệt xử lý lỗi 1 xe máy bị chia thành nhiều hộp hoặc bị bóng râm/bộ phận sinh thêm hộp phụ.
        """
        if not detections or len(detections) <= 1:
            return detections

        sorted_dets = sorted(detections, key=lambda d: d.confidence, reverse=True)
        kept: list[Detection] = []

        for det in sorted_dets:
            x1, y1, x2, y2 = det.bbox_px
            area = (x2 - x1) * (y2 - y1)
            suppress = False

            for k in kept:
                # Kiểm tra trùng lặp nếu cùng class hoặc cùng nhóm phương tiện
                same_group = (
                    k.class_name == det.class_name
                    or (k.class_name in ["motorbike", "bicycle", "car"] and det.class_name in ["motorbike", "bicycle", "car"])
                )
                if not same_group:
                    continue

                kx1, ky1, kx2, ky2 = k.bbox_px
                k_area = (kx2 - kx1) * (ky2 - ky1)

                inter_w = max(0, min(x2, kx2) - max(x1, kx1))
                inter_h = max(0, min(by := y2, ky := ky2) - max(y1, ky1))
                inter_area = inter_w * inter_h

                if inter_area > 0:
                    union_area = area + k_area - inter_area
                    iou = inter_area / union_area if union_area > 0 else 0
                    iomin = inter_area / min(area, k_area) if min(area, k_area) > 0 else 0

                    if iou >= iou_thresh or iomin >= iomin_thresh:
                        suppress = True
                        break

            if not suppress:
                kept.append(det)

        return kept

    # ── Tracking & Event generation ──────────────────────────────
    def update_tracker(
        self,
        detections: list[Detection],
        frame: np.ndarray,
        current_time: Optional[float] = None,
    ) -> list[EncroachmentEvent]:
        """
        Cập nhật tracker. Trả về danh sách sự kiện lấn chiếm mới.
        Chỉ tính các class thuộc OBSTACLE_CLASSES (bỏ person).
        current_time: Tuỳ chọn timestamp (giây), mặc định dùng time.time()
        """
        now = time.time() if current_time is None else current_time
        events: list[EncroachmentEvent] = []

        # Set IDs hiện tại trong polygon
        current_ids = set()

        for det in detections:
            if det.track_id < 0:
                continue
            if det.class_name == "person":
                continue  # Person chỉ hiện bbox, không tính lấn chiếm
            if not det.in_polygon:
                continue
            if det.class_name not in OBSTACLE_CLASSES:
                continue

            current_ids.add(det.track_id)

            if det.track_id in self.tracked:
                obj = self.tracked[det.track_id]
                obj.last_seen = now
                obj.bbox_norm = det.bbox_norm
                obj.confidence = det.confidence

                # Kiểm tra ngưỡng thời gian
                duration = now - obj.first_seen
                if duration >= ENCROACHMENT_TIME_THRESHOLD and not obj.event_fired:
                    obj.event_fired = True
                    # Vẽ overlay chi tiết (polygon vỉa hè + bounding boxes vi phạm) lên ảnh snapshot bằng chứng
                    annotated_snapshot = self.draw_overlay(frame, detections, current_time=now)
                    # Vẽ thêm banner cảnh báo khẩn cấp ở góc trên ảnh
                    h_snap, w_snap = annotated_snapshot.shape[:2]
                    banner_text = f"CANH BAO LAN CHIEM VIA HE | TRACK #{det.track_id} [{det.class_name.upper()}] - {duration:.1f}s"
                    cv2.rectangle(annotated_snapshot, (0, 0), (w_snap, 45), (0, 0, 180), -1)
                    cv2.putText(
                        annotated_snapshot, banner_text, (15, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA
                    )

                    events.append(EncroachmentEvent(
                        track_id=det.track_id,
                        class_name=det.class_name,
                        confidence=det.confidence,
                        bbox_norm=det.bbox_norm,
                        duration=duration,
                        frame=annotated_snapshot,
                    ))
                    logger.info(
                        "🚨 Encroachment event: track=%d class=%s duration=%.1fs",
                        det.track_id, det.class_name, duration,
                    )
            else:
                # Vật cản mới trong polygon
                self.tracked[det.track_id] = TrackedObject(
                    track_id=det.track_id,
                    class_name=det.class_name,
                    first_seen=now,
                    last_seen=now,
                    bbox_norm=det.bbox_norm,
                    confidence=det.confidence,
                )

        # Xoá tracked objects đã rời polygon > 3s
        stale_ids = [
            tid for tid, obj in self.tracked.items()
            if tid not in current_ids and (now - obj.last_seen) > 3.0
        ]
        for tid in stale_ids:
            del self.tracked[tid]

        return events

    def draw_overlay(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        current_time: Optional[float] = None,
        only_in_polygon: bool = False,
    ) -> np.ndarray:
        """Vẽ polygons + bounding boxes lên frame (để debug / demo)."""
        now = time.time() if current_time is None else current_time
        vis = frame.copy()
        h, w = vis.shape[:2]

        # Vẽ polygons
        for poly in self.polygons:
            pts = poly.astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(vis, [pts], True, (0, 255, 255), 2)
            # Fill transparent
            overlay = vis.copy()
            cv2.fillPoly(overlay, [pts], (0, 255, 255))
            cv2.addWeighted(overlay, 0.15, vis, 0.85, 0, vis)

        # Vẽ detections
        for det in detections:
            if only_in_polygon and not det.in_polygon:
                continue

            x1, y1, x2, y2 = det.bbox_px
            color = (0, 0, 255) if det.in_polygon else (0, 255, 0)

            # Nếu đang tracked và chưa fire event → orange
            if det.track_id in self.tracked and not self.tracked[det.track_id].event_fired:
                elapsed = now - self.tracked[det.track_id].first_seen
                if elapsed > 0:
                    color = (0, 165, 255)  # orange

            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)

            label = f"{det.class_name} {det.confidence:.2f}"
            if det.track_id >= 0 and det.track_id in self.tracked:
                elapsed = now - self.tracked[det.track_id].first_seen
                label += f" [{elapsed:.0f}s]"

            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(vis, (x1, y1 - th - 6), (x1 + tw, y1), color, -1)
            cv2.putText(vis, label, (x1, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        return vis
