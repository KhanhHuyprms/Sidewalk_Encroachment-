"""
main.py - Entry point chính cho module Phát Hiện Lấn Chiếm Vỉa Hè (SIDEWALK_ENCROACHMENT).

Cách chạy:
    python main.py                     # Khởi chạy Pipeline toàn diện (kết nối VMS MQTT, đọc video/RTSP, detect, publish BBox & Event)
    python main.py --test              # Gửi thử 1 sự kiện + 1 BBox lên camera cam_sidewalk trên VMS
    python main.py --video [FILE]      # Chạy detect trực quan kèm giao diện OpenCV trên file video (mặc định data/Cam_05.mp4)
    python main.py --call-vms          # Kiểm tra kết nối trọn gói: query camera, upload snapshot, publish BBox & Event
    python main.py --experiment        # Chạy kiểm nghiệm so sánh 3 chế độ tiền xử lý ảnh (Gốc vs CLAHE vs Adaptive Shadow)
"""
import sys
import os
from pathlib import Path
import logging
import argparse

# ── Cấu hình hiển thị UTF-8 trên Windows console ──────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Thiết lập sys.path để các module nội bộ import liền mạch ──────
ROOT_DIR = Path(__file__).resolve().parent
for folder in [ROOT_DIR, ROOT_DIR / "config", ROOT_DIR / "utils", ROOT_DIR / "test"]:
    str_folder = str(folder)
    if str_folder not in sys.path:
        sys.path.insert(0, str_folder)

# ── Logging setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(ROOT_DIR / "sidewalk_encroachment.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def run_pipeline():
    """Chạy pipeline chính (kết nối MQTT, nhận camera, detect, publish)."""
    from pipeline import SidewalkPipeline
    pipeline = SidewalkPipeline()
    if pipeline.start():
        pipeline.run_forever()
    else:
        logger.error("Pipeline failed to start")
        sys.exit(1)


def run_test():
    """Gửi thử sự kiện lên camera cam_sidewalk."""
    from test_event import send_test_event
    send_test_event()


def run_video(video_path: str | None = None):
    """Chạy detect trên file video local (demo / debug trực quan)."""
    from video_demo import run_video_demo
    if not video_path:
        default_video = ROOT_DIR / "data" / "Cam_05.mp4"
        if default_video.exists():
            video_path = str(default_video)
        else:
            video_path = "data/Cam_05.mp4"
    run_video_demo(video_path)


def run_call_vms():
    """Kiểm tra kết nối toàn diện với VMS Staging."""
    from test_call_cam_sidewalk import main as run_call_test
    run_call_test()


def run_experiment(video_path: str | None = None):
    """Chạy so sánh 3 phương pháp tiền xử lý ảnh (Gốc vs CLAHE vs Adaptive Shadow)."""
    from test_clahe_experiment import run_experiment_demo
    if not video_path:
        default_video = ROOT_DIR / "data" / "Cam_05.mp4"
        video_path = str(default_video) if default_video.exists() else "data/Cam_05.mp4"
    run_experiment_demo(video_path)


def run_list_cameras():
    """Gọi VMS lấy và log danh sách camera zone 51."""
    from log_zone51_cameras import log_cameras
    log_cameras()


def main():
    parser = argparse.ArgumentParser(
        description="Sidewalk Encroachment Detection - AIOC Smart VMS Integration"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Gửi thử 1 event + bbox lên camera cam_sidewalk",
    )
    parser.add_argument(
        "--cameras", "--list-cameras", action="store_true", dest="list_cameras",
        help="Gọi VMS lấy và lưu log danh sách camera Zone 51 qua MQTT",
    )
    parser.add_argument(
        "--video", nargs="?", const="", default=None,
        help="Chạy detect trên file video (mặc định data/Cam_05.mp4 nếu để trống)",
    )
    parser.add_argument(
        "--call-vms", action="store_true",
        help="Kiểm tra tương tác đầy đủ với VMS: query cam_sidewalk, upload snapshot, publish BBox & Event",
    )
    parser.add_argument(
        "--experiment", action="store_true",
        help="Chạy so sánh trực quan 3 chế độ tiền xử lý ảnh (Gốc, CLAHE, Adaptive Shadow)",
    )

    args = parser.parse_args()

    if args.test:
        run_test()
    elif args.list_cameras:
        run_list_cameras()
    elif args.video is not None:
        video_file = args.video if args.video else None
        run_video(video_file)
    elif args.call_vms:
        run_call_vms()
    elif args.experiment:
        run_experiment()
    else:
        run_pipeline()


if __name__ == "__main__":
    main()
