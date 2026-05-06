# Rocket.Chat Hermes gateway plugin handoff

Date: 2026-05-05

Goal: build a Hermes-native Rocket.Chat gateway integration usable by Mark and Jake. Do it the Hermes way, not as an OpenClaw shim, but use Jake Miller's MIT `rocketchat-openclaw` plugin as a protocol/reliability reference.

Current status

Created and enabled user plugin:

- `/Users/mark/.hermes/plugins/rocketchat/plugin.yaml`
- `/Users/mark/.hermes/plugins/rocketchat/__init__.py`
- `/Users/mark/.hermes/plugins/rocketchat/adapter.py`
- `/Users/mark/.hermes/plugins/rocketchat/test_adapter.py`

Plugin list showed:

- `rocketchat-platform` enabled

Memory was updated with the durable fact that the Rocket.Chat plugin lives at `/Users/mark/.hermes/plugins/rocketchat` and registers the `rocketchat` platform.

Verification already run

From `/Users/mark/.hermes/hermes-agent`:

```bash
venv/bin/python -m py_compile /Users/mark/.hermes/plugins/rocketchat/adapter.py
venv/bin/python - <<'PY'
import importlib.util, sys
from pathlib import Path
p=Path('/Users/mark/.hermes/plugins/rocketchat/adapter.py')
spec=importlib.util.spec_from_file_location('rocketchat_adapter', p)
mod=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=mod
spec.loader.exec_module(mod)
print('loaded', mod.RocketChatAdapter.__name__, mod.check_requirements())
print('date ms', int(mod._date_to_epoch({'$date':1688434691876})))
print('ws', mod._websocket_url('https://chat.example.com'))
PY
venv/bin/hermes plugins enable rocketchat-platform
venv/bin/python - <<'PY'
from hermes_cli.plugins import discover_plugins
from gateway.platform_registry import platform_registry

discover_plugins(force=True)
entry = platform_registry.get('rocketchat')
print('registered', bool(entry))
if entry:
    print(entry.name, entry.label, entry.allowed_users_env, entry.allow_all_env, entry.max_message_length)
PY
venv/bin/python -m pytest /Users/mark/.hermes/plugins/rocketchat/test_adapter.py -q -o 'addopts='
```

Result:

```text
4 passed
```

Important research notes

Rocket.Chat docs checked:

- Realtime API docs: `https://developer.rocket.chat/apidocs/realtimeapi`
- Post Message: `https://developer.rocket.chat/apidocs/post-message`
- Get Subscriptions (Realtime): `https://developer.rocket.chat/apidocs/get-subscriptions-realtime`
- Get Message: `https://developer.rocket.chat/apidocs/get-message`
- Get Thread Messages: `https://developer.rocket.chat/apidocs/get-thread-messages`

Key doc finding: Rocket.Chat now marks DDP/realtime methods as deprecated and recommends REST for new long-term development. However, for a messaging gateway that needs inbound realtime messages, REST-only would require polling or a Rocket.Chat-side app/webhook. Current plugin uses REST for stable operations and DDP only for inbound realtime delivery.

Jake's plugin inspected:

- Repo: `https://gitlab.com/doctorclaw/rocketchat-openclaw`
- Local clone used during work: `/tmp/rocketchat-openclaw`
- Useful files:
  - `src/rocketchat/realtime.ts`
  - `src/rocketchat/monitor.ts`
  - `src/rocketchat/client.ts`
  - `src/rocketchat/send.ts`

High-value ideas ported or mirrored:

- DDP `/websocket` connect/login/resume
- `stream-room-messages` subscriptions
- ping/pong
- stale reconnect replay drop
- dedup with 6-hour TTL and 500 cap
- room type handling: `d` DM, `p/g` group, `c` channel
- REST retry on transient HTTP failures
- `tmid` threading
- `chat.react` ack reactions
- `subscriptions.read` mark-as-read

Current plugin capabilities

- Hermes platform registration via `ctx.register_platform(...)`
- Platform name: `rocketchat`
- Env vars:
  - `ROCKETCHAT_URL`
  - `ROCKETCHAT_USER_ID`
  - `ROCKETCHAT_AUTH_TOKEN`
  - `ROCKETCHAT_ALLOWED_USERS`
  - `ROCKETCHAT_ALLOW_ALL_USERS`
  - `ROCKETCHAT_HOME_CHANNEL`
- Auth verification via `/api/v1/me`
- Room discovery via `/api/v1/subscriptions.get`
- Realtime inbound via DDP `/websocket`
- Text send via `/api/v1/chat.postMessage`
- Thread replies using `tmid`
- Configurable `reply_mode`: `thread`, `auto`, `off`/`channel`
- `auto_thread_chars`
- `require_mention`
- per-room config override skeleton
- ack reactions via `/api/v1/chat.react`
- mark-as-read via `/api/v1/subscriptions.read`
- typing indicator via `/api/v1/typing`
- basic edit via `/api/v1/chat.update`
- Hermes prompt platform hint

Potential config

Environment:

```bash
ROCKETCHAT_URL=https://your-rocket-chat.example.com
ROCKETCHAT_USER_ID=your-bot-user-id
ROCKETCHAT_AUTH_TOKEN=your-bot-auth-token
ROCKETCHAT_ALLOWED_USERS=mark,jake
ROCKETCHAT_HOME_CHANNEL=ROOM_ID

# Optional
ROCKETCHAT_ALLOW_ALL_USERS=true
ROCKETCHAT_REPLY_MODE=thread
ROCKETCHAT_AUTO_THREAD_CHARS=280
ROCKETCHAT_REQUIRE_MENTION=false
ROCKETCHAT_ACK_REACTION=eyes
ROCKETCHAT_MARK_AS_READ=true
```

Config YAML:

```yaml
gateway:
  platforms:
    rocketchat:
      enabled: true
      token: "<bot auth token>"
      extra:
        url: "https://your-rocket-chat.example.com"
        user_id: "<bot user id>"
        reply_mode: "thread"
        auto_thread_chars: 280
        require_mention: false
        ack_reaction: "eyes"
        mark_as_read: true
        rooms:
          ROOM_ID:
            require_mention: true
            reply_mode: "thread"
```

Next steps

1. Live smoke test against Mark's private Rocket.Chat server.
   - Need `ROCKETCHAT_URL`, bot `ROCKETCHAT_USER_ID`, and bot `ROCKETCHAT_AUTH_TOKEN`.
   - Do not print secrets.
   - Add values to `~/.hermes/.env` or config safely.
   - Restart gateway or run foreground.

2. Foreground debug command:

```bash
cd /Users/mark/.hermes/hermes-agent
venv/bin/hermes gateway run
```

Expected logs:

```text
Rocket.Chat: authenticated as @<botname> (...) on https://...
Rocket.Chat: tracking N subscribed rooms
Rocket.Chat: connecting realtime websocket to wss://.../websocket
```

3. Send a test message in a subscribed Rocket.Chat DM/room.

4. Watch logs:

```bash
tail -f ~/.hermes/logs/gateway.log
```

5. Fix any Rocket.Chat version-specific DDP quirks.

6. Add media support:
   - inbound authenticated file download into Hermes cache paths
   - outbound `rooms.media` + `rooms.mediaConfirm`

7. Add one-shot REST send helper so `send_message` can send to Rocket.Chat outside a running gateway process.

8. Package for Jake:
   - README
   - sample config
   - live test checklist
   - attribution to Jake's MIT plugin for reliability/protocol references

Known caveats

- Not live-tested against a real Rocket.Chat server yet.
- DDP is deprecated in Rocket.Chat docs, but still necessary for realtime inbound unless we switch to polling/webhooks/Rocket.Chat App later.
- Media delivery is not implemented yet.
- Per-room config skeleton exists but has not been live validated.
- The plugin is currently a user plugin, not an upstream Hermes PR.
