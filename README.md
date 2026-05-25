# Hermes Rocket.Chat Platform Plugin

Update-safe Hermes user plugin that adds Rocket.Chat as a gateway platform.

This package is meant to be copied into a Hermes profile under:

```text
~/.hermes/plugins/rocketchat
```

It registers the platform name:

```text
rocketchat
```

## What this upgrade provides

- Rocket.Chat platform registration as a Hermes user plugin, no Hermes core patch required.
- REST authentication against `/api/v1/me`.
- Room discovery through `/api/v1/subscriptions.get`.
- Realtime inbound messages through Rocket.Chat DDP `/websocket` and `stream-room-messages`.
- Outbound text messages through `/api/v1/chat.postMessage`.
- Thread-aware replies using Rocket.Chat `tmid`.
- Mention/allowlist controls for safe bot access.
- Reply modes: `thread`, `auto`, and channel/off behavior.
- Ack reactions through `/api/v1/chat.react`.
- Mark-as-read through `/api/v1/subscriptions.read`.
- Typing indicator through `/api/v1/typing`.
- Basic message edits through `/api/v1/chat.update`.
- Message deletion through `/api/v1/chat.delete` for ephemeral cleanup flows.
- Native outbound media uploads for images, documents, video, and voice/audio.
- Generated Hermes images delivered as real Rocket.Chat attachments.
- Remote image/GIF URL handling: download temporarily, size-check, upload natively, then clean up.
- Thread-aware native media uploads.
- Rocket.Chat native upload flow: `rooms.media/{rid}` plus `rooms.mediaConfirm`.
- Cleanup of empty markdown artifacts such as `![title]()` after native file extraction.
- Multiple-image sending via sequential native uploads.
- Missed-message backfill on reconnect using recent room history endpoints.
- Rocket.Chat-friendly text fallbacks for clarify/confirmation prompts where buttons are unavailable.
- Optional DM-only Rocket.Chat E2EE support: decrypt incoming encrypted DM text and encrypt outgoing DM replies. If the Hermes user has no E2E identity yet, the plugin can generate its own local recovery password and publish a new keypair to Rocket.Chat.
- Plugin-local `e2e1` control command for one-shot encrypted DM exchanges using Rocket.Chat's official room-key/suggested-key flow. Legacy slash aliases are accepted but not recommended because Rocket.Chat may show parser warnings.
- Persistent DM E2EE mode: `e2e_on` keeps the DM encrypted across turns until the user sends `e2e_off` as an encrypted message. Also supports `e2e_status` and stale-key/key-sharing recovery helpers.
- Test coverage for transport, upload, backfill, prompt fallback, and deletion behavior.

## Files

```text
rocketchat/
  __init__.py
  adapter.py
  e2e.py
  plugin.yaml
  test_adapter.py
  README.md
  HANDOFF.md
  AGENTS.md
```

## Install

1. Copy the extracted `rocketchat` directory to the target Hermes home:

```bash
mkdir -p ~/.hermes/plugins
cp -R rocketchat ~/.hermes/plugins/rocketchat
```

2. Enable the plugin:

```bash
hermes plugins enable rocketchat-platform
```

3. Add Rocket.Chat credentials to `~/.hermes/.env` or Hermes config. Do not paste secrets into chat/logs.

Minimum environment variables:

```bash
ROCKETCHAT_URL=https://your-rocket-chat.example.com
ROCKETCHAT_USER_ID=your-bot-user-id
ROCKETCHAT_AUTH_TOKEN=your-bot-auth-token
ROCKETCHAT_ALLOWED_USERS=mark,jake
```

Common optional environment variables:

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

4. Enable the platform in `~/.hermes/config.yaml` if needed:

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

5. Restart Hermes gateway after installing or upgrading:

```bash
hermes gateway restart
```

Or foreground smoke test:

```bash
hermes gateway run
```

Expected startup log shape:

```text
Rocket.Chat: authenticated as @<botname> (...) on https://...
Rocket.Chat: tracking N subscribed rooms
Rocket.Chat: connecting realtime websocket to wss://.../websocket
```

## Verify

From a Hermes source/venv install, adjust paths as needed:

```bash
python -m py_compile ~/.hermes/plugins/rocketchat/adapter.py
python -m pytest ~/.hermes/plugins/rocketchat/test_adapter.py -q -o 'addopts='
```

Expected current result:

```text
41 passed
```

## Live smoke-test checklist

- Send Hermes a normal text message from an allowed Rocket.Chat user.
- Confirm Hermes replies in the expected room/thread.
- Ask Hermes to generate or send an image and confirm it appears as a native Rocket.Chat attachment.
- Send a remote image/GIF URL through a Hermes flow and confirm it uploads as an attachment, not just a bare URL.
- Confirm a long reply or thread reply preserves `tmid` behavior according to configured reply mode.
- Restart the gateway, send a message during downtime, then confirm recent-message backfill catches it after reconnect if within the backfill window.
- Test `/approve`, `/always`, and `/cancel` style confirmation text in Rocket.Chat if using command approval flows.
- For one-shot E2EE DMs: enable plugin E2E, send `e2e1` in the DM, wait for the plaintext “E2E ready” control reply, then send one encrypted message. Hermes should decrypt it, answer encrypted, and then disable room E2E again.
- For persistent E2EE DMs: send `e2e_on`, wait for “E2E persistent mode ready”, then continue chatting. To leave persistent mode, send encrypted `e2e_off`. Use `e2e_status` before encryption or encrypted `e2e_status` during persistent mode to inspect helper/key state. Legacy `/e2e` slash aliases are accepted but may trigger Rocket.Chat “unknown command”/“not allowed” UI messages.

## Notes and caveats

- This is a user plugin, not an upstream Hermes core platform.
- It uses REST for stable operations and DDP only for inbound realtime delivery.
- E2EE is deliberately conservative: DM/private-room text crypto is implemented, but the adapter defaults to DM-only and does not support encrypted media/file upload. Gateway-side E2EE initialization can generate and manage Hermes's own local recovery password/keypair when the Hermes Rocket.Chat user has no existing E2E identity. Use deliberate non-slash controls: `e2e1` for one-shot, `e2e_on` for persistent mode, `e2e_off` to disable, and `e2e_status` for diagnostics. Do not put sensitive content in control messages; send sensitive content only after the ready prompt. Slash aliases are accepted for compatibility but are not recommended because Rocket.Chat's slash-command parser may show parser warnings or block them while encrypted.
- Rocket.Chat's DDP/realtime APIs are marked deprecated in current docs, but still remain useful for bot-style realtime inbound messages unless replacing with polling, webhooks, or a Rocket.Chat App.
- Keep secrets in `.env` or config; never commit them into this plugin directory.

## Attribution

Reliability/protocol ideas were informed by Jake Miller's MIT `rocketchat-openclaw` plugin, then ported into Hermes-native Python/asyncio `BasePlatformAdapter` semantics.
