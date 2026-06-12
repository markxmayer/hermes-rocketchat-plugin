"""Minimal Rocket.Chat E2EE helpers for Hermes' Rocket.Chat adapter.

This intentionally implements a conservative DM-oriented subset of Rocket.Chat's
client-side E2EE protocol.  It never logs key material.  It supports current
rc.v2 AES-GCM message content and legacy private-key wrapping well enough to
load the Hermes user's key and decrypt room session keys stored on subscriptions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


_B64_RSA_ENCODED_LEN = 344
_RSA_CIPHERTEXT_LEN = 256


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_uint(text: str) -> int:
    return int.from_bytes(_b64url_decode(text), "big")


def _binary_decode(text: str) -> bytes:
    # Rocket.Chat's Binary.decode maps each JS charCode (0-255) to one byte.
    return bytes(ord(ch) for ch in text)


def _binary_encode(data: bytes) -> str:
    return "".join(chr(b) for b in data)


def _json_dumps_compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _load_secret_file(path: str) -> str:
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"ROCKETCHAT_E2E_PASSWORD", "E2E_PASSWORD", "PASSWORD"}:
                return value.strip().strip('"').strip("'")
        return line
    return ""


DEFAULT_E2E_PASSWORD_FILE = "~/.hermes/secrets/rocketchat-e2e.env"


def load_e2e_password(*, explicit: str = "", env_var: str = "ROCKETCHAT_E2E_PASSWORD", file_path: str = "") -> str:
    if explicit:
        return explicit
    if os.getenv(env_var):
        return os.environ[env_var]
    path = file_path or os.getenv("ROCKETCHAT_E2E_PASSWORD_FILE", "")
    if path and Path(path).expanduser().exists():
        return _load_secret_file(path)
    return ""


def generate_passphrase() -> str:
    # Test/helper-only: production Hermes code is read-only for Rocket.Chat E2E
    # secrets and must not call this to create recovery material.
    return "hermes-" + secrets.token_urlsafe(32)


def load_required_e2e_password(
    *,
    explicit: str = "",
    env_var: str = "ROCKETCHAT_E2E_PASSWORD",
    file_path: str = "",
    default_file_path: str = DEFAULT_E2E_PASSWORD_FILE,
) -> tuple[str, str]:
    """Load the E2E recovery password without creating key material.

    Returns (password, path). Hermes is read-only for Rocket.Chat E2E keys and
    recovery secrets; missing configuration is an error, never a create path.
    """
    password = load_e2e_password(explicit=explicit, env_var=env_var, file_path=file_path)
    if password:
        return password, file_path or os.getenv("ROCKETCHAT_E2E_PASSWORD_FILE", "")
    path_text = file_path or os.getenv("ROCKETCHAT_E2E_PASSWORD_FILE", "") or default_file_path
    raise RuntimeError(f"Rocket.Chat E2E recovery key is not configured; set it first in {path_text}")


def decode_private_key(encrypted_private_key: str, password: str, user_id: str) -> str:
    stored = json.loads(encrypted_private_key)
    if "$binary" in stored:
        blob = _b64decode(stored["$binary"])
        iv, ciphertext = blob[:16], blob[16:]
        salt = _binary_decode(user_id)
        iterations = 1000
        algorithm = "AES-CBC"
    else:
        iv = _b64decode(stored["iv"])
        ciphertext = _b64decode(stored["ciphertext"])
        salt = _binary_decode(stored["salt"])
        iterations = int(stored.get("iterations") or 100000)
        algorithm = "AES-GCM" if len(iv) == 12 else "AES-CBC"

    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(_binary_decode(password))
    if algorithm == "AES-GCM":
        plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    else:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad_len = padded[-1]
        plaintext = padded[:-pad_len]
    return _binary_encode(plaintext)


def encode_private_key(private_key_json: str, password: str, user_id: str) -> str:
    # Test/helper-only. Production E2E startup reads the existing Rocket.Chat
    # identity and never writes or replaces private-key material.
    salt = f"v2:{user_id}:{uuid.uuid4()}"
    iterations = 100000
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_binary_decode(salt),
        iterations=iterations,
    ).derive(_binary_decode(password))
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, _binary_decode(private_key_json), None)
    return _json_dumps_compact({"iv": _b64encode(iv), "ciphertext": _b64encode(ciphertext), "salt": salt, "iterations": iterations})


def private_key_from_jwk(private_jwk: dict[str, Any]) -> rsa.RSAPrivateKey:
    numbers = rsa.RSAPrivateNumbers(
        p=_b64url_uint(private_jwk["p"]),
        q=_b64url_uint(private_jwk["q"]),
        d=_b64url_uint(private_jwk["d"]),
        dmp1=_b64url_uint(private_jwk["dp"]),
        dmq1=_b64url_uint(private_jwk["dq"]),
        iqmp=_b64url_uint(private_jwk["qi"]),
        public_numbers=rsa.RSAPublicNumbers(e=_b64url_uint(private_jwk["e"]), n=_b64url_uint(private_jwk["n"])),
    )
    return numbers.private_key()


def public_key_from_jwk(public_jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    return rsa.RSAPublicNumbers(e=_b64url_uint(public_jwk["e"]), n=_b64url_uint(public_jwk["n"])).public_key()


def generate_rsa_jwks() -> tuple[str, str]:
    # Test/helper-only. Production Hermes code must not generate Rocket.Chat E2E
    # identity keys.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    private = key.private_numbers()

    def enc_int(value: int) -> str:
        data = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    public_jwk = {
        "alg": "RSA-OAEP-256",
        "e": "AQAB",
        "ext": True,
        "key_ops": ["encrypt"],
        "kty": "RSA",
        "n": enc_int(public.n),
    }
    private_jwk = {
        "alg": "RSA-OAEP-256",
        "d": enc_int(private.d),
        "dp": enc_int(private.dmp1),
        "dq": enc_int(private.dmq1),
        "e": "AQAB",
        "ext": True,
        "key_ops": ["decrypt"],
        "kty": "RSA",
        "n": enc_int(public.n),
        "p": enc_int(private.p),
        "q": enc_int(private.q),
        "qi": enc_int(private.iqmp),
    }
    return _json_dumps_compact(public_jwk), _json_dumps_compact(private_jwk)


def split_prefixed_base64(value: str) -> tuple[str, bytes]:
    if len(value) < _B64_RSA_ENCODED_LEN:
        raise ValueError("invalid prefixed base64 value")
    prefix = value[:-_B64_RSA_ENCODED_LEN]
    encrypted = _b64decode(value[-_B64_RSA_ENCODED_LEN:])
    if len(encrypted) != _RSA_CIPHERTEXT_LEN:
        raise ValueError("invalid RSA ciphertext length")
    return prefix, encrypted


def join_prefixed_base64(prefix: str, encrypted: bytes) -> str:
    if len(encrypted) != _RSA_CIPHERTEXT_LEN:
        raise ValueError("invalid RSA ciphertext length")
    encoded = _b64encode(encrypted)
    if len(encoded) != _B64_RSA_ENCODED_LEN:
        raise ValueError("invalid encoded RSA ciphertext length")
    return prefix + encoded


def decrypt_session_key(group_key: str, private_key: rsa.RSAPrivateKey) -> tuple[str, str, bytes]:
    kid, encrypted = split_prefixed_base64(group_key)
    plaintext = private_key.decrypt(
        encrypted,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    session_key_json = _binary_encode(plaintext)
    jwk = json.loads(session_key_json)
    key_bytes = _b64url_decode(jwk["k"])
    return kid, session_key_json, key_bytes


def encrypt_session_key_for_public(session_key_json: str, kid: str, public_key_json: str) -> str:
    pub = public_key_from_jwk(json.loads(public_key_json))
    encrypted = pub.encrypt(
        _binary_decode(session_key_json),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return join_prefixed_base64(kid, encrypted)


def generate_session_key() -> tuple[str, str, bytes]:
    # Test/helper-only. Production Hermes code must not create or publish room
    # session keys.
    key = os.urandom(32)
    kid = str(uuid.uuid4())
    jwk = {
        "alg": "A256GCM",
        "ext": True,
        "k": base64.urlsafe_b64encode(key).decode("ascii").rstrip("="),
        "key_ops": ["encrypt", "decrypt"],
        "kty": "oct",
    }
    return kid, _json_dumps_compact(jwk), key


def decrypt_message_content(content: Any, session_key: bytes) -> dict[str, Any]:
    if isinstance(content, str):
        # Legacy format: key-id + base64(iv[16] + ciphertext), AES-CBC.
        payload = content[12:]
        blob = _b64decode(payload)
        iv, ciphertext = blob[:16], blob[16:]
        decryptor = Cipher(algorithms.AES(session_key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad_len = padded[-1]
        plaintext = padded[:-pad_len].decode("utf-8")
        data = json.loads(plaintext)
        if "text" in data and "msg" not in data:
            data["msg"] = data.pop("text")
        return data

    if not isinstance(content, dict):
        raise TypeError("unsupported encrypted content")
    if content.get("algorithm") != "rc.v2.aes-sha2":
        raise ValueError("unsupported encrypted content algorithm")
    iv = _b64decode(content["iv"])
    ciphertext = _b64decode(content["ciphertext"])
    plaintext = AESGCM(session_key).decrypt(iv, ciphertext, None).decode("utf-8")
    data = json.loads(plaintext)
    if "text" in data and "msg" not in data:
        data["msg"] = data.pop("text")
    return data


def _decrypt_file_content_metadata(content: Any, session_key: bytes) -> dict[str, Any]:
    """Decrypt Rocket.Chat's per-file E2EE metadata with the room key."""
    data = decrypt_message_content(content, session_key)
    if isinstance(data.get("attachments"), list) and data["attachments"] and isinstance(data["attachments"][0], dict):
        return data["attachments"][0]
    return data if isinstance(data, dict) else {}


def decrypt_file_bytes(encryption: dict[str, Any], body: bytes) -> tuple[bytes, str]:
    """Decrypt Rocket.Chat E2EE file bytes using decrypted file metadata.

    Rocket.Chat encrypts the uploaded file itself with an ephemeral AES-CTR key.
    That key/iv/type/hash metadata is then encrypted with the room session key and
    stored in the file object's ``content`` field.  Callers must pass the already
    decrypted metadata here.
    """
    key = encryption.get("key") if isinstance(encryption, dict) else None
    if not isinstance(key, dict):
        raise ValueError("missing encrypted file key metadata")
    if str(key.get("kty") or "") != "oct":
        raise ValueError("unsupported encrypted file key type")
    raw_key = _b64url_decode(str(key.get("k") or ""))
    iv = _b64decode(str(encryption.get("iv") or ""))
    if len(raw_key) not in {16, 24, 32}:
        raise ValueError("unsupported encrypted file AES key length")
    if len(iv) != 16:
        raise ValueError("unsupported encrypted file iv length")
    decryptor = Cipher(algorithms.AES(raw_key), modes.CTR(iv)).decryptor()
    plaintext = decryptor.update(body) + decryptor.finalize()
    expected_hash = str(encryption.get("hash") or "").strip().lower()
    if expected_hash:
        actual_hash = hashlib.sha256(plaintext).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError("encrypted file hash mismatch")
    return plaintext, str(encryption.get("type") or "").strip().lower()


def encrypt_message_content(msg: str, session_key: bytes, kid: str, *, attachments: Optional[list[Any]] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"msg": msg}
    if attachments:
        payload["attachments"] = attachments
    plaintext = _json_dumps_compact(payload).encode("utf-8")
    iv = os.urandom(12)
    ciphertext = AESGCM(session_key).encrypt(iv, plaintext, None)
    return {
        "algorithm": "rc.v2.aes-sha2",
        "kid": kid,
        "iv": _b64encode(iv),
        "ciphertext": _b64encode(ciphertext),
    }


@dataclass
class RoomE2EState:
    rid: str
    kid: str
    session_key_json: str
    session_key: bytes


class RocketChatE2E:
    def __init__(
        self,
        *,
        user_id: str,
        password: str,
        rest_get: Any,
        rest_post: Any,
        ddp_call: Any = None,
        force_unreadable_identity: bool = False,
    ) -> None:
        self.user_id = user_id
        self.password = password
        self._rest_get = rest_get
        self._rest_post = rest_post
        self._ddp_call = ddp_call
        self.force_unreadable_identity = force_unreadable_identity
        self.public_key_json = ""
        self.private_key_json = ""
        self.encrypted_private_key_json = ""
        self.private_key: Optional[rsa.RSAPrivateKey] = None
        self.rooms: dict[str, RoomE2EState] = {}

    async def start(self) -> None:
        keys = await self._rest_get("/api/v1/e2e.fetchMyKeys")
        public_key = str(keys.get("public_key") or "")
        encrypted_private_key = str(keys.get("private_key") or "")
        if not public_key or not encrypted_private_key:
            raise RuntimeError(
                "Rocket.Chat E2E identity is not set for this user. Set the E2E recovery key/identity in a Rocket.Chat client first; Hermes will only read existing keys."
            )
        try:
            self.private_key_json = decode_private_key(encrypted_private_key, self.password, self.user_id)
        except Exception as exc:
            raise RuntimeError(
                "Rocket.Chat E2E identity exists but could not be decrypted with the configured recovery key. Update the configured key or reset it in Rocket.Chat; Hermes will not replace it."
            ) from exc
        self.public_key_json = public_key
        self.encrypted_private_key_json = encrypted_private_key
        self.private_key = private_key_from_jwk(json.loads(self.private_key_json))
        # Read-only key policy: do not trigger Rocket.Chat key request/share flows
        # from Hermes startup. Existing subscription room keys are imported later
        # if Rocket.Chat has already exposed them to this user.

    def have_room(self, rid: str) -> bool:
        return rid in self.rooms

    async def accept_suggested_key(self, rid: str, suggested_key: str) -> bool:
        if not suggested_key:
            return False
        try:
            ok = self.import_room_key(rid, suggested_key)
        except Exception:
            ok = False
        if ok:
            # Hermes is read-only for E2E key management. Use the suggested key
            # locally if Rocket.Chat already supplied it, but do not accept/reject
            # or otherwise mutate key state on the server.
            return True
        return False

    def import_room_key(self, rid: str, group_key: str) -> bool:
        if not self.private_key or not group_key:
            return False
        kid, session_key_json, session_key = decrypt_session_key(group_key, self.private_key)
        self.rooms[rid] = RoomE2EState(rid=rid, kid=kid, session_key_json=session_key_json, session_key=session_key)
        return True

    async def _method_call(self, method: str, params: list[Any] | None = None) -> Any:
        """Call Rocket.Chat Meteor methods over REST to avoid DDP receive-loop deadlocks.

        Inbound Rocket.Chat messages are handled inside the DDP websocket receive loop.
        Awaiting a DDP method call from that same handler blocks the loop from reading
        the result frame and eventually times out. REST method.call is slower but safe
        from both inbound handlers and background tasks.
        """
        data = await self._rest_post(
            f"/api/v1/method.call/{method}",
            {"message": _json_dumps_compact({"msg": "method", "method": method, "params": params or []})},
        )
        if not isinstance(data, dict):
            return None
        message = data.get("message")
        if isinstance(message, str) and message:
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    return parsed.get("result")
            except Exception:
                return None
        return data.get("result")

    async def create_room_key(self, rid: str) -> RoomE2EState:
        raise RuntimeError("Hermes E2E key management is read-only; create the room key in Rocket.Chat")

    async def reset_room_key(self, rid: str) -> RoomE2EState:
        """Rotate an existing encrypted room to a key generated by this client.

        Rocket.Chat can leave a stale e2eKeyId on a room after E2E is disabled.
        Re-enabling then resurrects a key that this bot never received. The official
        reset endpoint moves old participant keys to oldRoomKeys, stores the new
        key on this user's subscription, and queues other users for suggested-key
        distribution.
        """
        raise RuntimeError("Hermes E2E key management is read-only; rotate room keys in Rocket.Chat")

    async def request_subscription_keys(self) -> bool:
        return False

    async def queue_me_for_room_keys(self) -> bool:
        """Do not repost user identity/key material from Hermes."""
        return False

    async def request_room_key(self, rid: str, key_id: str) -> bool:
        return False

    async def distribute_room_key(self, rid: str) -> int:
        """Do not share/modify room keys from Hermes."""
        return 0

    def decrypt_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(msg.get("rid") or "")
        state = self.rooms.get(rid)
        if not state:
            raise RuntimeError("missing E2E room key")
        content = msg.get("content") if isinstance(msg.get("content"), dict) else msg.get("msg")
        data = decrypt_message_content(content, state.session_key)
        for file_meta in [data.get("file") if isinstance(data.get("file"), dict) else None, *(data.get("files") or [])]:
            if not isinstance(file_meta, dict) or not isinstance(file_meta.get("content"), dict):
                continue
            try:
                file_meta["encryption"] = _decrypt_file_content_metadata(file_meta["content"], state.session_key)
            except Exception:
                # Keep message text/attachments usable if a specific file's
                # metadata cannot be decrypted.  Callers will skip that file.
                file_meta["encryption_error"] = "metadata-decrypt-failed"
        out = dict(msg)
        out.update(data)
        out["e2e"] = "done"
        return out

    def decrypt_file_bytes(self, encryption: dict[str, Any], body: bytes) -> tuple[bytes, str]:
        return decrypt_file_bytes(encryption, body)

    def encrypt_message_payload(self, rid: str, text: str) -> dict[str, Any]:
        state = self.rooms.get(rid)
        if not state:
            raise RuntimeError("missing E2E room key")
        return {
            "rid": rid,
            "content": encrypt_message_content(text, state.session_key, state.kid),
            "t": "e2e",
            "e2e": "pending",
        }
