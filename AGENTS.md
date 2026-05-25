# Agent instructions for the Hermes Rocket.Chat plugin

This repository is an update-safe Hermes user plugin for Rocket.Chat gateway support. Treat it as shared code that may be installed by another Hermes user or bot operator.

## Non-negotiable safety rules

- Never print, commit, package, or ask the user to paste Rocket.Chat auth tokens, user IDs paired with tokens, E2E passwords, recovery phrases, private keys, room keys, or `.env` contents.
- Do not copy Mark's E2E password file or key material to Jake or any other operator. Each Rocket.Chat bot/user must create or use its own E2E recovery password and keypair.
- Never create, rotate, request, share, accept/reject, reset, repost, or force-replace Rocket.Chat E2E identity keys or room keys from Hermes. Hermes may only read/decrypt keys that already exist; missing/unreadable keys must be fixed by the user in Rocket.Chat clients.
- Keep E2E support DM/private-room scoped. Inbound encrypted image attachments must be decrypted with Rocket.Chat's per-file AES-CTR metadata and then verified with image magic bytes; encrypted media/file upload from Hermes still requires a separate implementation and test pass.
- The gateway decrypts messages before passing them to Hermes. Rocket.Chat E2EE does not hide message text from the selected model provider unless the operator is using a local model.

## E2E configuration

Minimum E2E environment/config knobs:

```bash
ROCKETCHAT_E2E_ENABLED=true
ROCKETCHAT_E2E_DM_ONLY=true
ROCKETCHAT_E2E_PASSWORD_FILE=~/.hermes/secrets/rocketchat-e2e.env
```

Equivalent config shape:

```yaml
gateway:
  platforms:
    rocketchat:
      enabled: true
      extra:
        e2e:
          enabled: true
          dm_only: true
          password_file: "~/.hermes/secrets/rocketchat-e2e.env"
```

The plugin also accepts an explicit `e2e.password` config value or `ROCKETCHAT_E2E_PASSWORD`, but prefer `ROCKETCHAT_E2E_PASSWORD_FILE` so secrets stay out of normal config and logs.

## How the plugin reads its E2E secret

When E2E is enabled, adapter startup reads the configured E2E recovery password from config/env/file. This password must already match the Rocket.Chat client identity; Hermes must not generate or publish E2E identity material.

Behavior:

1. If an explicit password is configured, use it.
2. Else if `ROCKETCHAT_E2E_PASSWORD` is set, use it.
3. Else if `ROCKETCHAT_E2E_PASSWORD_FILE` or configured `password_file` exists, read one of these formats:
   - `ROCKETCHAT_E2E_PASSWORD=<secret>`
   - `E2E_PASSWORD=<secret>`
   - `PASSWORD=<secret>`
   - a bare one-line secret
4. Else E2E startup fails with a message telling the user to configure the key first.

Do not generate RSA keys or recovery passwords in Hermes. The plugin uses the configured recovery password only to unwrap an existing Rocket.Chat private key. If the bot user has no E2E identity, create/set it in Rocket.Chat first.

## Identity and recovery workflow

On startup, `RocketChatE2E.start()` calls `/api/v1/e2e.fetchMyKeys`.

- If Rocket.Chat already has `public_key` and `private_key`, the plugin attempts to decrypt the private key using the configured local E2E password.
- If Rocket.Chat has no E2E identity for the bot user, startup fails; set the identity/key in Rocket.Chat first.
- If Rocket.Chat has keys but the configured password cannot decrypt them, startup fails; update/fix the configured key in Rocket.Chat or config. Hermes must not auto-reset or force-publish replacement identity material.

Room keys are also read-only. If a room key is missing, Hermes reports that it needs to be set/shared in Rocket.Chat first. Do not call room-key request, queue, accept/reject, reset, create, or suggested-key distribution endpoints from Hermes.

## User-facing E2E controls

One-shot mode:

1. User sends `e2e1` in a DM.
2. Hermes uses the existing room key if already available and replies with a plaintext ready message; otherwise it tells the user to set/share the key in Rocket.Chat first.
3. User sends one encrypted message.
4. Hermes decrypts, answers encrypted, then disables room E2E again.

Persistent mode:

1. User sends `e2e_on` in a plaintext DM.
2. Hermes prepares E2E and replies with persistent-ready.
3. User can continue chatting across multiple encrypted turns.
4. To turn persistent mode off, user sends encrypted `e2e_off`.

Status: send `e2e_status` before encryption or encrypted `e2e_status` during persistent mode.

Important Rocket.Chat behavior: slash aliases such as `/e2e` are accepted for compatibility, but they may trigger Rocket.Chat parser warnings, and slash commands can be blocked while a room is encrypted. Prefer deliberate non-slash controls: `e2e1`, `e2e_on`, `e2e_off`, `e2e_status`.

## Development rules

- Use REST `POST /api/v1/method.call/<method>` for handler-initiated Rocket.Chat Meteor method calls. Do not await DDP method responses from inside the DDP inbound receive loop; that can deadlock because the same receive loop must read the response frame.
- Do not execute `/e2e` or similar plugin-local slash command side effects from backfilled messages. Backfill messages should be marked and command side effects skipped.
- When changing E2E behavior, update `README.md`, `HANDOFF.md`, tests, plugin version, and release package notes.
- Build handoff archives from a git tag with `git archive`, not by copying a dirty working tree.
- Exclude `.git/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.env`, and any secrets paths from packages. A normal `.gitignore` file is fine.

## Verification commands

Use the Hermes source venv if the system Python lacks pytest:

```bash
/Users/mark/.hermes/hermes-agent/venv/bin/python -m py_compile adapter.py e2e.py
/Users/mark/.hermes/hermes-agent/venv/bin/python -m pytest test_adapter.py -q -o 'addopts='
```

Expected result for v0.3.4:

```text
48 passed
```

For a recipient install, verify from the extracted archive, not just the source tree.
