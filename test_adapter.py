import asyncio
import importlib.util
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


def _bare_adapter():
    rc = object.__new__(adapter.RocketChatAdapter)
    rc.base_url = "https://chat.example.com"
    rc.user_id = "bot-user-id"
    rc.auth_token = "secret-token"
    rc._bot_username = "hermes"
    rc._rooms = {"ROOM1": adapter._RoomInfo(rid="ROOM1", name="general", t="d")}
    rc._dedup = adapter.MessageDeduplicator(max_size=500, ttl_seconds=6 * 60 * 60)
    rc.ack_reaction = ""
    rc.mark_as_read = False
    rc.require_mention = False
    rc.rooms_config = {}
    rc.e2e_enabled = False
    rc.e2e_dm_only = True
    rc.e2e_auto_create_dm_key = False
    rc._e2e = None
    return rc


def test_extract_inbound_image_prefers_original_file_link(monkeypatch):
    rc = _bare_adapter()
    downloads = []

    async def fake_download(url, filename, content_type):
        downloads.append((url, filename, content_type))
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
    assert downloads == [("https://chat.example.com/file-upload/file123/screen.png", "screen.png", "image/png")]


def test_handle_rc_message_emits_photo_event_with_cached_image(monkeypatch):
    rc = _bare_adapter()
    events = []

    def fake_build_source(**kwargs):
        return SimpleNamespace(**kwargs)

    async def fake_handle_message(event):
        events.append(event)

    async def fake_download(url, filename, content_type):
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
