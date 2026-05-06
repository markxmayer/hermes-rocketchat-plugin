import asyncio
import importlib.util
import sys
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
