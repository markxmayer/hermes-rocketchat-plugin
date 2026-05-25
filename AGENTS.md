# Agent instructions for the Hermes Rocket.Chat plugin

This repository is an update-safe Hermes user plugin for Rocket.Chat gateway support. Treat it as shared code that may be installed by another Hermes user or bot operator.

## Non-negotiable safety rules

- Never print, commit, package, or ask the user to paste Rocket.Chat auth tokens, user IDs paired with tokens, E2E passwords, recovery phrases, private keys, room keys, or `.env` contents.
- Do not copy Mark's E2E password file or key material to Jake or any other operator. Each Rocket.Chat bot/user must create or use its own E2E recovery password and keypair.
- Do not reset or force-replace a bot user's Rocket.Chat E2E identity unless the operator explicitly approves the risk. Resetting the identity can make older encrypted messages unreadable to that bot user.
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

## How the plugin creates/manages its E2E secret

When E2E is enabled, adapter startup calls `load_or_create_e2e_password(...)` from `e2e.py`.

Behavior:

1. If an explicit password is configured, use it.
2. Else if `ROCKETCHAT_E2E_PASSWORD` is set, use it.
3. Else if `ROCKETCHAT_E2E_PASSWORD_FILE` or configured `password_file` exists, read one of these formats:
   - `ROCKETCHAT_E2E_PASSWORD=<secret>`
   - `E2E_PASSWORD=<secret>`
   - `PASSWORD=<secret>`
   - a bare one-line secret
4. Else generate a new high-entropy password and write it to the password file, defaulting to:
   `~/.hermes/secrets/rocketchat-e2e.env`

Generated files are created with owner-only permissions:

- parent directory: best effort `0700`
- password file: `0600`

The generated file contains:

```bash
# Local Rocket.Chat E2E recovery password for Hermes. Do not commit or share.
ROCKETCHAT_E2E_PASSWORD=<generated-secret>
```

Do not manually generate RSA keys for normal setup. The plugin uses the recovery password to unwrap an existing Rocket.Chat private key or, if the bot user has no E2E identity, generates a new RSA-OAEP keypair and publishes it via Rocket.Chat's `e2e.setUserPublicAndPrivateKeys` endpoint.

## Identity and recovery workflow

On startup, `RocketChatE2E.start()` calls `/api/v1/e2e.fetchMyKeys`.

- If Rocket.Chat already has `public_key` and `private_key`, the plugin attempts to decrypt the private key using the configured local E2E password.
- If Rocket.Chat has no E2E identity for the bot user, the plugin generates a fresh RSA keypair, encrypts the private key using the local E2E password, and publishes the pair with `/api/v1/e2e.setUserPublicAndPrivateKeys`.
- If Rocket.Chat has keys but the configured password cannot decrypt them, startup should fail by default. Do not auto-reset. Only if the operator explicitly opts in with `force_unreadable_identity` / `ROCKETCHAT_E2E_FORCE_UNREADABLE_IDENTITY=true` may the plugin force-publish a replacement identity.

If key sharing for a room appears stale, prefer the built-in room-key request and queue helpers before identity reset:

- request room key via `e2ekeyRequest` / room key polling
- queue the bot for room keys by force-publishing the same existing public/private key material
- use stale room-key rotation only for the affected room flow, not as a blanket identity reset

## User-facing E2E controls

One-shot mode:

1. User sends `e2e1` in a DM.
2. Hermes prepares the room key and replies with a plaintext ready message.
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

Expected result for v0.3.3:

```text
47 passed
```

For a recipient install, verify from the extracted archive, not just the source tree.
