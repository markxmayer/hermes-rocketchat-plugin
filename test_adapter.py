import asyncio
import base64
import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("rocketchat_adapter_test", ROOT / "adapter.py")
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


def test_timestamp_parsing_and_ws_url():
    assert int(adapter._date_to_epoch({"$date": 1688434691876})) == 1688434691
    assert adapter._websocket_url("https://chat.example.com") == "wss://chat.example.com/websocket"
    assert adapter._websocket_url("http://localhost:3000") == "ws://localhost:3000/websocket"


def test_strip_bot_mention():
    assert adapter._strip_bot_mention("@hermes: hello", "hermes") == "hello"
    assert adapter._strip_bot_mention("hey @Hermes please help", "hermes") == "hey please help"


def test_format_message_removes_empty_markdown_image_artifact():
    rc = _bare_adapter()
    assert rc.format_message("Here you go:\n\n![Success — Hermes Rules]()") == "Here you go:"
    assert rc.format_message("Before ![chart](/tmp/chart.png) after") == "Before /tmp/chart.png after"


def test_register_platform_context():
    calls = []
    class Ctx:
        def register_platform(self, **kwargs):
            calls.append(kwargs)
    adapter.register(Ctx())
    assert calls
    call = calls[0]
    assert call["name"] == "rocketchat"
    assert call["allowed_users_env"] == "ROCKETCHAT_ALLOWED_USERS"
    assert call["allow_all_env"] == "ROCKETCHAT_ALLOW_ALL_USERS"
    assert call["max_message_length"] == adapter.MAX_MESSAGE_LENGTH


def test_ddp_login_result_does_not_deadlock():
    class FakeAdapter:
        auth_token = "tok"
        base_url = "https://chat.example.com"
    ddp = adapter._DDPClient(FakeAdapter())
    sent = []
    class FakeWS:
        async def send(self, payload):
            sent.append(payload)
    ddp.ws = FakeWS()

    async def run():
        await ddp._handle_raw('{"msg":"connected","session":"abc"}')
        assert sent
        login_frame = adapter.json.loads(sent[0])
        assert login_frame["method"] == "login"
        login_id = login_frame["id"]
        await ddp._handle_raw(adapter.json.dumps({"msg": "result", "id": login_id, "result": {"id": "u"}}))
        assert ddp._connected.is_set()
    asyncio.run(run())


def test_ddp_subscribe_uses_last_seen_room_timestamp():
    rc = _bare_adapter()
    rc._last_seen_message_ms["ROOM1"] = 1688434691876
    ddp = adapter._DDPClient(rc)
    sent = []

    class FakeWS:
        async def send(self, payload):
            sent.append(payload)

    ddp.ws = FakeWS()
    ddp._connected.set()

    asyncio.run(ddp.subscribe_room("ROOM1"))

    frame = adapter.json.loads(sent[0])
    assert frame["name"] == "stream-room-messages"
    assert frame["params"][0] == "ROOM1"
    assert frame["params"][1]["args"][0]["lastUpdate"]["$date"] == 1688434691876


def test_ddp_login_runs_reconnect_backfill_after_resubscribe():
    class FakeAdapter:
        auth_token = "tok"
        base_url = "https://chat.example.com"
        backfill_on_connect = True
        backfill_window_seconds = 123

        def __init__(self):
            self.backfills = []

        async def _backfill_recent_messages(self, *, window_seconds=300):
            self.backfills.append(window_seconds)

        def _subscription_last_update_ms(self, rid):
            return 42

    fake_adapter = FakeAdapter()
    ddp = adapter._DDPClient(fake_adapter)
    sent = []

    class FakeWS:
        async def send(self, payload):
            sent.append(payload)

    ddp.ws = FakeWS()
    ddp._desired_rooms.add("ROOM1")

    async def run():
        await ddp._handle_raw('{"msg":"connected","session":"abc"}')
        login_id = adapter.json.loads(sent[0])["id"]
        await ddp._handle_raw(adapter.json.dumps({"msg": "result", "id": login_id, "result": {"id": "u"}}))

    asyncio.run(run())

    assert fake_adapter.backfills == [123]
    sub_frame = adapter.json.loads(sent[1])
    assert sub_frame["params"][1]["args"][0]["lastUpdate"]["$date"] == 42


def _bare_adapter():
    rc = object.__new__(adapter.RocketChatAdapter)
    rc.base_url = "https://chat.example.com"
    rc.user_id = "bot-user-id"
    rc.auth_token = "secret-token"
    rc._bot_username = "hermes"
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="general", t="d")}
    rc._dedup = adapter.MessageDeduplicator(max_size=500, ttl_seconds=6 * 60 * 60)
    rc._last_seen_message_ms = {}
    rc.backfill_on_connect = True
    rc.backfill_window_seconds = 300
    rc.ack_reaction = ""
    rc.mark_as_read = False
    rc.require_mention = False
    rc.rooms_config = {}
    rc.e2e_enabled = False
    rc.e2e_dm_only = True
    rc.e2e_auto_create_dm_key = False
    rc.e2e_allow_room_key_rotation = False
    rc.e2e_queue_self_for_keys = False
    rc._e2e = None
    rc._e2e_armed_until = {}
    rc._e2e_disable_after_reply = set()
    rc._e2e_persistent_rooms = set()
    rc._e2e_pending_ready_tasks = {}
    rc.e2e_key_wait_attempts = 24
    rc.e2e_key_wait_delay = 1.25
    rc.e2e_background_wait_attempts = 48
    rc.e2e_background_wait_delay = 2.5
    return rc


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


def test_extract_inbound_image_prefers_original_file_link(monkeypatch):
    rc = _bare_adapter()
    downloads = []

    async def fake_download(url, filename, content_type, **kwargs):
        downloads.append((url, filename, content_type, kwargs.get("encryption")))
        return "/tmp/hermes-image.png", "image/png"

    monkeypatch.setattr(rc, "_download_inbound_media", fake_download)
    msg = {
        "file": {"_id": "file123", "name": "screen.png", "type": "image/png"},
        "attachments": [
            {
                "title": "screen.png",
                "title_link": "/file-upload/file123/screen.png",
                "image_url": "/file-upload/thumb123/screen.png",
                "image_type": "image/png",
            }
        ],
    }

    paths, types = asyncio.run(rc._extract_inbound_media(msg))

    assert paths == ["/tmp/hermes-image.png"]
    assert types == ["image/png"]
    assert downloads == [("https://chat.example.com/file-upload/file123/screen.png", "screen.png", "image/png", None)]


def test_extract_inbound_image_uses_decrypted_attachment_encryption(monkeypatch):
    rc = _bare_adapter()
    downloads = []

    async def fake_download(url, filename, content_type, **kwargs):
        downloads.append((url, filename, content_type, kwargs.get("encryption")))
        return "/tmp/decrypted-image.png", "image/png"

    monkeypatch.setattr(rc, "_download_inbound_media", fake_download)
    msg = {
        "file": {"_id": "file123", "name": "encrypted-name", "type": "application/octet-stream"},
        "files": [{"_id": "file123", "name": "encrypted-name", "type": "application/octet-stream"}],
        "attachments": [
            {
                "title": "screen.png",
                "title_link": "/file-upload/file123/encrypted-name",
                "image_url": "/file-upload/file123/encrypted-name",
                "image_type": "image/png",
                "encryption": {"key": {"kty": "oct", "k": "abc"}, "iv": "def"},
                "hashes": {"sha256": "00" * 32},
            }
        ],
    }

    paths, types = asyncio.run(rc._extract_inbound_media(msg))

    assert paths == ["/tmp/decrypted-image.png"]
    assert types == ["image/png"]
    assert downloads == [
        (
            "https://chat.example.com/file-upload/file123/encrypted-name",
            "screen.png",
            "image/png",
            {"key": {"kty": "oct", "k": "abc"}, "iv": "def", "type": "image/png", "hash": "00" * 32},
        )
    ]


def test_sniff_image_mime_detects_common_images():
    assert adapter._sniff_image_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert adapter._sniff_image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert adapter._sniff_image_mime(b"GIF89arest") == "image/gif"
    assert adapter._sniff_image_mime(b"RIFFxxxxWEBPrest") == "image/webp"
    assert adapter._sniff_image_mime(b"not an image") == ""


def test_download_inbound_media_accepts_e2e_octet_stream_with_image_magic(monkeypatch):
    rc = _bare_adapter()
    png_body = b"\x89PNG\r\n\x1a\nfake png payload"
    cached = []

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/octet-stream"}
        content = _FakeContent([png_body])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse()

    def fake_cache_image_from_bytes(body, ext):
        cached.append((body, ext))
        return "/tmp/cached-e2e.png"

    rc._session = FakeSession()
    monkeypatch.setattr(adapter, "cache_image_from_bytes", fake_cache_image_from_bytes)

    path, mime = asyncio.run(rc._download_inbound_media(
        "https://chat.example.com/file-upload/file123/photo.png",
        "photo.png",
        "image/png",
    ))

    assert path == "/tmp/cached-e2e.png"
    assert mime == "image/png"
    assert cached == [(png_body, ".png")]


def test_download_inbound_media_decrypts_e2e_file_before_sniffing(monkeypatch):
    rc = _bare_adapter()
    encrypted_body = b"encrypted bytes"
    plaintext = b"\x89PNG\r\n\x1a\ndecrypted png"
    cached = []

    class FakeE2E:
        def decrypt_file_bytes(self, encryption, body):
            assert encryption == {"key": "metadata"}
            assert body == encrypted_body
            return plaintext, "image/png"

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/octet-stream"}
        content = _FakeContent([encrypted_body])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse()

    def fake_cache_image_from_bytes(body, ext):
        cached.append((body, ext))
        return "/tmp/cached-decrypted-e2e.png"

    rc._session = FakeSession()
    rc._e2e = FakeE2E()
    monkeypatch.setattr(adapter, "cache_image_from_bytes", fake_cache_image_from_bytes)

    path, mime = asyncio.run(rc._download_inbound_media(
        "https://chat.example.com/file-upload/file123/photo.png",
        "photo.png",
        "image/png",
        encryption={"key": "metadata"},
    ))

    assert path == "/tmp/cached-decrypted-e2e.png"
    assert mime == "image/png"
    assert cached == [(plaintext, ".png")]


def test_handle_rc_message_emits_photo_event_with_cached_image(monkeypatch):
    rc = _bare_adapter()
    events = []

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    async def fake_download(url, filename, content_type, **kwargs):
        return "/tmp/cached-rocketchat-image.jpg", "image/jpeg"

    monkeypatch.setattr(rc, "build_source", fake_build_source)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)
    monkeypatch.setattr(rc, "_download_inbound_media", fake_download)

    msg = {
        "_id": "msg1",
        "rid": "ROOM1",
        "msg": "what is in this image?",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
        "file": {"_id": "file123", "name": "photo.jpg", "type": "image/jpeg"},
        "attachments": [{"title_link": "/file-upload/file123/photo.jpg", "image_type": "image/jpeg"}],
    }

    asyncio.run(rc._handle_rc_message(msg))

    assert len(events) == 1
    event = events[0]
    assert event.message_type == adapter.MessageType.PHOTO
    assert event.text == "what is in this image?"
    assert event.media_urls == ["/tmp/cached-rocketchat-image.jpg"]
    assert event.media_types == ["image/jpeg"]


def test_send_image_file_uses_native_rooms_media_upload(monkeypatch, tmp_path):
    rc = _bare_adapter()
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"fake image bytes")
    uploads = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        return adapter.SendResult(success=True, message_id="fallback-text")

    async def fake_upload(chat_id, file_path, *, caption=None, file_name=None, reply_to=None, metadata=None):
        uploads.append((chat_id, file_path, caption, file_name, reply_to, metadata))
        return adapter.SendResult(success=True, message_id="uploaded-msg")

    monkeypatch.setattr(rc, "send", fake_send)
    monkeypatch.setattr(rc, "_upload_and_confirm_media", fake_upload, raising=False)

    result = asyncio.run(rc.send_image_file("ROOM1", str(image_path), caption="Here is the chart", metadata={"thread_id": "thread1"}))

    assert result.success is True
    assert result.message_id == "uploaded-msg"
    assert uploads == [("ROOM1", str(image_path), "Here is the chart", None, None, {"thread_id": "thread1"})]


def test_send_document_uses_native_rooms_media_upload(monkeypatch, tmp_path):
    rc = _bare_adapter()
    doc_path = tmp_path / "report.pdf"
    doc_path.write_bytes(b"%PDF fake")
    uploads = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        return adapter.SendResult(success=True, message_id="fallback-text")

    async def fake_upload(chat_id, file_path, *, caption=None, file_name=None, reply_to=None, metadata=None):
        uploads.append((chat_id, file_path, caption, file_name, reply_to, metadata))
        return adapter.SendResult(success=True, message_id="uploaded-doc")

    monkeypatch.setattr(rc, "send", fake_send)
    monkeypatch.setattr(rc, "_upload_and_confirm_media", fake_upload, raising=False)

    result = asyncio.run(rc.send_document("ROOM1", str(doc_path), caption="PDF report", file_name="report.pdf", reply_to="root-msg"))

    assert result.success is True
    assert result.message_id == "uploaded-doc"
    assert uploads == [("ROOM1", str(doc_path), "PDF report", "report.pdf", "root-msg", None)]


def test_upload_and_confirm_media_sends_file_then_confirm_with_thread(monkeypatch):
    rc = _bare_adapter()
    calls = []

    async def fake_api_upload_media(rid, file_path, *, file_name=None, content_type=None):
        calls.append(("upload", rid, file_path, file_name, content_type))
        return {"file": {"_id": "file123", "url": "/file-upload/file123/report.pdf"}, "success": True}

    async def fake_api_post(path, payload):
        calls.append(("post", path, payload))
        return {"message": {"_id": "msg123"}, "success": True}

    monkeypatch.setattr(rc, "_api_upload_media", fake_api_upload_media)
    monkeypatch.setattr(rc, "_api_post", fake_api_post)

    result = asyncio.run(rc._upload_and_confirm_media(
        "ROOM 1",
        "/tmp/report.pdf",
        caption="Report attached",
        file_name="report.pdf",
        metadata={"thread_id": "thread42"},
    ))

    assert result.success is True
    assert result.message_id == "msg123"
    assert calls == [
        ("upload", "ROOM 1", "/tmp/report.pdf", "report.pdf", None),
        ("post", "/api/v1/rooms.mediaConfirm/ROOM%201/file123", {"msg": "Report attached", "tmid": "thread42"}),
    ]


def test_delete_message_calls_rocketchat_delete(monkeypatch):
    rc = _bare_adapter()
    calls = []

    async def fake_api_post(path, payload):
        calls.append((path, payload))
        return {"success": True}

    monkeypatch.setattr(rc, "_api_post", fake_api_post)

    assert asyncio.run(rc.delete_message("ROOM1", "msg123")) is True
    assert calls == [("/api/v1/chat.delete", {"roomId": "ROOM1", "msgId": "msg123", "asUser": False})]


def test_send_remote_image_downloads_then_uploads(monkeypatch):
    rc = _bare_adapter()
    calls = []

    async def fake_download(url, *, suffix=""):
        calls.append(("download", url, suffix))
        return "/tmp/remote-image.png", "remote-image.png"

    async def fake_upload(chat_id, file_path, *, caption=None, file_name=None, reply_to=None, metadata=None):
        calls.append(("upload", chat_id, file_path, caption, file_name, reply_to, metadata))
        return adapter.SendResult(success=True, message_id="remote-uploaded")

    monkeypatch.setattr(rc, "_download_remote_media_to_temp", fake_download, raising=False)
    monkeypatch.setattr(rc, "_upload_and_confirm_media", fake_upload)

    result = asyncio.run(rc.send_image("ROOM1", "https://cdn.example.com/image.png", caption="remote", metadata={"thread_id": "t1"}))

    assert result.success is True
    assert result.message_id == "remote-uploaded"
    assert calls == [
        ("download", "https://cdn.example.com/image.png", ".png"),
        ("upload", "ROOM1", "/tmp/remote-image.png", "remote", "remote-image.png", None, {"thread_id": "t1"}),
    ]


def test_backfill_recent_messages_uses_room_history_and_chronological_order(monkeypatch):
    rc = _bare_adapter()
    rc._rooms = {
        "CHAN": adapter._RoomInfo(rid="CHAN", name="general", t="c"),
        "GROUP": adapter._RoomInfo(rid="GROUP", name="secret", t="p"),
        "DM": adapter._RoomInfo(rid="DM", name="mark", t="d"),
    }
    api_calls = []
    handled = []

    async def fake_api_get(path, *, params=None):
        api_calls.append((path, params))
        return {"messages": [
            {"_id": f"{path}-new", "rid": params["roomId"], "msg": "new", "ts": {"$date": 2000}},
            {"_id": f"{path}-old", "rid": params["roomId"], "msg": "old", "ts": {"$date": 1000}},
        ], "success": True}

    async def fake_handle(msg):
        handled.append(msg["_id"])

    monkeypatch.setattr(rc, "_api_get", fake_api_get)
    monkeypatch.setattr(rc, "_handle_rc_message", fake_handle)

    asyncio.run(rc._backfill_recent_messages(window_seconds=300))

    assert [call[0] for call in api_calls] == ["/api/v1/channels.history", "/api/v1/groups.history", "/api/v1/im.history"]
    assert handled == [
        "/api/v1/channels.history-old", "/api/v1/channels.history-new",
        "/api/v1/groups.history-old", "/api/v1/groups.history-new",
        "/api/v1/im.history-old", "/api/v1/im.history-new",
    ]


def test_handle_rc_message_records_last_seen_room_timestamp(monkeypatch):
    rc = _bare_adapter()
    events = []

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    async def fake_extract(msg):
        return [], []

    monkeypatch.setattr(rc, "build_source", fake_build_source)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)
    monkeypatch.setattr(rc, "_extract_inbound_media", fake_extract)

    seen_ts = int(time.time() * 1000)
    msg = {
        "_id": "seen1",
        "rid": "ROOM1",
        "msg": "hello",
        "ts": {"$date": seen_ts},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }

    asyncio.run(rc._handle_rc_message(msg))

    assert len(events) == 1
    assert rc._last_seen_message_ms["ROOM1"] == seen_ts


def test_limited_response_reader_accepts_small_chunked_body():
    body = asyncio.run(adapter._read_response_body_limited(
        SimpleNamespace(headers={}, content=_FakeContent([b"ab", b"cd"])),
        max_bytes=4,
        label="media",
    ))

    assert body == b"abcd"


def test_limited_response_reader_rejects_oversized_stream_without_content_length():
    try:
        asyncio.run(adapter._read_response_body_limited(
            SimpleNamespace(headers={}, content=_FakeContent([b"abc", b"de"])),
            max_bytes=4,
            label="media",
        ))
    except RuntimeError as exc:
        assert "media file too large" in str(exc)
    else:
        raise AssertionError("expected oversized stream to fail")


def test_limited_response_reader_rejects_oversized_content_length_before_reading():
    try:
        asyncio.run(adapter._read_response_body_limited(
            SimpleNamespace(headers={"Content-Length": "5"}, content=_FakeContent([b"abc"])),
            max_bytes=4,
            label="remote media",
        ))
    except RuntimeError as exc:
        assert "remote media file too large: 5 bytes" in str(exc)
    else:
        raise AssertionError("expected oversized Content-Length to fail")


def test_rocket_path_segment_quotes_path_separators_and_spaces():
    assert adapter._rocket_path_segment("ROOM 1/child") == "ROOM%201%2Fchild"
    assert adapter._rocket_path_segment("file 123") == "file%20123"


def test_persistent_seen_messages_survive_adapter_restart(tmp_path):
    state_path = tmp_path / "seen.json"
    first = _bare_adapter()
    first._persistent_seen_path = state_path
    first._persistent_seen_ids = set()
    first._persistent_seen_order = []

    assert first._remember_persistent_seen_message("msg-1") is False
    assert first._remember_persistent_seen_message("msg-1") is True

    second = _bare_adapter()
    second._persistent_seen_path = state_path
    second._load_persistent_seen_messages()

    assert second._remember_persistent_seen_message("msg-1") is True
    assert second._remember_persistent_seen_message("msg-2") is False


def test_handle_rc_message_skips_restart_persistent_duplicate(monkeypatch, tmp_path):
    rc = _bare_adapter()
    rc._persistent_seen_path = tmp_path / "seen.json"
    rc._persistent_seen_ids = {"msg1"}
    rc._persistent_seen_order = ["msg1"]
    events = []

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "msg1",
        "rid": "ROOM1",
        "msg": "test",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }

    asyncio.run(rc._handle_rc_message(msg))

    assert events == []


def test_handle_e2e_command_reports_missing_key_without_scheduling_watch(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._e2e = object()
    room = adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True, e2e_key_id="key1")
    rc._rooms = {"ROOM1": room}
    sent = []
    scheduled = []

    async def fake_ensure(room_arg, *, wait_for_key=True):
        assert wait_for_key is False
        return False, "missing key"

    async def fake_send_plain(rid, content):
        sent.append((rid, content))
        return adapter.SendResult(success=True, message_id="sent")

    def fake_schedule(room_arg, *, persistent):
        scheduled.append((room_arg.rid, persistent))

    monkeypatch.setattr(rc, "_ensure_e2e_exchange_ready", fake_ensure)
    monkeypatch.setattr(rc, "_send_plain_text", fake_send_plain)
    monkeypatch.setattr(rc, "_schedule_e2e_ready_watch", fake_schedule)

    handled = asyncio.run(rc._handle_e2e_command(room, "e2e1"))

    assert handled is True
    assert scheduled == []
    assert sent == [("ROOM1", "missing key")]


def test_plain_e2e_word_does_not_trigger_control_command(monkeypatch):
    rc = _bare_adapter()
    sent = []

    async def fake_send_plain(rid, content):
        sent.append((rid, content))
        return adapter.SendResult(success=True)

    monkeypatch.setattr(rc, "_send_plain_text", fake_send_plain)

    assert asyncio.run(rc._handle_e2e_command(rc._rooms["ROOM1"], "e2e")) is False
    assert asyncio.run(rc._handle_e2e_command(rc._rooms["ROOM1"], "let's talk about e2e")) is False
    assert sent == []


def test_delayed_e2e_ready_watch_arms_and_announces(monkeypatch):
    rc = _bare_adapter()
    room = adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True, e2e_key_id="key1")
    rc._rooms = {"ROOM1": room}
    sent = []

    async def fake_wait(room_arg, *, attempts=None, delay=None):
        assert attempts == rc.e2e_background_wait_attempts
        assert delay == rc.e2e_background_wait_delay
        return True

    async def fake_send_plain(rid, content):
        sent.append((rid, content))
        return adapter.SendResult(success=True, message_id="sent")

    monkeypatch.setattr(rc, "_wait_for_e2e_room_key", fake_wait)
    monkeypatch.setattr(rc, "_send_plain_text", fake_send_plain)
    rc._e2e_pending_ready_tasks["ROOM1"] = SimpleNamespace(done=lambda: False)

    asyncio.run(rc._e2e_ready_watch_loop(room, persistent=False))

    assert sent == [("ROOM1", rc._e2e_ready_message(persistent=False))]
    assert rc._e2e_armed_until["ROOM1"] > time.time()
    assert "ROOM1" not in rc._e2e_pending_ready_tasks


def test_send_clarify_and_slash_confirm_use_text_fallback(monkeypatch):
    rc = _bare_adapter()
    sent = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        sent.append((chat_id, content, metadata))
        return adapter.SendResult(success=True, message_id="sent")

    monkeypatch.setattr(rc, "send", fake_send)

    clarify = asyncio.run(rc.send_clarify("ROOM1", "Pick one", ["A", "B"], "clarify1", "session1", metadata={"thread_id": "t"}))
    confirm = asyncio.run(rc.send_slash_confirm("ROOM1", "Reload MCP?", "This reloads tools", "session1", "confirm1"))

    assert clarify.success is True and confirm.success is True
    assert "Pick one" in sent[0][1] and "1. A" in sent[0][1] and "2. B" in sent[0][1]
    assert "Reload MCP?" in sent[1][1] and "/approve" in sent[1][1] and "/cancel" in sent[1][1]



def test_e2e_helper_encrypts_and_decrypts_dm_message_roundtrip():
    e2e = adapter._load_e2e_module()
    public_key, private_key = e2e.generate_rsa_jwks()
    wrapped = e2e.encode_private_key(private_key, "test password", "bot-user-id")
    decoded = e2e.decode_private_key(wrapped, "test password", "bot-user-id")
    assert adapter.json.loads(decoded)["kty"] == "RSA"

    private = e2e.private_key_from_jwk(adapter.json.loads(decoded))
    kid, session_key_json, session_key = e2e.generate_session_key()
    group_key = e2e.encrypt_session_key_for_public(session_key_json, kid, public_key)
    parsed_kid, parsed_session_json, parsed_session_key = e2e.decrypt_session_key(group_key, private)
    assert parsed_kid == kid
    assert parsed_session_json == session_key_json
    assert parsed_session_key == session_key

    content = e2e.encrypt_message_content("secret hello", session_key, kid)
    assert content["algorithm"] == "rc.v2.aes-sha2"
    assert e2e.decrypt_message_content(content, session_key)["msg"] == "secret hello"


def test_e2e_helper_decrypts_file_metadata_and_aes_ctr_bytes():
    e2e = adapter._load_e2e_module()
    kid, session_key_json, session_key = e2e.generate_session_key()
    plaintext = b"\x89PNG\r\n\x1a\nplain image bytes"
    file_key = os.urandom(32)
    iv = os.urandom(16)
    encryptor = e2e.Cipher(e2e.algorithms.AES(file_key), e2e.modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    encryption = {
        "key": {
            "kty": "oct",
            "k": base64.urlsafe_b64encode(file_key).decode("ascii").rstrip("="),
            "ext": True,
            "key_ops": ["encrypt", "decrypt"],
        },
        "iv": base64.b64encode(iv).decode("ascii"),
        "type": "image/png",
        "hash": hashlib.sha256(plaintext).hexdigest(),
    }
    encrypted_meta = e2e.encrypt_message_content("", session_key, kid, attachments=[encryption])["ciphertext"]
    # Re-wrap the ciphertext into a normal encrypted content object carrying the
    # metadata dict directly; this mirrors Rocket.Chat's file.content payload.
    metadata_content = e2e.encrypt_message_content("", session_key, kid, attachments=[encryption])
    decrypted_meta = e2e.decrypt_message_content(metadata_content, session_key)["attachments"][0]

    decrypted, mime = e2e.decrypt_file_bytes(decrypted_meta, ciphertext)

    assert encrypted_meta
    assert decrypted == plaintext
    assert mime == "image/png"


def test_e2e_decrypt_message_exposes_decrypted_file_encryption_metadata():
    e2e = adapter._load_e2e_module()
    kid, session_key_json, session_key = e2e.generate_session_key()
    helper = e2e.RocketChatE2E(user_id="bot-user-id", password="pw", rest_get=None, rest_post=None)
    helper.rooms["ROOM1"] = e2e.RoomE2EState("ROOM1", kid, session_key_json, session_key)
    metadata = {
        "key": {"kty": "oct", "k": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")},
        "iv": base64.b64encode(os.urandom(16)).decode("ascii"),
        "type": "image/png",
        "hash": hashlib.sha256(b"image").hexdigest(),
    }
    encrypted_file_metadata = e2e.encrypt_message_content("", session_key, kid, attachments=[metadata])
    content = e2e.encrypt_message_content("Do you see this image?", session_key, kid)
    encrypted_msg = {
        "_id": "msg1",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {**content, "ciphertext": e2e.encrypt_message_content("", session_key, kid, attachments=[])["ciphertext"]},
    }
    encrypted_msg["content"] = e2e.encrypt_message_content(
        "Do you see this image?",
        session_key,
        kid,
        attachments=[{"title_link": "/file-upload/file123/photo.png"}],
    )
    # Rocket.Chat includes file metadata inside the encrypted message content for
    # attachment messages.  Build that payload explicitly so the helper must
    # decrypt the nested file.content metadata too.
    payload = {
        "msg": "Do you see this image?",
        "file": {"_id": "file123", "name": "photo.png", "type": "application/octet-stream", "content": encrypted_file_metadata},
        "files": [{"_id": "file123", "name": "photo.png", "type": "application/octet-stream", "content": encrypted_file_metadata}],
        "attachments": [{"title_link": "/file-upload/file123/photo.png"}],
    }
    encrypted_msg["content"] = e2e.encrypt_message_content("", session_key, kid, attachments=[])
    encrypted_msg["content"] = {
        **encrypted_msg["content"],
        "ciphertext": base64.b64encode(e2e.AESGCM(session_key).encrypt(
            base64.b64decode(encrypted_msg["content"]["iv"]),
            adapter.json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            None,
        )).decode("ascii"),
    }

    decrypted = helper.decrypt_message(encrypted_msg)

    assert decrypted["msg"] == "Do you see this image?"
    assert decrypted["file"]["encryption"]["type"] == "image/png"
    assert decrypted["files"][0]["encryption"]["type"] == "image/png"


def test_e2e_helper_refuses_to_create_password_file_when_absent(tmp_path):
    e2e = adapter._load_e2e_module()
    secret_path = tmp_path / "rocketchat-e2e.env"

    try:
        e2e.load_or_create_e2e_password(file_path=str(secret_path))
    except RuntimeError as exc:
        assert "recovery key is not configured" in str(exc)
    else:
        raise AssertionError("Hermes must not generate Rocket.Chat E2E recovery secrets")

    assert not secret_path.exists()


def test_e2e_start_refuses_to_generate_identity_when_server_has_no_keys(tmp_path):
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        assert path == "/api/v1/e2e.fetchMyKeys"
        return {"success": True}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True}

    async def run():
        helper = e2e.RocketChatE2E(user_id="bot-user-id", password="generated-password", rest_get=fake_get, rest_post=fake_post)
        try:
            await helper.start()
        except RuntimeError as exc:
            assert "identity is not set" in str(exc)
        else:
            raise AssertionError("missing E2E identity must fail read-only startup")

    asyncio.run(run())

    assert posts == []


def test_e2e_start_refuses_to_replace_unreadable_existing_identity():
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        return {"success": True, "public_key": "old-public", "private_key": '{"bad":"blob"}'}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True}

    async def run():
        helper = e2e.RocketChatE2E(
            user_id="bot-user-id",
            password="generated-password",
            rest_get=fake_get,
            rest_post=fake_post,
            force_unreadable_identity=True,
        )
        try:
            await helper.start()
        except RuntimeError as exc:
            assert "will not replace it" in str(exc)
        else:
            raise AssertionError("unreadable E2E identity must not be replaced")

    asyncio.run(run())

    assert posts == []


def test_e2e_does_not_accept_or_reject_unreadable_suggested_room_key():
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        return {"success": True}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True}

    async def run():
        helper = e2e.RocketChatE2E(user_id="bot-user-id", password="generated-password", rest_get=fake_get, rest_post=fake_post)
        public_key, private_key = e2e.generate_rsa_jwks()
        helper.public_key_json = public_key
        helper.private_key_json = private_key
        helper.private_key = e2e.private_key_from_jwk(adapter.json.loads(private_key))
        assert await helper.accept_suggested_key("ROOM1", "not-a-valid-key") is False

    asyncio.run(run())

    assert posts == []


def test_e2e_does_not_request_room_key_via_notify_room_users_stream():
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        return {"success": True}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True, "message": adapter.json.dumps({"result": None})}

    async def run():
        helper = e2e.RocketChatE2E(user_id="bot-user-id", password="generated-password", rest_get=fake_get, rest_post=fake_post)
        assert await helper.request_room_key("ROOM1", "kid1") is False

    asyncio.run(run())

    assert posts == []


def test_e2e_does_not_repost_user_identity_for_room_key_sharing():
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        return {"success": True}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True}

    async def run():
        helper = e2e.RocketChatE2E(user_id="bot-user-id", password="generated-password", rest_get=fake_get, rest_post=fake_post)
        helper.public_key_json = "public-json"
        helper.encrypted_private_key_json = "encrypted-private-json"
        assert await helper.queue_me_for_room_keys() is False

    asyncio.run(run())

    assert posts == []


def test_e2e_refuses_to_reset_existing_room_key():
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        return {"success": True}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True}

    async def run():
        helper = e2e.RocketChatE2E(user_id="bot-user-id", password="generated-password", rest_get=fake_get, rest_post=fake_post)
        try:
            await helper.reset_room_key("ROOM1")
        except RuntimeError as exc:
            assert "read-only" in str(exc)
        else:
            raise AssertionError("reset_room_key must be disabled")

    asyncio.run(run())

    assert posts == []


def test_e2e_does_not_distribute_cached_room_key():
    e2e = adapter._load_e2e_module()
    posts = []

    async def fake_get(path, *, params=None):
        return {"success": True}

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"success": True}

    async def run():
        helper = e2e.RocketChatE2E(
            user_id="bot-user-id",
            password="generated-password",
            rest_get=fake_get,
            rest_post=fake_post,
        )
        kid, session_key_json, session_key = e2e.generate_session_key()
        helper.rooms["ROOM1"] = e2e.RoomE2EState(rid="ROOM1", kid=kid, session_key_json=session_key_json, session_key=session_key)
        assert await helper.distribute_room_key("ROOM1") == 0

    asyncio.run(run())

    assert posts == []


def test_e2e_prepare_does_not_rotate_recreate_request_or_share_keys(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    calls = []

    class FakeE2E:
        def have_room(self, rid):
            return False

        async def request_subscription_keys(self):
            calls.append("request_subscription_keys")
            raise AssertionError("must not request key management")

        async def request_room_key(self, rid, key_id):
            calls.append(("request_room_key", rid, key_id))
            raise AssertionError("must not request room keys")

        async def reset_room_key(self, rid):
            calls.append(("reset_room_key", rid))
            raise AssertionError("must not rotate room keys")

        async def create_room_key(self, rid):
            calls.append(("create_room_key", rid))
            raise AssertionError("must not create room keys")

        async def distribute_room_key(self, rid):
            calls.append(("distribute_room_key", rid))
            raise AssertionError("must not share room keys")

        async def queue_me_for_room_keys(self):
            calls.append("queue_me_for_room_keys")
            raise AssertionError("must not repost user identity")

    rc._e2e = FakeE2E()
    room = adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True, e2e_key_id="kid1")

    async def fake_refresh_subscriptions():
        return None

    async def fake_refresh_room_info(room_arg):
        return room_arg

    async def fake_wait(room_arg):
        calls.append("wait_for_key")
        raise AssertionError("must not poll waiting for key changes")

    monkeypatch.setattr(rc, "_refresh_subscriptions", fake_refresh_subscriptions)
    monkeypatch.setattr(rc, "_refresh_room_info", fake_refresh_room_info)
    monkeypatch.setattr(rc, "_wait_for_e2e_room_key", fake_wait)

    ok, message = asyncio.run(rc._ensure_e2e_exchange_ready(room))

    assert ok is False
    assert "will only use an existing key" in message
    assert calls == []


def test_refresh_subscriptions_does_not_treat_cached_e2e_key_as_room_encrypted(monkeypatch):
    rc = _bare_adapter()
    rc._rooms = {}
    subscribed = []
    prepared = []

    async def fake_api_get(path):
        assert path == "/api/v1/subscriptions.get"
        return {
            "update": [{
                "rid": "ROOM1",
                "name": "mark",
                "t": "d",
                "encrypted": False,
                "E2EKey": "cached-room-key",
                "e2eKeyId": "kid1",
            }]
        }

    async def fake_prepare(room):
        prepared.append((room.rid, room.encrypted, bool(room.e2e_key)))
        return False

    rc._ddp = SimpleNamespace(subscribe_room=lambda rid: subscribed.append(rid))
    async def fake_subscribe_room(rid):
        subscribed.append(rid)
    rc._ddp.subscribe_room = fake_subscribe_room
    monkeypatch.setattr(rc, "_api_get", fake_api_get)
    monkeypatch.setattr(rc, "_prepare_e2e_room", fake_prepare)

    asyncio.run(rc._refresh_subscriptions())

    assert rc._rooms["ROOM1"].encrypted is False
    assert rc._rooms["ROOM1"].e2e_key == "cached-room-key"
    assert prepared == [("ROOM1", False, True)]
    assert subscribed == ["ROOM1"]


def test_handle_room_e2e_toggle_refreshes_subscriptions(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=False)}
    refreshes = []

    async def fake_refresh():
        refreshes.append(True)

    monkeypatch.setattr(rc, "_refresh_subscriptions", fake_refresh)

    msg = {
        "_id": "toggle1",
        "rid": "ROOM1",
        "t": "room_e2e_enabled",
        "msg": "mark",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }

    async def run():
        await rc._handle_rc_message(msg)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert rc._rooms["ROOM1"].encrypted is True
    assert refreshes == [True]


def test_handle_e2e_message_marks_room_encrypted_and_refreshes_before_decrypt(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    room = adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=False)
    rc._rooms = {"ROOM1": room}
    rc._e2e = object()
    events = []
    refreshes = []

    async def fake_refresh():
        refreshes.append(True)
        rc._rooms["ROOM1"].e2e_suggested_key = "suggested"

    async def fake_decrypt(msg, room_info):
        assert room_info.encrypted is True
        assert room_info.e2e_suggested_key == "suggested"
        out = dict(msg)
        out["e2e"] = "done"
        out["msg"] = "decrypted after refresh"
        return out

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "_refresh_subscriptions", fake_refresh)
    monkeypatch.setattr(rc, "_decrypt_e2e_message", fake_decrypt)
    monkeypatch.setattr(rc, "build_source", fake_build_source)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "e2e-refresh-msg1",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"},
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }

    asyncio.run(rc._handle_rc_message(msg))

    assert refreshes == [True]
    assert events[0].text == "decrypted after refresh"


def test_handle_e2e_message_uses_rooms_info_when_subscription_encrypted_flag_lags(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=False)}
    rc._e2e = object()
    seen = []
    events = []

    async def fake_refresh_subscriptions():
        # Mirrors Rocket.Chat live behavior seen during testing: room is encrypted
        # according to rooms.info, but subscriptions.get still reports encrypted=false.
        rc._rooms["ROOM1"] = adapter._RoomInfo(
            rid="ROOM1",
            name="mark",
            t="d",
            encrypted=False,
            e2e_key="cached-room-key",
            e2e_key_id="kid1",
        )

    async def fake_refresh_room_info(room_info):
        room_info.encrypted = True
        rc._rooms[room_info.rid] = room_info
        return room_info

    async def fake_decrypt(msg, room_info):
        seen.append((room_info.encrypted, room_info.e2e_key))
        out = dict(msg)
        out["e2e"] = "done"
        out["msg"] = "decrypted after authoritative room refresh"
        return out

    monkeypatch.setattr(rc, "_refresh_subscriptions", fake_refresh_subscriptions)
    monkeypatch.setattr(rc, "_refresh_room_info", fake_refresh_room_info)
    monkeypatch.setattr(rc, "_decrypt_e2e_message", fake_decrypt)
    monkeypatch.setattr(rc, "build_source", lambda **kwargs: SimpleNamespace(**kwargs))

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "e2e-lag-msg1",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"},
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }

    asyncio.run(rc._handle_rc_message(msg))

    assert seen == [(True, "cached-room-key")]
    assert events[0].text == "decrypted after authoritative room refresh"


def test_handle_rc_message_decrypts_e2e_dm_before_emitting(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    room = adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True)
    rc._rooms = {"ROOM1": room}
    rc._e2e = object()
    events = []

    async def fake_decrypt(msg, room_info):
        assert msg["t"] == "e2e"
        assert room_info.rid == "ROOM1"
        out = dict(msg)
        out["t"] = "e2e"
        out["e2e"] = "done"
        out["msg"] = "decrypted hello"
        return out

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "_decrypt_e2e_message", fake_decrypt)
    monkeypatch.setattr(rc, "build_source", fake_build_source)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "e2e-msg1",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"},
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }

    asyncio.run(rc._handle_rc_message(msg))

    assert len(events) == 1
    assert events[0].text == "decrypted hello"


def test_send_uses_chat_send_message_for_encrypted_dm(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True)}
    rc._e2e = object()
    calls = []

    async def fake_prepare(room):
        return True

    class FakeE2E:
        def encrypt_message_payload(self, rid, text):
            return {"rid": rid, "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"}, "t": "e2e", "e2e": "pending"}

    async def fake_api_post(path, payload):
        calls.append((path, payload))
        return {"success": True, "message": {"_id": "encrypted-msg"}}

    rc._e2e = FakeE2E()
    monkeypatch.setattr(rc, "_prepare_e2e_room", fake_prepare)
    monkeypatch.setattr(rc, "_api_post", fake_api_post)

    result = asyncio.run(rc.send("ROOM1", "encrypted response"))

    assert result.success is True
    assert result.message_id == "encrypted-msg"
    assert calls[0][0] == "/api/v1/chat.sendMessage"
    assert calls[0][1]["message"]["t"] == "e2e"
    assert "text" not in calls[0][1]["message"]


def test_e2e_command_arms_one_shot_exchange(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._e2e = object()
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=False)}
    sent = []
    prepared = []

    async def fake_ready(room, *, wait_for_key=True):
        assert wait_for_key is False
        prepared.append(room.rid)
        return True, "E2E ready. Send one encrypted message now."

    async def fake_plain(chat_id, content):
        sent.append((chat_id, content))
        return adapter.SendResult(success=True)

    monkeypatch.setattr(rc, "_ensure_e2e_exchange_ready", fake_ready)
    monkeypatch.setattr(rc, "_send_plain_text", fake_plain)

    msg = {
        "_id": "cmd-e2e",
        "rid": "ROOM1",
        "msg": "e2e1",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert prepared == ["ROOM1"]
    assert "ROOM1" in rc._e2e_armed_until
    assert sent == [("ROOM1", "E2E ready. Send one encrypted message now.")]


def test_e2e_command_enables_persistent_mode(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._e2e = object()
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=False)}
    sent = []

    async def fake_ready(room, *, wait_for_key=True):
        assert wait_for_key is False
        return True, "ready"

    async def fake_plain(chat_id, content):
        sent.append((chat_id, content))
        return adapter.SendResult(success=True)

    monkeypatch.setattr(rc, "_ensure_e2e_exchange_ready", fake_ready)
    monkeypatch.setattr(rc, "_send_plain_text", fake_plain)

    msg = {
        "_id": "cmd-e2e-on",
        "rid": "ROOM1",
        "msg": "e2e_on",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert "ROOM1" in rc._e2e_persistent_rooms
    assert "ROOM1" not in rc._e2e_disable_after_reply
    assert sent and "persistent mode ready" in sent[0][1]


def test_e2e_status_command_is_handled_locally(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._e2e = object()
    sent = []
    events = []

    async def fake_plain(chat_id, content):
        sent.append((chat_id, content))
        return adapter.SendResult(success=True)

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "_send_plain_text", fake_plain)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "cmd-status",
        "rid": "ROOM1",
        "msg": "e2e_status",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert not events
    assert sent and "E2E status:" in sent[0][1]


def test_e2e_cancel_command_disarms_and_restores_plaintext(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._e2e = object()
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True)}
    rc._e2e_armed_until["ROOM1"] = time.time() + 60
    rc._e2e_disable_after_reply.add("ROOM1")
    calls = []

    async def fake_api_post(path, payload):
        calls.append((path, payload))
        return {"success": True, "message": {"_id": "control-msg"}}

    monkeypatch.setattr(rc, "_api_post", fake_api_post)

    msg = {
        "_id": "cmd-cancel",
        "rid": "ROOM1",
        "msg": "e2e_off",
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert "ROOM1" not in rc._e2e_armed_until
    assert "ROOM1" not in rc._e2e_disable_after_reply
    assert calls[0] == ("/api/v1/rooms.saveRoomSettings", {"rid": "ROOM1", "encrypted": False})
    assert calls[1][0] == "/api/v1/chat.postMessage"
    assert "cancelled" in calls[1][1]["text"]


def test_armed_e2e_message_disables_room_after_encrypted_reply(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True)}
    rc._e2e = object()
    rc._e2e_armed_until["ROOM1"] = time.time() + 60
    events = []

    async def fake_decrypt(msg, room_info):
        out = dict(msg)
        out["msg"] = "private question"
        out["e2e"] = "done"
        return out

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "_decrypt_e2e_message", fake_decrypt)
    monkeypatch.setattr(rc, "build_source", fake_build_source)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "one-shot-e2e-msg",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"},
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert events and events[0].text == "private question"
    assert "ROOM1" in rc._e2e_disable_after_reply

    calls = []

    class FakeE2E:
        def encrypt_message_payload(self, rid, text):
            return {"rid": rid, "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"}, "t": "e2e", "e2e": "pending"}

    async def fake_prepare(room):
        return True

    async def fake_api_post(path, payload):
        calls.append((path, payload))
        return {"success": True, "message": {"_id": "reply-msg"}}

    rc._e2e = FakeE2E()
    monkeypatch.setattr(rc, "_prepare_e2e_room", fake_prepare)
    monkeypatch.setattr(rc, "_api_post", fake_api_post)

    result = asyncio.run(rc.send("ROOM1", "private answer"))
    assert result.success is True
    assert calls[-1] == ("/api/v1/rooms.saveRoomSettings", {"rid": "ROOM1", "encrypted": False})
    assert "ROOM1" not in rc._e2e_disable_after_reply


def test_persistent_e2e_message_does_not_disable_room_after_reply(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True)}
    rc._e2e = object()
    rc._e2e_persistent_rooms.add("ROOM1")
    rc._e2e_armed_until["ROOM1"] = time.time() + 60
    events = []

    async def fake_decrypt(msg, room_info):
        out = dict(msg)
        out["msg"] = "persistent private question"
        out["e2e"] = "done"
        return out

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    monkeypatch.setattr(rc, "_decrypt_e2e_message", fake_decrypt)
    monkeypatch.setattr(rc, "build_source", fake_build_source)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "persistent-e2e-msg",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"},
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert events and events[0].text == "persistent private question"
    assert "ROOM1" not in rc._e2e_disable_after_reply

    calls = []

    class FakeE2E:
        def encrypt_message_payload(self, rid, text):
            return {"rid": rid, "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"}, "t": "e2e", "e2e": "pending"}

    async def fake_prepare(room):
        return True

    async def fake_api_post(path, payload):
        calls.append((path, payload))
        return {"success": True, "message": {"_id": "reply-msg"}}

    rc._e2e = FakeE2E()
    monkeypatch.setattr(rc, "_prepare_e2e_room", fake_prepare)
    monkeypatch.setattr(rc, "_api_post", fake_api_post)

    result = asyncio.run(rc.send("ROOM1", "private answer"))
    assert result.success is True
    assert all(call[0] != "/api/v1/rooms.saveRoomSettings" for call in calls)
    assert "ROOM1" in rc._e2e_persistent_rooms


def test_encrypted_e2e_off_disables_persistent_mode(monkeypatch):
    rc = _bare_adapter()
    rc.e2e_enabled = True
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="mark", t="d", encrypted=True)}
    rc._e2e_persistent_rooms.add("ROOM1")
    calls = []
    events = []

    class FakeE2E:
        def encrypt_message_payload(self, rid, text):
            return {"rid": rid, "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"}, "t": "e2e", "e2e": "pending"}

    async def fake_decrypt(msg, room_info):
        out = dict(msg)
        out["msg"] = "e2e_off"
        out["e2e"] = "done"
        return out

    async def fake_prepare(room):
        return True

    async def fake_api_post(path, payload):
        calls.append((path, payload))
        return {"success": True, "message": {"_id": "control-msg"}}

    async def fake_handle_message(event):
        events.append(event)

    rc._e2e = FakeE2E()
    monkeypatch.setattr(rc, "_decrypt_e2e_message", fake_decrypt)
    monkeypatch.setattr(rc, "_prepare_e2e_room", fake_prepare)
    monkeypatch.setattr(rc, "_api_post", fake_api_post)
    monkeypatch.setattr(rc, "handle_message", fake_handle_message)

    msg = {
        "_id": "persistent-off-msg",
        "rid": "ROOM1",
        "t": "e2e",
        "content": {"algorithm": "rc.v2.aes-sha2", "kid": "kid", "iv": "iv", "ciphertext": "ct"},
        "ts": {"$date": int(time.time() * 1000)},
        "u": {"_id": "human-id", "username": "mark", "name": "Mark"},
    }
    asyncio.run(rc._handle_rc_message(msg))

    assert not events
    assert "ROOM1" not in rc._e2e_persistent_rooms
    assert calls[0][0] == "/api/v1/chat.sendMessage"
    assert calls[-1] == ("/api/v1/rooms.saveRoomSettings", {"rid": "ROOM1", "encrypted": False})
