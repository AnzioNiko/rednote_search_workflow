"""浏览器到本机服务的混合加密密钥传输；私钥仅驻留进程内存。"""

from __future__ import annotations

import base64
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.secure_credentials import store_secret, validate_credential_id

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY_ID = secrets.token_urlsafe(18)
MAX_SECRET_BYTES = 5120


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _int_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def public_key_payload() -> dict[str, Any]:
    numbers = _PRIVATE_KEY.public_key().public_numbers()
    return {
        "key_id": _KEY_ID,
        "public_key": {
            "kty": "RSA",
            "n": _int_b64url(numbers.n),
            "e": _int_b64url(numbers.e),
            "alg": "RSA-OAEP-256",
            "ext": True,
            "key_ops": ["encrypt"],
        },
    }


def _decode_b64url(value: Any, label: str, max_length: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > max_length or not all(ch.isalnum() or ch in "-_" for ch in value):
        raise ValueError(f"加密密钥请求中的 {label} 格式无效。")
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError(f"加密密钥请求中的 {label} 格式无效。") from None


def decrypt_and_store(payload: Any) -> str:
    allowed_fields = {"credential_id", "key_id", "wrapped_key", "nonce", "ciphertext"}
    if not isinstance(payload, dict) or set(payload) != allowed_fields:
        raise ValueError("密钥请求字段无效；服务端只接受加密后的密钥载荷。")
    credential_id = validate_credential_id(payload["credential_id"])
    if not isinstance(payload["key_id"], str) or not secrets.compare_digest(payload["key_id"], _KEY_ID):
        raise ValueError("加密会话已过期，请重新提交密钥。")

    wrapped_key = _decode_b64url(payload["wrapped_key"], "wrapped_key", 1024)
    nonce = _decode_b64url(payload["nonce"], "nonce", 32)
    ciphertext = _decode_b64url(payload["ciphertext"], "ciphertext", 16 * 1024)
    if len(nonce) != 12 or not ciphertext or len(wrapped_key) != 256:
        raise ValueError("加密密钥载荷长度无效。")

    aad = credential_id.encode("utf-8")
    try:
        aes_key = _PRIVATE_KEY.decrypt(
            wrapped_key,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        if len(aes_key) != 32:
            raise ValueError("加密密钥载荷格式无效。")
        secret_bytes = AESGCM(aes_key).decrypt(nonce, ciphertext, aad)
        if not secret_bytes or len(secret_bytes) > MAX_SECRET_BYTES:
            raise ValueError("API 密钥为空或超出安全存储限制。")
        secret = secret_bytes.decode("utf-8")
        if not secret.strip() or "\x00" in secret:
            raise ValueError("API 密钥格式无效。")
    except ValueError:
        raise
    except Exception:
        raise ValueError("无法解密密钥载荷；请刷新页面后重试。") from None

    store_secret(credential_id, secret)
    return credential_id
