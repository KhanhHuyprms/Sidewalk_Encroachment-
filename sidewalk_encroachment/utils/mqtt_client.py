"""
mqtt_client.py - Kết nối MQTT broker qua WebSocket TLS.

Quản lý:
  - Kết nối + tự reconnect
  - Subscribe camera zone + ai_zones
  - Publish event / bbox
"""
import json
import ssl
import logging
import threading
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from config import (
    MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_CLIENT_ID,
    ZONE_ID, ORGANIZATION_ID,
    TOPIC_CAMERAS_ZONE, TOPIC_CAMERAS_GETDEVICE,
    AI_MODULE,
)

logger = logging.getLogger("mqtt_client")


class MQTTClient:
    """Wrapper quanh paho-mqtt, kết nối qua WebSocket TLS."""

    def __init__(self):
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            transport="websockets",
        )
        self._client.ws_set_options(path="/mqtt")
        self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        # Callback registry
        self._on_cameras_cb:  Optional[Callable] = None
        self._on_ai_zones_cb: Optional[Callable] = None
        self._subscribed_camera_topics: list[str] = []

        self._connected = threading.Event()

    # ── Callbacks registration ────────────────────────────────────
    def on_cameras(self, cb: Callable):
        """Đăng ký callback nhận danh sách camera."""
        self._on_cameras_cb = cb

    def on_ai_zones(self, cb: Callable):
        """Đăng ký callback nhận polygon ai_zones."""
        self._on_ai_zones_cb = cb

    # ── Connection ────────────────────────────────────────────────
    def connect(self):
        """Kết nối broker và bắt đầu network loop."""
        logger.info("Connecting to %s:%d ...", MQTT_BROKER, MQTT_PORT)
        self._client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
        self._client.loop_start()

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("Disconnected from broker.")

    def wait_connected(self, timeout=15):
        return self._connected.wait(timeout)

    # ── Subscribe ai_zones cho camera ────────────────────────────
    def subscribe_camera_zones(self, camera_codes: list[str]):
        """Subscribe ai_zones/{MODULE} cho danh sách camera."""
        # Unsubscribe cũ
        for t in self._subscribed_camera_topics:
            self._client.unsubscribe(t)
        self._subscribed_camera_topics.clear()

        for code in camera_codes:
            topic = f"smart_vms/cameras/{code}/ai_zones/{AI_MODULE}"
            self._client.subscribe(topic, qos=1)
            self._subscribed_camera_topics.append(topic)
            logger.info("Subscribed: %s", topic)

    # ── Publish ──────────────────────────────────────────────────
    def publish(self, topic: str, payload: dict, qos: int = 1):
        """Publish JSON payload."""
        msg = json.dumps(payload, ensure_ascii=False, default=str)
        result = self._client.publish(topic, msg, qos=qos)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.debug("Published → %s", topic)
        else:
            logger.warning("Publish failed → %s  rc=%s", topic, result.rc)
        return result

    def publish_bbox(self, topic: str, payload: dict):
        """Publish bbox (QoS 0, không chờ ack)."""
        msg = json.dumps(payload, ensure_ascii=False, default=str)
        self._client.publish(topic, msg, qos=0)

    # ── Internal callbacks ───────────────────────────────────────
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("Connected to broker (rc=%s).", rc)
            self._connected.set()
            # Subscribe camera zone (retained)
            client.subscribe(TOPIC_CAMERAS_ZONE, qos=1)
            logger.info("Subscribed: %s", TOPIC_CAMERAS_ZONE)

            # Request danh sách camera
            request = {"zone_id": ZONE_ID, "organization_id": ORGANIZATION_ID}
            client.publish(
                TOPIC_CAMERAS_GETDEVICE,
                json.dumps(request),
                qos=1,
            )
            logger.info("Requested cameras for zone %d", ZONE_ID)

            # Re-subscribe ai_zones nếu đã có
            for t in self._subscribed_camera_topics:
                client.subscribe(t, qos=1)
        else:
            logger.error("Connection failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected.clear()
        logger.warning("Disconnected (rc=%s). Auto-reconnect by paho.", rc)

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            # Xử lý an toàn cho payload rỗng (VMS gửi "" hoặc b"" khi xóa vùng / tắt module theo C03)
            if not msg.payload or len(msg.payload.strip()) == 0:
                logger.info("Nhận payload rỗng trên topic %s (VMS xóa vùng hoặc tắt module)", topic)
                if "/ai_zones/" in topic and self._on_ai_zones_cb:
                    self._on_ai_zones_cb(topic, {"ai_zones": []})
                return

            try:
                data = json.loads(msg.payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Payload không phải JSON trên topic %s -> xử lý an toàn như payload rỗng", topic)
                if "/ai_zones/" in topic and self._on_ai_zones_cb:
                    self._on_ai_zones_cb(topic, {"ai_zones": []})
                return

            # Danh sách camera zone
            if topic == TOPIC_CAMERAS_ZONE:
                logger.info("Received camera list on %s", topic)
                if self._on_cameras_cb:
                    self._on_cameras_cb(data)
                return

            # ai_zones/{MODULE}
            if "/ai_zones/" in topic:
                logger.info("Received ai_zones on %s", topic)
                if self._on_ai_zones_cb:
                    self._on_ai_zones_cb(topic, data)
                return

            logger.debug("Unhandled topic: %s", topic)

        except Exception:
            logger.exception("Error handling message on %s", msg.topic)
