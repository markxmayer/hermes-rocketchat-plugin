"""
Rocket.Chat Platform Adapter for Hermes Agent.

Hermes-native gateway plugin inspired by Jake Miller's MIT-licensed
rocketchat-openclaw transport, but implemented directly against Hermes'
BasePlatformAdapter interface.

Configuration in ~/.hermes/config.yaml::

    gateway:
      platforms:
        rocketchat:
          enabled: true
          token: "<bot auth token>"        # optional; env can be used instead
          extra:
            url: "https://chat.example.com"
            user_id: "<bot user id>"
            reply_mode: "thread"           # off | thread | auto
            auto_thread_chars: 280
            require_mention: false
            ack_reaction: "eyes"           # false/empty disables
            mark_as_read: true
            rooms:                         # optional per-room overrides
              ROOM_ID:
                require_mention: true
                reply_mode: "thread"

Environment variables override/seed config:
    ROCKETCHAT_URL
    ROCKETCHAT_USER_ID
    ROCKETCHAT_AUTH_TOKEN
    ROCKETCHAT_ALLOWED_USERS
    ROCKETCHAT_ALLOW_ALL_USERS
    ROCKETCHAT_HOME_CHANNEL
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import mimetypes
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult, cache_image_from_bytes
from gateway.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 12000
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2
_STALE_MESSAGE_AGE_SEC = 5 * 60
_MAX_INBOUND_MEDIA_BYTES = int(os.getenv("ROCKETCHAT_MAX_INBOUND_MEDIA_BYTES", str(25 * 1024 * 1024)))
_IMAGE_MIME_PREFIX = "image/"
_PERSISTENT_DEDUP_MAX_IDS = 2000


def _default_state_dir() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes") / "state"


def _load_e2e_module():
    spec = importlib.util.spec_from_file_location("rocketchat_e2e", Path(__file__).resolve().with_name("e2e.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Rocket.Chat E2E helper module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_url(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/websocket"


def _date_to_epoch(value: Any) -> Optional[float]:
    """Parse Rocket.Chat/Meteor timestamps into seconds since epoch."""
    if value is None:
        return None
    if isinstance(value, dict) and "$date" in value:
        try:
            raw = float(value["$date"])
            return raw / 1000 if raw > 10_000_000_000 else raw
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)):
        raw = float(value)
        return raw / 1000 if raw > 10_000_000_000 else raw
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            raw = float(text)
            return raw / 1000 if raw > 10_000_000_000 else raw
        try:
            # Rocket.Chat examples use ISO strings with Z.
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _room_type(rc_type: str | None) -> str:
    if rc_type == "d":
        return "dm"
    if rc_type in {"p", "g"}:
        return "group"
    return "channel"


def _strip_bot_mention(text: str, bot_username: str) -> str:
    if not text or not bot_username:
        return text or ""
    pattern = re.compile(rf"(^|\s)@{re.escape(bot_username)}\b[:,]?\s*", re.IGNORECASE)
    return pattern.sub(" ", text).strip()


@dataclass
class _RoomInfo:
    rid: str
    name: str = ""
    fname: str = ""
    t: str = "c"
    encrypted: bool = False
    e2e_key: str = ""
    e2e_suggested_key: str = ""
    e2e_key_id: str = ""

    @property
    def display_name(self) -> str:
        return self.fname or self.name or self.rid

    @property
    def chat_type(self) -> str:
        return _room_type(self.t)


class _DDPClient:
    """Small Rocket.Chat DDP client using the already-installed websockets package."""

    def __init__(self, adapter: "RocketChatAdapter") -> None:
        self.adapter = adapter
        self.ws: Any = None
        self._next_id = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._desired_rooms: set[str] = set()
        self._active_rooms: set[str] = set()
        self._connected = asyncio.Event()
        self._login_id: Optional[str] = None
        self._closing = False

    def next_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("Rocket.Chat DDP websocket is not connected")
        await self.ws.send(json.dumps(payload, separators=(",", ":")))

    async def connect_once(self) -> None:
        import websockets

        self._closing = False
        self._connected.clear()
        self._active_rooms.clear()
        url = _websocket_url(self.adapter.base_url)
        logger.info("Rocket.Chat: connecting realtime websocket to %s", url)
        async with websockets.connect(
            url,
            ping_interval=30,
            ping_timeout=20,
            close_timeout=10,
            user_agent_header="HermesAgent RocketChatAdapter/0.1",
        ) as ws:
            self.ws = ws
            await self.send_json({"msg": "connect", "version": "1", "support": ["1"]})
            async for raw in ws:
                await self._handle_raw(raw)
        self.ws = None

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            logger.debug("Rocket.Chat: ignored unparsable DDP frame")
            return

        kind = msg.get("msg")
        if kind == "ping":
            await self.send_json({"msg": "pong"})
            return
        if kind == "connected":
            # Rocket.Chat requires a DDP login before subscriptions. Do not
            # await a method result from inside the frame handler; the same
            # receive loop must remain free to process the later result frame.
            self._login_id = self.next_id()
            await self.send_json({
                "msg": "method",
                "method": "login",
                "id": self._login_id,
                "params": [{"resume": self.adapter.auth_token}],
            })
            return
        if kind == "result":
            msg_id = str(msg.get("id"))
            if self._login_id and msg_id == self._login_id:
                self._login_id = None
                if msg.get("error"):
                    raise RuntimeError(f"DDP login failed: {msg.get('error')}")
                self._connected.set()
                await self.resubscribe_all()
                return
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                if msg.get("error"):
                    fut.set_exception(RuntimeError(str(msg.get("error"))))
                else:
                    fut.set_result(msg.get("result"))
            return
        if kind == "changed" and msg.get("collection") == "stream-room-messages":
            fields = msg.get("fields") or {}
            for incoming in fields.get("args") or []:
                await self.adapter._handle_rc_message(incoming)
            return
        if kind == "nosub":
            logger.warning("Rocket.Chat: DDP subscription failed: %s", msg)

    async def call(self, method: str, params: list[Any] | None = None, timeout: int = 30) -> Any:
        msg_id = self.next_id()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[msg_id] = fut
        await self.send_json({"msg": "method", "method": method, "id": msg_id, "params": params or []})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def subscribe_room(self, rid: str) -> None:
        rid = str(rid or "").strip()
        if not rid:
            return
        self._desired_rooms.add(rid)
        if self.ws is None or not self._connected.is_set() or rid in self._active_rooms:
            return
        sub_id = self.next_id()
        self._active_rooms.add(rid)
        await self.send_json({
            "msg": "sub",
            "id": sub_id,
            "name": "stream-room-messages",
            "params": [rid, {"useCollection": False, "args": [{"lastUpdate": {"$date": int(time.time() * 1000)}}]}],
        })

    async def resubscribe_all(self) -> None:
        for rid in list(self._desired_rooms):
            await self.subscribe_room(rid)

    async def close(self) -> None:
        self._closing = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self.ws is not None:
            await self.ws.close()


class RocketChatAdapter(BasePlatformAdapter):
    """Rocket.Chat gateway adapter using REST API v1 + Realtime DDP."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        platform = Platform("rocketchat")
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}

        self.base_url = _normalize_url(extra.get("url") or extra.get("base_url") or os.getenv("ROCKETCHAT_URL", ""))
        self.user_id = str(extra.get("user_id") or os.getenv("ROCKETCHAT_USER_ID", "")).strip()
        self.auth_token = str(getattr(config, "token", None) or extra.get("auth_token") or os.getenv("ROCKETCHAT_AUTH_TOKEN", "")).strip()
        self.reply_mode = str(extra.get("reply_mode") or os.getenv("ROCKETCHAT_REPLY_MODE", "thread")).lower()
        self.auto_thread_chars = int(extra.get("auto_thread_chars") or os.getenv("ROCKETCHAT_AUTO_THREAD_CHARS", "280"))
        self.require_mention = bool(extra.get("require_mention", _truthy(os.getenv("ROCKETCHAT_REQUIRE_MENTION", "false"))))
        self.ack_reaction = extra.get("ack_reaction", os.getenv("ROCKETCHAT_ACK_REACTION", ""))
        self.mark_as_read = bool(extra.get("mark_as_read", _truthy(os.getenv("ROCKETCHAT_MARK_AS_READ", "false"))))
        self.backfill_on_connect = bool(extra.get("backfill_on_connect", _truthy(os.getenv("ROCKETCHAT_BACKFILL_ON_CONNECT", "true"))))
        self.backfill_window_seconds = int(extra.get("backfill_window_seconds") or os.getenv("ROCKETCHAT_BACKFILL_WINDOW_SECONDS", "300"))
        self.rooms_config: dict[str, Any] = extra.get("rooms", {}) if isinstance(extra.get("rooms"), dict) else {}
        e2e_cfg = extra.get("e2e", {}) if isinstance(extra.get("e2e"), dict) else {}
        self.e2e_enabled = bool(e2e_cfg.get("enabled", _truthy(os.getenv("ROCKETCHAT_E2E_ENABLED", "false"))))
        self.e2e_dm_only = bool(e2e_cfg.get("dm_only", _truthy(os.getenv("ROCKETCHAT_E2E_DM_ONLY", "true"))))
        self.e2e_password = str(e2e_cfg.get("password") or "")
        self.e2e_password_file = str(e2e_cfg.get("password_file") or os.getenv("ROCKETCHAT_E2E_PASSWORD_FILE", ""))
        self.e2e_auto_create_dm_key = bool(e2e_cfg.get("auto_create_dm_key", _truthy(os.getenv("ROCKETCHAT_E2E_AUTO_CREATE_DM_KEY", "false"))))
        self.e2e_force_unreadable_identity = bool(e2e_cfg.get("force_unreadable_identity", _truthy(os.getenv("ROCKETCHAT_E2E_FORCE_UNREADABLE_IDENTITY", "false"))))
        self.e2e_key_wait_attempts = int(e2e_cfg.get("key_wait_attempts") or os.getenv("ROCKETCHAT_E2E_KEY_WAIT_ATTEMPTS", "24"))
        self.e2e_key_wait_delay = float(e2e_cfg.get("key_wait_delay") or os.getenv("ROCKETCHAT_E2E_KEY_WAIT_DELAY", "1.25"))
        self.e2e_background_wait_attempts = int(e2e_cfg.get("background_wait_attempts") or os.getenv("ROCKETCHAT_E2E_BACKGROUND_WAIT_ATTEMPTS", "48"))
        self.e2e_background_wait_delay = float(e2e_cfg.get("background_wait_delay") or os.getenv("ROCKETCHAT_E2E_BACKGROUND_WAIT_DELAY", "2.5"))

        self._session: Any = None
        self._ddp = _DDPClient(self)
        self._ws_task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._closing = False
        self._bot_username = ""
        self._bot_name = ""
        self._rooms: dict[str, _RoomInfo] = {}
        self._e2e: Any = None
        self._e2e_module: Any = None
        self._e2e_armed_until: dict[str, float] = {}
        self._e2e_disable_after_reply: set[str] = set()
        self._e2e_persistent_rooms: set[str] = set()
        self._e2e_pending_ready_tasks: dict[str, asyncio.Task] = {}
        self._dedup = MessageDeduplicator(max_size=500, ttl_seconds=6 * 60 * 60)
        self._persistent_seen_path = Path(
            extra.get("dedup_state_file")
            or os.getenv("ROCKETCHAT_DEDUP_STATE_FILE", "")
            or (_default_state_dir() / "rocketchat_seen_messages.json")
        ).expanduser()
        self._persistent_seen_ids: set[str] = set()
        self._persistent_seen_order: list[str] = []
        self._load_persistent_seen_messages()

    @property
    def name(self) -> str:
        return "Rocket.Chat"

    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        import aiohttp
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        for attempt in range(4):
            async with self._session.get(url, headers=self._headers(), params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body_text = await resp.text()
                if resp.status in {429, 502, 503, 504} and attempt < 3:
                    await asyncio.sleep(self._retry_delay(resp, attempt))
                    continue
                if resp.status >= 400:
                    raise RuntimeError(f"GET {path} failed HTTP {resp.status}: {body_text[:300]}")
                return json.loads(body_text or "{}")
        return {}

    async def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import aiohttp
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        for attempt in range(4):
            async with self._session.post(url, headers=self._headers(), json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body_text = await resp.text()
                if resp.status in {429, 502, 503, 504} and attempt < 3:
                    await asyncio.sleep(self._retry_delay(resp, attempt))
                    continue
                if resp.status >= 400:
                    raise RuntimeError(f"POST {path} failed HTTP {resp.status}: {body_text[:300]}")
                return json.loads(body_text or "{}")
        return {}

    @staticmethod
    def _retry_delay(resp: Any, attempt: int) -> float:
        try:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                return min(float(retry_after), 30.0)
        except Exception:
            pass
        return min((2 ** attempt) + random.uniform(0, 0.5), 30.0)

    async def connect(self) -> bool:
        import aiohttp

        if not self.base_url or not self.user_id or not self.auth_token:
            logger.error("Rocket.Chat: ROCKETCHAT_URL, ROCKETCHAT_USER_ID, and ROCKETCHAT_AUTH_TOKEN are required")
            self._set_fatal_error("config_missing", "Rocket.Chat URL, user ID, or auth token missing", retryable=False)
            return False

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "HermesAgent RocketChatAdapter/0.1"},
        )
        self._closing = False
        try:
            me = await self._api_get("/api/v1/me")
            user = me.get("_id") and me or me.get("user", {})
            self._bot_username = str(user.get("username") or "")
            self._bot_name = str(user.get("name") or self._bot_username or self.user_id)
            logger.info("Rocket.Chat: authenticated as @%s (%s) on %s", self._bot_username, self.user_id, self.base_url)
            await self._init_e2e()
            await self._refresh_subscriptions()
            if self.backfill_on_connect:
                await self._backfill_recent_messages(window_seconds=self.backfill_window_seconds)
        except Exception as exc:
            logger.error("Rocket.Chat: authentication/subscription discovery failed: %s", exc)
            await self.disconnect()
            self._set_fatal_error("auth_failed", "Rocket.Chat authentication failed", retryable=False)
            return False

        self._ws_task = asyncio.create_task(self._realtime_loop())
        self._refresh_task = asyncio.create_task(self._subscription_refresh_loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._closing = True
        for task in (self._refresh_task, self._ws_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        for task in list(getattr(self, "_e2e_pending_ready_tasks", {}).values()):
            if task and not task.done():
                task.cancel()
        self._e2e_pending_ready_tasks.clear()
        await self._ddp.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._mark_disconnected()
        logger.info("Rocket.Chat: disconnected")

    async def _realtime_loop(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                await self._ddp.connect_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    break
                attempt += 1
                delay = min(_RECONNECT_BASE_DELAY * (2 ** min(attempt, 5)), _RECONNECT_MAX_DELAY)
                delay += random.uniform(0, _RECONNECT_JITTER * delay)
                logger.warning("Rocket.Chat: realtime disconnected (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _subscription_refresh_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(120)
                await self._refresh_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Rocket.Chat: subscription refresh failed: %s", exc)

    async def _refresh_subscriptions(self) -> None:
        data = await self._api_get("/api/v1/subscriptions.get")
        subs = data.get("update") or data.get("subscriptions") or []
        for sub in subs:
            rid = str(sub.get("rid") or "").strip()
            if not rid:
                continue
            self._rooms[rid] = _RoomInfo(
                rid=rid,
                name=str(sub.get("name") or ""),
                fname=str(sub.get("fname") or ""),
                t=str(sub.get("t") or "c"),
                # Keep the room encryption flag separate from cached key material.
                # A disabled room can retain E2EKey on the subscription; treating
                # that as encrypted makes /e2e skip rooms.saveRoomSettings and then
                # wait forever for a key we already have.
                encrypted=bool(sub.get("encrypted")),
                e2e_key=str(sub.get("E2EKey") or ""),
                e2e_suggested_key=str(sub.get("E2ESuggestedKey") or ""),
                e2e_key_id=str(sub.get("e2eKeyId") or sub.get("E2EKeyId") or ""),
            )
            await self._prepare_e2e_room(self._rooms[rid])
            await self._ddp.subscribe_room(rid)
        logger.info("Rocket.Chat: tracking %d subscribed rooms", len(self._rooms))

    @staticmethod
    def _history_endpoint_for_room(room: _RoomInfo) -> str:
        if room.t == "d":
            return "/api/v1/im.history"
        if room.t in {"p", "g"}:
            return "/api/v1/groups.history"
        return "/api/v1/channels.history"

    async def _backfill_recent_messages(self, *, window_seconds: int = 300) -> None:
        """Best-effort startup/reconnect backfill for messages missed while DDP was down."""
        if not self._rooms:
            return
        oldest_epoch = time.time() - max(1, int(window_seconds))
        oldest = datetime.fromtimestamp(oldest_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        for room in list(self._rooms.values()):
            try:
                data = await self._api_get(
                    self._history_endpoint_for_room(room),
                    params={
                        "roomId": room.rid,
                        "oldest": oldest,
                        "inclusive": "true",
                        "count": 50,
                        "showThreadMessages": "true",
                    },
                )
                messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
                messages.sort(key=lambda m: _date_to_epoch(m.get("ts")) or 0)
                for msg in messages:
                    msg = dict(msg)
                    msg["__hermes_backfill"] = True
                    await self._handle_rc_message(msg)
            except Exception as exc:
                logger.debug("Rocket.Chat: backfill failed for room %s: %s", room.rid, exc)

    def _load_persistent_seen_messages(self) -> None:
        """Load recently handled Rocket.Chat IDs so clean restarts do not replay backfill."""
        self._persistent_seen_ids = set()
        self._persistent_seen_order = []
        path = getattr(self, "_persistent_seen_path", None)
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            ids = data.get("ids") if isinstance(data, dict) else data
            if not isinstance(ids, list):
                return
            for msg_id in ids[-_PERSISTENT_DEDUP_MAX_IDS:]:
                msg_id = str(msg_id or "").strip()
                if msg_id and msg_id not in self._persistent_seen_ids:
                    self._persistent_seen_ids.add(msg_id)
                    self._persistent_seen_order.append(msg_id)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.debug("Rocket.Chat: persistent dedup state could not be loaded: %s", exc)

    def _remember_persistent_seen_message(self, msg_id: str) -> bool:
        """Return True for restart-persistent duplicates; otherwise record *msg_id*."""
        msg_id = str(msg_id or "").strip()
        if not msg_id:
            return False
        if not hasattr(self, "_persistent_seen_ids"):
            self._persistent_seen_ids = set()
            self._persistent_seen_order = []
        if msg_id in self._persistent_seen_ids:
            return True
        self._persistent_seen_ids.add(msg_id)
        self._persistent_seen_order.append(msg_id)
        if len(self._persistent_seen_order) > _PERSISTENT_DEDUP_MAX_IDS:
            self._persistent_seen_order = self._persistent_seen_order[-_PERSISTENT_DEDUP_MAX_IDS:]
            self._persistent_seen_ids = set(self._persistent_seen_order)
        path = getattr(self, "_persistent_seen_path", None)
        if path:
            try:
                path = Path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                tmp.write_text(
                    json.dumps({"ids": self._persistent_seen_order, "updated_at": datetime.now(timezone.utc).isoformat()}),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            except Exception as exc:
                logger.debug("Rocket.Chat: persistent dedup state could not be saved: %s", exc)
        return False

    async def _init_e2e(self) -> None:
        if not self.e2e_enabled:
            return
        try:
            self._e2e_module = _load_e2e_module()
            password, password_path, password_created = self._e2e_module.load_or_create_e2e_password(
                explicit=self.e2e_password,
                file_path=self.e2e_password_file,
            )
            if password_created:
                logger.info("Rocket.Chat: generated local E2E recovery password file at %s", password_path)
            elif password_path:
                logger.info("Rocket.Chat: loaded local E2E recovery password file at %s", password_path)
            self._e2e = self._e2e_module.RocketChatE2E(
                user_id=self.user_id,
                password=password,
                rest_get=self._api_get,
                rest_post=self._api_post,
                ddp_call=self._ddp.call,
                force_unreadable_identity=self.e2e_force_unreadable_identity,
            )
            await self._e2e.start()
            logger.info("Rocket.Chat: E2E helper initialized for DM-capable encrypted rooms")
        except Exception as exc:
            self._e2e = None
            logger.error("Rocket.Chat: E2E initialization failed: %s", exc)

    def _e2e_supported_for_room(self, room: _RoomInfo) -> bool:
        if not self.e2e_enabled or not self._e2e:
            return False
        if self.e2e_dm_only and room.t != "d":
            return False
        return room.t in {"d", "p"}

    def _e2e_allowed_for_room(self, room: _RoomInfo) -> bool:
        return self._e2e_supported_for_room(room) and bool(room.encrypted)

    async def _set_room_encrypted(self, room: _RoomInfo, encrypted: bool) -> None:
        if room.encrypted is encrypted:
            return
        await self._api_post("/api/v1/rooms.saveRoomSettings", {"rid": room.rid, "encrypted": encrypted})
        room.encrypted = encrypted
        self._rooms[room.rid] = room

    async def _refresh_room_info(self, room: _RoomInfo) -> _RoomInfo:
        try:
            data = await self._api_get("/api/v1/rooms.info", params={"roomId": room.rid})
            info = data.get("room") or {}
            if isinstance(info, dict):
                if "encrypted" in info:
                    room.encrypted = bool(info.get("encrypted"))
                key_id = str(info.get("e2eKeyId") or info.get("E2EKeyId") or "")
                if key_id:
                    room.e2e_key_id = key_id
                self._rooms[room.rid] = room
        except Exception as exc:
            logger.debug("Rocket.Chat: rooms.info refresh failed for %s: %s", room.rid, exc)
        return room

    async def _wait_for_e2e_room_key(self, room: _RoomInfo, *, attempts: int | None = None, delay: float | None = None) -> bool:
        attempts = max(1, int(attempts if attempts is not None else getattr(self, "e2e_key_wait_attempts", 24)))
        delay = float(delay if delay is not None else getattr(self, "e2e_key_wait_delay", 1.25))
        for _ in range(attempts):
            await asyncio.sleep(delay)
            await self._refresh_subscriptions()
            room = await self._refresh_room_info(self._rooms.get(room.rid, room))
            if await self._prepare_e2e_room(room):
                return True
        return False

    def _e2e_ready_message(self, *, persistent: bool) -> str:
        if persistent:
            return "E2E persistent mode ready. I will keep this DM encrypted until you send `e2e_off` as an encrypted message."
        return "E2E ready. Send one encrypted message now; I will answer encrypted and then return the DM to normal mode."

    def _arm_e2e_room(self, room: _RoomInfo, *, persistent: bool) -> None:
        if persistent:
            self._e2e_armed_until.pop(room.rid, None)
            self._e2e_disable_after_reply.discard(room.rid)
            self._e2e_persistent_rooms.add(room.rid)
        else:
            self._e2e_persistent_rooms.discard(room.rid)
            self._e2e_armed_until[room.rid] = time.time() + 5 * 60

    def _schedule_e2e_ready_watch(self, room: _RoomInfo, *, persistent: bool) -> None:
        existing = getattr(self, "_e2e_pending_ready_tasks", {}).get(room.rid)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._e2e_ready_watch_loop(room, persistent=persistent))
        self._e2e_pending_ready_tasks[room.rid] = task

    async def _e2e_ready_watch_loop(self, room: _RoomInfo, *, persistent: bool) -> None:
        try:
            attempts = max(1, int(getattr(self, "e2e_background_wait_attempts", 48)))
            delay = float(getattr(self, "e2e_background_wait_delay", 2.5))
            if await self._wait_for_e2e_room_key(room, attempts=attempts, delay=delay):
                room = self._rooms.get(room.rid, room)
                self._arm_e2e_room(room, persistent=persistent)
                await self._send_plain_text(room.rid, self._e2e_ready_message(persistent=persistent))
                logger.info("Rocket.Chat: delayed E2E room key became ready for %s", room.rid)
                return
            await self._send_plain_text(
                room.rid,
                "E2E key sharing still has not completed. Please try `/e2e` again, or unlock/reopen this DM in your Rocket.Chat client so it can share the room key.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Rocket.Chat: delayed E2E key wait failed for %s: %s", room.rid, exc)
        finally:
            getattr(self, "_e2e_pending_ready_tasks", {}).pop(room.rid, None)

    async def _ensure_e2e_exchange_ready(self, room: _RoomInfo, *, wait_for_key: bool = True) -> tuple[bool, str]:
        if not self._e2e_supported_for_room(room):
            return False, "E2E is not initialized or this room type is not supported."
        try:
            if not room.encrypted:
                await self._set_room_encrypted(room, True)
                await self._refresh_subscriptions()
                room = await self._refresh_room_info(self._rooms.get(room.rid, room))
            else:
                room = await self._refresh_room_info(room)
            if await self._prepare_e2e_room(room):
                try:
                    shared = await self._e2e.distribute_room_key(room.rid)
                    if shared:
                        logger.info("Rocket.Chat: shared E2E room key for %s with %d participant(s)", room.rid, shared)
                except Exception as exc:
                    logger.debug("Rocket.Chat: E2E key distribution failed for %s: %s", room.rid, exc)
                return True, "E2E ready. Send one encrypted message now; I will answer encrypted and then return the DM to normal mode."
            if self._e2e and hasattr(self._e2e, "request_subscription_keys"):
                try:
                    await self._e2e.request_subscription_keys()
                except Exception as exc:
                    logger.debug("Rocket.Chat: E2E subscription-key request failed for %s: %s", room.rid, exc)
                if self._e2e and hasattr(self._e2e, "queue_me_for_room_keys"):
                    try:
                        if await self._e2e.queue_me_for_room_keys():
                            logger.info("Rocket.Chat: queued self for E2E room-key sharing in encrypted rooms")
                    except Exception as exc:
                        logger.debug("Rocket.Chat: E2E self queue request failed for %s: %s", room.rid, exc)
                if room.e2e_key_id and hasattr(self._e2e, "request_room_key"):
                    try:
                        await self._e2e.request_room_key(room.rid, room.e2e_key_id)
                        logger.info("Rocket.Chat: requested E2E room key %s for %s", room.e2e_key_id, room.rid)
                    except Exception as exc:
                        logger.debug("Rocket.Chat: E2E room-key request failed for %s: %s", room.rid, exc)
                if not wait_for_key:
                    return False, "E2E key sharing has been requested; waiting for the room key in the background."
                if await self._wait_for_e2e_room_key(room):
                    return True, "E2E ready. Send one encrypted message now; I will answer encrypted and then return the DM to normal mode."
            if not wait_for_key:
                return False, "E2E key sharing has been requested; waiting for the room key in the background."
            if room.t == "d" and room.e2e_key_id and hasattr(self._e2e, "reset_room_key"):
                try:
                    logger.info("Rocket.Chat: resetting stale/missing E2E room key for %s", room.rid)
                    await self._e2e.reset_room_key(room.rid)
                    await self._refresh_subscriptions()
                    room = await self._refresh_room_info(self._rooms.get(room.rid, room))
                    try:
                        shared = await self._e2e.distribute_room_key(room.rid)
                        if shared:
                            logger.info("Rocket.Chat: shared reset E2E room key for %s with %d participant(s)", room.rid, shared)
                    except Exception as exc:
                        logger.debug("Rocket.Chat: reset E2E key distribution failed for %s: %s", room.rid, exc)
                    return True, "E2E ready. Send one encrypted message now; I will answer encrypted and then return the DM to normal mode."
                except Exception as exc:
                    logger.warning("Rocket.Chat: failed to reset missing E2E room key for %s: %s", room.rid, exc)
            if room.t == "d" and not room.e2e_key_id:
                try:
                    await self._e2e.create_room_key(room.rid)
                    await self._refresh_subscriptions()
                    return True, "E2E ready. Send one encrypted message now; I will answer encrypted and then return the DM to normal mode."
                except Exception as exc:
                    if "error-room-e2e-key-already-exists" not in str(exc):
                        raise
                    logger.info("Rocket.Chat: room %s already has an E2E key; waiting for suggested key distribution", room.rid)
            return False, "E2E is enabled, but I do not have this room key yet. I requested it from Rocket.Chat; please keep this DM focused/unlocked for key sharing, or disable E2E and send /e2e again so I can rotate a fresh one-shot key."
        except Exception as exc:
            detail = repr(exc) if not str(exc) else str(exc)
            logger.warning("Rocket.Chat: failed to prepare one-shot E2E exchange for %s: %s", room.rid, detail)
            return False, f"I could not prepare E2E for this room: {detail}"

    async def _prepare_e2e_room(self, room: _RoomInfo) -> bool:
        if not self._e2e_allowed_for_room(room):
            return False
        if self._e2e.have_room(room.rid):
            return True
        try:
            if room.e2e_key:
                return bool(self._e2e.import_room_key(room.rid, room.e2e_key))
            if room.e2e_suggested_key:
                return bool(await self._e2e.accept_suggested_key(room.rid, room.e2e_suggested_key))
            if room.t == "d" and self.e2e_auto_create_dm_key and not room.e2e_key_id:
                await self._e2e.create_room_key(room.rid)
                return True
        except Exception as exc:
            logger.warning("Rocket.Chat: failed to prepare E2E room %s: %s", room.rid, exc)
        return False

    async def _decrypt_e2e_message(self, msg: dict[str, Any], room: _RoomInfo) -> Optional[dict[str, Any]]:
        if not await self._prepare_e2e_room(room):
            logger.warning("Rocket.Chat: encrypted message in %s could not be decrypted; missing room key", room.rid)
            return None
        try:
            return self._e2e.decrypt_message(msg)
        except Exception as exc:
            logger.warning("Rocket.Chat: encrypted message in %s could not be decrypted: %s", room.rid, exc)
            return None

    async def _send_plain_text(self, chat_id: str, content: str) -> SendResult:
        try:
            data = await self._api_post("/api/v1/chat.postMessage", {"roomId": str(chat_id), "text": content})
            msg = data.get("message") or {}
            return SendResult(success=True, message_id=str(msg.get("_id") or data.get("_id") or "") or None)
        except Exception as exc:
            logger.error("Rocket.Chat: plaintext control send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _send_e2e_chunk(self, room: _RoomInfo, chunk: str) -> dict[str, Any]:
        if not await self._prepare_e2e_room(room):
            raise RuntimeError("encrypted room key is unavailable")
        message = self._e2e.encrypt_message_payload(room.rid, chunk)
        data = await self._api_post("/api/v1/chat.sendMessage", {"message": message})
        if data.get("success") is False:
            raise RuntimeError(str(data.get("error") or "encrypted send failed"))
        return data

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if not content:
            return SendResult(success=True)
        chunks = self.truncate_message(self.format_message(content), self.MAX_MESSAGE_LENGTH)
        metadata = metadata or {}
        thread_id = metadata.get("thread_id") or metadata.get("tmid") or reply_to
        room = self._rooms.get(str(chat_id))
        use_e2e = bool(room and self._e2e_allowed_for_room(room))
        last_id = None
        for chunk in chunks:
            try:
                if use_e2e and room:
                    if thread_id:
                        logger.debug("Rocket.Chat: sending encrypted DM reply without thread metadata; Rocket.Chat E2E thread support is intentionally disabled")
                    data = await self._send_e2e_chunk(room, chunk)
                else:
                    payload: dict[str, Any] = {"roomId": str(chat_id), "text": chunk}
                    if thread_id and self._should_thread(chunk):
                        payload["tmid"] = str(thread_id)
                    data = await self._api_post("/api/v1/chat.postMessage", payload)
                msg = data.get("message") or {}
                last_id = msg.get("_id") or data.get("_id") or last_id
            except Exception as exc:
                logger.error("Rocket.Chat: send failed: %s", exc)
                return SendResult(success=False, error=str(exc))
        if room and room.rid in self._e2e_disable_after_reply and room.rid not in self._e2e_persistent_rooms:
            self._e2e_disable_after_reply.discard(room.rid)
            self._e2e_armed_until.pop(room.rid, None)
            try:
                await self._set_room_encrypted(room, False)
            except Exception as exc:
                logger.warning("Rocket.Chat: failed to disable one-shot E2E room %s after reply: %s", room.rid, exc)
        return SendResult(success=True, message_id=str(last_id) if last_id else None)

    def _should_thread(self, text: str) -> bool:
        if self.reply_mode in {"off", "channel", "none", "false"}:
            return False
        if self.reply_mode == "auto":
            return len(text) >= self.auto_thread_chars or text.count("\n") >= 3
        return True

    async def _api_upload_media(self, rid: str, file_path: str, *, file_name: Optional[str] = None, content_type: Optional[str] = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Rocket.Chat HTTP session is not connected")
        import aiohttp

        path_obj = Path(file_path).expanduser()
        if not path_obj.is_file():
            raise FileNotFoundError(str(path_obj))
        upload_name = file_name or path_obj.name
        media_type = content_type or mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        url = urljoin(self.base_url + "/", f"api/v1/rooms.media/{quote(str(rid), safe='')}")
        headers = dict(self._headers())
        headers.pop("Content-Type", None)  # aiohttp sets multipart boundary.
        for attempt in range(4):
            form = aiohttp.FormData()
            with path_obj.open("rb") as fh:
                form.add_field("file", fh, filename=upload_name, content_type=media_type)
                async with self._session.post(url, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    body_text = await resp.text()
                    if resp.status in {429, 502, 503, 504} and attempt < 3:
                        await asyncio.sleep(self._retry_delay(resp, attempt))
                        continue
                    if resp.status >= 400:
                        raise RuntimeError(f"POST /api/v1/rooms.media/{{rid}} failed HTTP {resp.status}: {body_text[:300]}")
                    return json.loads(body_text or "{}")
        return {}

    async def _upload_and_confirm_media(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = metadata or {}
        try:
            upload = await self._api_upload_media(str(chat_id), file_path, file_name=file_name)
            file_obj = upload.get("file") or {}
            file_id = str(file_obj.get("_id") or upload.get("fileId") or "").strip()
            if not file_id:
                return SendResult(success=False, error="Rocket.Chat media upload did not return a file id", raw_response=upload)
            payload: dict[str, Any] = {"msg": caption or ""}
            thread_id = metadata.get("thread_id") or metadata.get("tmid") or reply_to
            if thread_id:
                payload["tmid"] = str(thread_id)
            confirm = await self._api_post(f"/api/v1/rooms.mediaConfirm/{quote(str(chat_id), safe='')}/{quote(file_id, safe='')}", payload)
            msg = confirm.get("message") or {}
            msg_id = msg.get("_id") or confirm.get("_id") or file_id
            return SendResult(success=True, message_id=str(msg_id), raw_response=confirm)
        except Exception as exc:
            logger.error("Rocket.Chat: native media upload failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _download_remote_media_to_temp(self, url: str, *, suffix: str = "") -> tuple[str, str]:
        if self._session is None:
            raise RuntimeError("Rocket.Chat HTTP session is not connected")
        if not str(url).startswith(("http://", "https://")):
            raise ValueError("remote media URL must be http(s)")
        import aiohttp

        parsed = urlsplit(url)
        name = Path(unquote(parsed.path)).name or "remote-media"
        if not suffix:
            suffix = Path(name).suffix
        headers = {"Accept": "image/*,video/*,audio/*,application/octet-stream,*/*;q=0.8"}
        async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            body = await resp.read()
            if resp.status >= 400:
                snippet = body[:120].decode("utf-8", errors="replace")
                raise RuntimeError(f"GET remote media failed HTTP {resp.status}: {snippet}")
            size_header = str(resp.headers.get("Content-Length") or "").strip()
            if size_header and int(size_header) > _MAX_INBOUND_MEDIA_BYTES:
                raise RuntimeError(f"remote media file too large: {size_header} bytes")
            if len(body) > _MAX_INBOUND_MEDIA_BYTES:
                raise RuntimeError(f"remote media file too large: {len(body)} bytes")
            response_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not suffix and response_type:
                suffix = mimetypes.guess_extension(response_type) or ""
            fd, tmp_path = tempfile.mkstemp(prefix="rocketchat-remote-", suffix=suffix or Path(name).suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            upload_name = name if Path(name).suffix else f"{name}{suffix}"
            return tmp_path, upload_name

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if image_url.startswith("file://"):
            return await self.send_image_file(chat_id, unquote(image_url[7:]), caption=caption, reply_to=reply_to, metadata=metadata)
        if image_url.startswith(("http://", "https://")):
            tmp_path, upload_name = await self._download_remote_media_to_temp(image_url, suffix=Path(urlsplit(image_url).path).suffix or ".png")
            try:
                return await self._upload_and_confirm_media(chat_id, tmp_path, caption=caption, file_name=upload_name, reply_to=reply_to, metadata=metadata)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return await self.send(chat_id=chat_id, content=f"{caption}\n{image_url}" if caption else image_url, reply_to=reply_to, metadata=metadata)

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self.send_image(chat_id, animation_url, caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: list[tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        for image_url, alt_text in images:
            if human_delay > 0:
                await asyncio.sleep(human_delay)
            await self.send_image(chat_id, image_url, caption=alt_text or None, metadata=metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_confirm_media(
            chat_id,
            image_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_confirm_media(
            chat_id,
            file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_confirm_media(
            chat_id,
            video_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._upload_and_confirm_media(
            chat_id,
            audio_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        try:
            # Rocket.Chat's documented fallback is REST /api/v1/typing.
            await self._api_post("/api/v1/typing", {"roomId": str(chat_id), "typing": True})
        except Exception:
            logger.debug("Rocket.Chat: typing indicator failed", exc_info=True)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            data = await self._api_post(
                "/api/v1/chat.delete",
                {"roomId": str(chat_id), "msgId": str(message_id), "asUser": False},
            )
            return data.get("success") is not False
        except Exception:
            logger.debug("Rocket.Chat: delete message failed", exc_info=True)
            return False

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        text = f"⚠️ {title}\n\n{message}\n\nReply with /approve, /always, or /cancel."
        return await self.send(chat_id=chat_id, content=text, metadata=metadata)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if choices:
            try:
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
            except Exception:
                logger.debug("Rocket.Chat: clarify text-capture registration failed", exc_info=True)
            lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices, start=1):
                lines.append(f"{i}. {choice}")
            lines.extend(["", "Reply with the number, option text, or your own answer."])
            text = "\n".join(lines)
        else:
            text = f"❓ {question}"
        return await self.send(chat_id=chat_id, content=text, metadata=metadata)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        try:
            data = await self._api_post("/api/v1/chat.update", {"roomId": str(chat_id), "msgId": str(message_id), "text": content})
            if data.get("success") is False:
                return SendResult(success=False, error=str(data.get("error") or "edit failed"))
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        room = self._rooms.get(str(chat_id))
        if room:
            return {"name": room.display_name, "type": room.chat_type, "chat_id": room.rid}
        return {"name": str(chat_id), "type": "channel", "chat_id": str(chat_id)}

    def format_message(self, content: str) -> str:
        # Rocket.Chat supports standard Markdown. When the gateway extracts a
        # local image/file for native upload, it can leave an empty markdown
        # image stub like ![alt](); remove that instead of displaying it.
        text = re.sub(r"!\[[^\]]*\]\(\s*\)", "", content or "")
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _room_cfg(self, rid: str) -> dict[str, Any]:
        cfg = self.rooms_config.get(rid, {})
        return cfg if isinstance(cfg, dict) else {}

    def _message_mentions_bot(self, msg: dict[str, Any], text: str) -> bool:
        if not self._bot_username:
            return False
        if re.search(rf"(^|\s)@{re.escape(self._bot_username)}\b", text or "", re.IGNORECASE):
            return True
        for mention in msg.get("mentions") or []:
            if mention.get("_id") == self.user_id or str(mention.get("username") or "").lower() == self._bot_username.lower():
                return True
        return False

    def _should_process_room_message(self, rid: str, msg: dict[str, Any], text: str, room: _RoomInfo) -> bool:
        room_cfg = self._room_cfg(rid)
        require_mention = bool(room_cfg.get("require_mention", self.require_mention))
        if room.chat_type == "dm":
            return True
        if require_mention:
            return self._message_mentions_bot(msg, text)
        return True

    def _absolute_media_url(self, raw_url: str) -> str:
        raw_url = str(raw_url or "").strip()
        if not raw_url:
            return ""
        if raw_url.startswith(("http://", "https://")):
            return raw_url
        return urljoin(self.base_url + "/", raw_url.lstrip("/"))

    @staticmethod
    def _media_filename(candidate: dict[str, Any]) -> str:
        for key in ("name", "title", "filename"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return Path(unquote(value)).name or "rocketchat-upload"
        url = str(candidate.get("url") or "").strip()
        if url:
            return Path(unquote(urlsplit(url).path)).name or "rocketchat-upload"
        return "rocketchat-upload"

    @staticmethod
    def _media_content_type(candidate: dict[str, Any]) -> str:
        for key in ("type", "image_type", "content_type", "mime"):
            value = str(candidate.get(key) or "").split(";", 1)[0].strip().lower()
            if value:
                return value
        filename = RocketChatAdapter._media_filename(candidate)
        guessed, _ = mimetypes.guess_type(filename)
        return (guessed or "application/octet-stream").lower()

    def _iter_inbound_media_candidates(self, msg: dict[str, Any]) -> Iterable[dict[str, Any]]:
        """Yield Rocket.Chat file candidates, preferring original upload links."""
        files: list[dict[str, Any]] = []
        file_obj = msg.get("file")
        if isinstance(file_obj, dict):
            files.append(file_obj)
        for item in msg.get("files") or []:
            if isinstance(item, dict):
                files.append(item)

        file_by_id = {str(f.get("_id") or ""): f for f in files if f.get("_id")}
        file_by_name = {str(f.get("name") or ""): f for f in files if f.get("name")}

        for attachment in msg.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            raw_url = str(attachment.get("title_link") or "").strip()
            if not raw_url:
                # Preview thumbnails are a fallback only; original title_link is better for vision.
                raw_url = str(attachment.get("image_url") or "").strip()
            if not raw_url:
                continue
            title = str(attachment.get("title") or "").strip()
            file_id = ""
            parts = [p for p in urlsplit(raw_url).path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "file-upload":
                file_id = parts[1]
            file_meta = file_by_id.get(file_id) or file_by_name.get(title) or {}
            yield {
                "url": self._absolute_media_url(raw_url),
                "name": title or file_meta.get("name") or Path(unquote(urlsplit(raw_url).path)).name,
                "type": file_meta.get("type") or attachment.get("image_type") or attachment.get("type"),
            }

        for file_meta in files:
            file_id = str(file_meta.get("_id") or "").strip()
            name = str(file_meta.get("name") or "").strip()
            if not file_id or not name:
                continue
            yield {
                "url": self._absolute_media_url(f"/file-upload/{quote(file_id)}/{quote(name)}"),
                "name": name,
                "type": file_meta.get("type"),
            }

    async def _extract_inbound_media(self, msg: dict[str, Any]) -> tuple[list[str], list[str]]:
        media_paths: list[str] = []
        media_types: list[str] = []
        seen_urls: set[str] = set()
        for candidate in self._iter_inbound_media_candidates(msg):
            url = str(candidate.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            content_type = self._media_content_type(candidate)
            if not content_type.startswith(_IMAGE_MIME_PREFIX):
                continue
            filename = self._media_filename(candidate)
            try:
                cached_path, cached_type = await self._download_inbound_media(url, filename, content_type)
            except Exception as exc:
                logger.warning("Rocket.Chat: failed to download inbound media %s: %s", urlsplit(url).path, exc)
                continue
            media_paths.append(cached_path)
            media_types.append(cached_type or content_type)
        return media_paths, media_types

    async def _download_inbound_media(self, url: str, filename: str, content_type: str) -> tuple[str, str]:
        """Download one authenticated Rocket.Chat image and cache it locally."""
        if self._session is None:
            raise RuntimeError("Rocket.Chat HTTP session is not connected")
        import aiohttp

        headers = dict(self._headers())
        headers.pop("Content-Type", None)
        headers["Accept"] = "image/*,*/*;q=0.8"
        async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            body = await resp.read()
            if resp.status >= 400:
                snippet = body[:120].decode("utf-8", errors="replace")
                raise RuntimeError(f"GET media failed HTTP {resp.status}: {snippet}")
            size_header = str(resp.headers.get("Content-Length") or "").strip()
            if size_header and int(size_header) > _MAX_INBOUND_MEDIA_BYTES:
                raise RuntimeError(f"media file too large: {size_header} bytes")
            if len(body) > _MAX_INBOUND_MEDIA_BYTES:
                raise RuntimeError(f"media file too large: {len(body)} bytes")
            response_type = str(resp.headers.get("Content-Type") or content_type or "").split(";", 1)[0].strip().lower()
            if not response_type.startswith(_IMAGE_MIME_PREFIX):
                raise RuntimeError(f"unsupported inbound media type: {response_type or 'unknown'}")
            ext = mimetypes.guess_extension(response_type) or Path(filename).suffix or ".jpg"
            if ext == ".jpe":
                ext = ".jpg"
            return cache_image_from_bytes(body, ext), response_type

    def _e2e_status_text(self, room: _RoomInfo) -> str:
        helper_ready = bool(self._e2e)
        have_key = bool(helper_ready and getattr(self._e2e, "have_room", lambda _rid: False)(room.rid))
        armed_until = self._e2e_armed_until.get(room.rid, 0)
        armed = armed_until > time.time()
        return "\n".join([
            "E2E status:",
            f"- helper: {'ready' if helper_ready else 'not initialized'}",
            f"- room type: {room.chat_type}",
            f"- room encrypted: {bool(room.encrypted)}",
            f"- room key id: {'present' if room.e2e_key_id else 'missing'}",
            f"- my room key: {'present' if have_key else 'missing'}",
            f"- suggested key: {'present' if room.e2e_suggested_key else 'missing'}",
            f"- one-shot exchange: {'armed' if armed else 'not armed'}",
            f"- persistent mode: {'on' if room.rid in self._e2e_persistent_rooms else 'off'}",
        ])

    async def _send_e2e_control_text(self, room: _RoomInfo, content: str) -> SendResult:
        try:
            data = await self._send_e2e_chunk(room, content)
            msg = data.get("message") or {}
            return SendResult(success=True, message_id=str(msg.get("_id") or data.get("_id") or "") or None)
        except Exception as exc:
            logger.error("Rocket.Chat: encrypted control send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _disable_e2e_mode(self, room: _RoomInfo) -> None:
        self._e2e_armed_until.pop(room.rid, None)
        self._e2e_disable_after_reply.discard(room.rid)
        self._e2e_persistent_rooms.discard(room.rid)
        await self._set_room_encrypted(room, False)

    @staticmethod
    def _parse_e2e_control(text: str) -> str:
        """Return one of: once, on, off, status, usage, or empty string."""
        raw = (text or "").strip().lower()
        if not raw:
            return ""
        # Deliberate non-slash controls avoid Rocket.Chat's slash-command parser
        # and avoid accidental triggers from ordinary uses of the phrase "e2e".
        non_slash = {
            "e2e1": "once",
            "e2e_on": "on",
            "e2e_off": "off",
            "e2e_status": "status",
        }
        if raw in non_slash:
            return non_slash[raw]
        # Backwards-compatible aliases. We keep accepting them, but docs should
        # advertise the non-slash forms because Rocket.Chat can emit "unknown
        # command"/"not allowed" UI messages for slash commands.
        if raw == "/e2e":
            return "once"
        if raw.startswith("/e2e "):
            subcmd = raw.split(maxsplit=1)[1].strip()
            if subcmd in {"status", "state"}:
                return "status"
            if subcmd in {"cancel", "off", "stop"}:
                return "off"
            if subcmd in {"on", "persist", "persistent", "stay", "keep"}:
                return "on"
            if subcmd in {"", "next", "arm", "once", "one-shot", "oneshot"}:
                return "once"
            return "usage"
        return ""

    async def _handle_encrypted_e2e_control(self, room: _RoomInfo, text: str) -> bool:
        command = self._parse_e2e_control(text)
        if command not in {"off", "status"}:
            return False
        if command == "status":
            await self._send_e2e_control_text(room, self._e2e_status_text(room))
            return True
        await self._send_e2e_control_text(room, "Turning E2E persistent mode off; normal DM mode restored.")
        try:
            await self._disable_e2e_mode(room)
        except Exception as exc:
            logger.warning("Rocket.Chat: failed to disable persistent E2E room %s: %s", room.rid, exc)
        return True

    async def _handle_e2e_command(self, room: _RoomInfo, text: str) -> bool:
        command = self._parse_e2e_control(text)
        if not command:
            return False
        if command == "status":
            await self._send_plain_text(room.rid, self._e2e_status_text(room))
            return True
        if command == "off":
            try:
                await self._disable_e2e_mode(room)
            except Exception as exc:
                logger.debug("Rocket.Chat: E2E cancel could not disable room %s: %s", room.rid, exc)
            await self._send_plain_text(room.rid, "E2E mode cancelled; normal DM mode restored.")
            return True
        if command == "on":
            ok, message = await self._ensure_e2e_exchange_ready(room, wait_for_key=False)
            if ok:
                self._arm_e2e_room(room, persistent=True)
                message = self._e2e_ready_message(persistent=True)
            else:
                self._schedule_e2e_ready_watch(room, persistent=True)
                message = "E2E key sharing has been requested, but the room key is not available yet. I will keep checking and send a ready message here when it arrives; you should not need to send `e2e_on` again."
            await self._send_plain_text(room.rid, message)
            return True
        if command == "usage":
            await self._send_plain_text(room.rid, "Usage: `e2e1` for one encrypted reply, `e2e_on` for persistent mode, `e2e_status`, or `e2e_off`. Slash aliases are accepted but may trigger Rocket.Chat parser warnings.")
            return True
        ok, message = await self._ensure_e2e_exchange_ready(room, wait_for_key=False)
        if ok:
            self._arm_e2e_room(room, persistent=False)
        else:
            self._schedule_e2e_ready_watch(room, persistent=False)
            message = "E2E key sharing has been requested, but the room key is not available yet. I will keep checking and send a ready message here when it arrives; you should not need to send `e2e1` again."
        await self._send_plain_text(room.rid, message)
        return True

    async def _handle_rc_message(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        msg_id = str(msg.get("_id") or "")
        rid = str(msg.get("rid") or "")
        if not msg_id or not rid:
            return
        if self._dedup.is_duplicate(msg_id) or self._remember_persistent_seen_message(msg_id):
            return
        room = self._rooms.get(rid, _RoomInfo(rid=rid))
        msg_type = msg.get("t")
        if msg_type in {"room_e2e_enabled", "room_e2e_disabled"}:
            # Rocket.Chat can toggle E2E on the same DM room briefly. Refresh
            # immediately instead of waiting for the periodic subscription poll,
            # otherwise we can miss a short-lived suggested room key window.
            room.encrypted = msg_type == "room_e2e_enabled"
            self._rooms[rid] = room
            if self.e2e_enabled:
                asyncio.create_task(self._refresh_subscriptions())
            return
        if msg_type == "e2e":
            if not room.encrypted:
                room.encrypted = True
                self._rooms[rid] = room
                try:
                    await self._refresh_subscriptions()
                    # subscriptions.get can lag or report encrypted=false even
                    # after a room_e2e_enabled/control send. rooms.info is the
                    # authoritative room setting needed before importing E2EKey.
                    room = await self._refresh_room_info(self._rooms.get(rid, room))
                except Exception as exc:
                    logger.debug("Rocket.Chat: E2E subscription refresh failed for %s: %s", rid, exc)
            decrypted = await self._decrypt_e2e_message(msg, room)
            if not decrypted:
                return
            msg = decrypted
        elif msg_type:
            return  # system/control message
        user = msg.get("u") or {}
        user_id = str(user.get("_id") or "")
        username = str(user.get("username") or user_id or "")
        if user_id == self.user_id or (self._bot_username and username.lower() == self._bot_username.lower()):
            return
        ts = _date_to_epoch(msg.get("ts"))
        if ts and time.time() - ts > _STALE_MESSAGE_AGE_SEC:
            logger.debug("Rocket.Chat: dropped stale message %s", msg_id)
            return

        room = self._rooms.get(rid, room)
        text = str(msg.get("msg") or "")
        if msg_type == "e2e" and await self._handle_encrypted_e2e_control(room, text):
            return
        if not msg.get("__hermes_backfill") and await self._handle_e2e_command(room, text):
            return
        if msg_type == "e2e" and self._e2e_armed_until.get(rid, 0) > time.time() and rid not in self._e2e_persistent_rooms:
            self._e2e_disable_after_reply.add(rid)
        if not self._should_process_room_message(rid, msg, text, room):
            return
        clean_text = _strip_bot_mention(text, self._bot_username) if room.chat_type != "dm" else text
        media_urls, media_types = await self._extract_inbound_media(msg)
        if not clean_text.strip() and not media_urls and not msg.get("file") and not msg.get("attachments"):
            return

        if self.ack_reaction:
            asyncio.create_task(self._react(msg_id, str(self.ack_reaction)))
        if self.mark_as_read:
            asyncio.create_task(self._mark_read(rid))

        tmid = str(msg.get("tmid") or "") or None
        source = self.build_source(
            chat_id=rid,
            chat_name=room.display_name,
            chat_type="thread" if tmid else room.chat_type,
            user_id=username,          # human-friendly allowlist target
            user_name=str(user.get("name") or username),
            user_id_alt=user_id,
            thread_id=tmid,
            parent_chat_id=rid if tmid else None,
            message_id=msg_id,
        )
        message_type = MessageType.PHOTO if any(mtype.startswith(_IMAGE_MIME_PREFIX) for mtype in media_types) else MessageType.TEXT
        event = MessageEvent(
            text=clean_text,
            message_type=message_type,
            source=source,
            raw_message=msg,
            message_id=msg_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=tmid,
            timestamp=datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc),
        )
        await self.handle_message(event)

    async def _react(self, message_id: str, emoji: str) -> None:
        normalized = emoji if emoji.startswith(":") else f":{emoji.strip(':')}:"
        try:
            await self._api_post("/api/v1/chat.react", {"messageId": message_id, "emoji": normalized, "shouldReact": True})
        except Exception:
            logger.debug("Rocket.Chat: reaction failed", exc_info=True)

    async def _mark_read(self, rid: str) -> None:
        try:
            await self._api_post("/api/v1/subscriptions.read", {"rid": rid})
        except Exception:
            logger.debug("Rocket.Chat: mark-as-read failed", exc_info=True)


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        import websockets  # noqa: F401
        return True
    except ImportError:
        logger.warning("Rocket.Chat: aiohttp and websockets are required")
        return False


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(
        _normalize_url(extra.get("url") or extra.get("base_url") or os.getenv("ROCKETCHAT_URL", ""))
        and (extra.get("user_id") or os.getenv("ROCKETCHAT_USER_ID", ""))
        and (getattr(config, "token", None) or extra.get("auth_token") or os.getenv("ROCKETCHAT_AUTH_TOKEN", ""))
    )


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


def register(ctx) -> None:
    ctx.register_platform(
        name="rocketchat",
        label="Rocket.Chat",
        adapter_factory=lambda cfg: RocketChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ROCKETCHAT_URL", "ROCKETCHAT_USER_ID", "ROCKETCHAT_AUTH_TOKEN"],
        install_hint="Uses Hermes' aiohttp + websockets dependencies; no extra packages normally needed.",
        allowed_users_env="ROCKETCHAT_ALLOWED_USERS",
        allow_all_env="ROCKETCHAT_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🚀",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Rocket.Chat. Rocket.Chat supports standard Markdown, "
            "channels, DMs, groups, and threads. Prefer concise replies in channels; "
            "long or multi-part replies may be threaded by the adapter."
        ),
    )
