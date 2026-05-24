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
