# Rocket.Chat Hermes gateway plugin handoff

Date: 2026-05-25

Goal: provide an update-safe Hermes-native Rocket.Chat gateway plugin usable by Mark and Jake. It is a Python/asyncio Hermes user plugin, not an OpenClaw shim, though Jake Miller's MIT `rocketchat-openclaw` plugin was used as a protocol/reliability reference.

## Current status

Plugin directory:

```text
~/.hermes/plugins/rocketchat
```

Packaged files:

```text
rocketchat/__init__.py
rocketchat/adapter.py
rocketchat/e2e.py
rocketchat/plugin.yaml
rocketchat/test_adapter.py
rocketchat/README.md
rocketchat/HANDOFF.md
rocketchat/AGENTS.md
```

Platform registration:

```text
rocketchat
```

Plugin registration:

```text
rocketchat-platform
```

Current local commit used for packaging:

```text
v0.3.3 local fix (`fix: use decrypted attachment E2EE file metadata`)
```

Plugin version: `0.3.3`

## Current capabilities

- Hermes platform registration via `ctx.register_platform(...)`.
- REST auth verification via `/api/v1/me`.
- Room discovery via `/api/v1/subscriptions.get`.
- Realtime inbound delivery through DDP `/websocket` and `stream-room-messages`.
- Text send through `/api/v1/chat.postMessage`.
- Thread replies using Rocket.Chat `tmid`.
- Configurable reply modes: `thread`, `auto`, `off`/channel.
- Mention/allowlist controls.
- Ack reactions via `/api/v1/chat.react`.
- Mark-as-read via `/api/v1/subscriptions.read`.
- Typing indicator via `/api/v1/typing`.
- Basic edit via `/api/v1/chat.update`.
- Delete message via `/api/v1/chat.delete`.
- Native outbound media upload for images, documents, video, and voice/audio.
- Generated Hermes images delivered as native Rocket.Chat attachments.
- Remote image/GIF URL download, size-check, native upload, and temporary-file cleanup.
- Thread-aware native media upload.
- Rocket.Chat two-step upload flow: `rooms.media/{rid}` then `rooms.mediaConfirm`.
- Cleanup of empty markdown artifacts like `![title]()` after native file extraction.
- Multiple-image sending through sequential native uploads.
- Recent-message backfill on reconnect through `channels.history`, `groups.history`, and `im.history`.
- Rocket.Chat-friendly text fallbacks for clarify and slash-confirm flows.
- Optional DM-only Rocket.Chat E2EE support.
- Inbound E2EE DM image attachments are decrypted from Rocket.Chat's AES-CTR file payloads using decrypted attachment-level file metadata and then verified with image magic bytes before caching for Hermes vision.
- One-shot encrypted DM exchanges with `e2e1`.
- Persistent encrypted DM mode with `e2e_on`; disable while encrypted by sending `e2e_off` as encrypted text. `e2e_status` reports helper/key state. Legacy `/e2e` slash aliases remain accepted but are not recommended because Rocket.Chat may display parser warnings or block slash commands while encrypted.
- E2E key-sharing helpers for existing rooms, stale-key rotation fallback, and bot key-share queueing without rotating the bot RSA identity.

## Verification run

The current package was verified with:

```bash
cd /Users/mark/.hermes/plugins/rocketchat
python -m py_compile adapter.py
python -m pytest test_adapter.py -q
```

Expected result:

```text
47 passed
```

## Configuration

Minimum environment variables:

```bash
ROCKETCHAT_URL=https://your-rocket-chat.example.com
ROCKETCHAT_USER_ID=your-bot-user-id
ROCKETCHAT_AUTH_TOKEN=your-bot-auth-token
ROCKETCHAT_ALLOWED_USERS=mark,jake
```

Optional environment variables:

```bash
ROCKETCHAT_ALLOW_ALL_USERS=false
ROCKETCHAT_HOME_CHANNEL=ROOM_ID
ROCKETCHAT_REPLY_MODE=thread
ROCKETCHAT_AUTO_THREAD_CHARS=280
ROCKETCHAT_REQUIRE_MENTION=false
ROCKETCHAT_ACK_REACTION=eyes
ROCKETCHAT_MARK_AS_READ=true
ROCKETCHAT_BACKFILL_ON_CONNECT=true
ROCKETCHAT_BACKFILL_WINDOW_SECONDS=300

# Optional DM-only E2EE. If this file is absent and E2E is enabled,
# Hermes generates a local recovery password file with mode 0600.
ROCKETCHAT_E2E_ENABLED=true
ROCKETCHAT_E2E_DM_ONLY=true
ROCKETCHAT_E2E_PASSWORD_FILE=~/.hermes/secrets/rocketchat-e2e.env
```

Example config:

```yaml
gateway:
  platforms:
    rocketchat:
      enabled: true
      extra:
        url: "https://your-rocket-chat.example.com"
        user_id: "<bot user id>"
        reply_mode: "thread"
        auto_thread_chars: 280
        require_mention: false
        ack_reaction: "eyes"
        mark_as_read: true
        backfill_on_connect: true
        backfill_window_seconds: 300
        e2e:
          enabled: true
          dm_only: true
          password_file: "~/.hermes/secrets/rocketchat-e2e.env"
```

## Install/upgrade quick path

```bash
mkdir -p ~/.hermes/plugins
cp -R rocketchat ~/.hermes/plugins/rocketchat
hermes plugins enable rocketchat-platform
hermes gateway restart
```

Restarting the gateway is required for plugin code changes to load.

## Live test checklist

1. Confirm gateway logs show successful Rocket.Chat auth and websocket connection.
2. Send a normal message from an allowed user.
3. Confirm Hermes replies in the expected room/thread.
4. Send or generate an image and confirm native Rocket.Chat attachment delivery.
5. Test a remote image/GIF URL flow and confirm native upload instead of bare URL output.
6. Confirm command approvals render usable `/approve`, `/always`, `/cancel` text prompts.
7. Restart gateway and test whether messages sent during a short downtime are recovered by backfill.
8. For one-shot E2E: send `e2e1`, wait for ready, send one encrypted message, and confirm Hermes answers encrypted then returns the DM to plaintext.
9. For persistent E2E: send `e2e_on`, wait for persistent-ready, chat for multiple turns, then send encrypted `e2e_off` and confirm the DM returns to plaintext.

## Protocol notes

Rocket.Chat docs now mark DDP/realtime methods as deprecated and recommend REST for long-term development. This plugin uses REST for stable operations and DDP only for inbound realtime delivery. A future alternative could replace DDP with polling, outgoing webhooks, or a Rocket.Chat App, but the current approach gives a practical Hermes gateway today.

## Attribution

Jake Miller's MIT `rocketchat-openclaw` plugin informed the Rocket.Chat protocol and reliability approach, especially DDP connection behavior, room subscriptions, ping/pong, stale replay handling, deduplication, room type handling, retries, threading, ack reactions, and mark-as-read behavior. The implementation here is Hermes-native Python using Hermes gateway adapter semantics.
