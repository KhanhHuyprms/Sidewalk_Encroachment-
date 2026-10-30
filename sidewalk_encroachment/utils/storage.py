"""
storage.py - Upload ảnh snapshot lên Storage API.

Trả về object_key để đặt vào snapshot_url của event.
"""
import io
import logging
import requests
from config import STORAGE_API_URL

logger = logging.getLogger("storage")


def upload_snapshot(
    image_bytes: bytes,
    camera_id: str,
    category: str = "anomaly",
    filename: str = "snapshot.jpg",
) -> str | None:
    """
    Upload ảnh lên Storage API.

    Returns:
        object_key nếu thành công, None nếu thất bại.
    """
    try:
        files = {
            "file": (filename, io.BytesIO(image_bytes), "image/jpeg"),
        }
        data = {
            "camera_id": camera_id,
            "category": category,
        }

        resp = requests.post(STORAGE_API_URL, files=files, data=data, timeout=15)

        if resp.status_code == 200:
            result = resp.json()
            if result.get("success"):
                object_key = result["object_key"]
                logger.info("Uploaded snapshot → %s", object_key)
                return object_key
            else:
                logger.warning("Upload failed: %s", result.get("error"))
                return None
        else:
            logger.warning("Upload HTTP %d: %s", resp.status_code, resp.text[:200])
            return None

    except requests.RequestException as e:
        logger.error("Upload error: %s", e)
        return None
