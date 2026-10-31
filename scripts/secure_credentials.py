"""使用 Windows Credential Manager 安全保存本机工作流密钥。"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes

SERVICE_PREFIX = "SocialTrendWorkflow:LLM:"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
CREDENTIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def safe_child_environment() -> dict[str, str]:
    """避免把常见模型 API 密钥环境变量转交给 OpenCLI 或媒体工具子进程。"""
    environment = os.environ.copy()
    secret_markers = (
        "API_KEY", "APIKEY", "API_TOKEN", "LLM_KEY", "LLM_TOKEN",
        "OPENAI_KEY", "OPENAI_TOKEN", "GEMINI_KEY", "GEMINI_TOKEN",
        "GOOGLE_API_KEY", "ANTHROPIC_KEY", "ANTHROPIC_TOKEN",
        "MISTRAL_KEY", "OPENROUTER_KEY", "AZURE_OPENAI_KEY",
    )
    for name in list(environment):
        if any(marker in name.upper() for marker in secret_markers):
            environment.pop(name, None)
    return environment


def validate_credential_id(credential_id: str) -> str:
    value = str(credential_id).strip()
    if not CREDENTIAL_ID_PATTERN.fullmatch(value):
        raise ValueError("密钥凭据标识只能包含英文字母、数字、点、下划线和连字符，长度为 1 到 64。")
    return value


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("当前版本使用 Windows Credential Manager；此系统暂不支持安全密钥存储。")


def _advapi32():
    _require_windows()
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    library.CredReadW.restype = wintypes.BOOL
    library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    library.CredDeleteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def _target_name(credential_id: str) -> str:
    return SERVICE_PREFIX + validate_credential_id(credential_id)


def store_secret(credential_id: str, secret: str) -> None:
    if not secret or not secret.strip():
        raise ValueError("API 密钥不能为空。")
    secret_bytes = secret.encode("utf-8")
    if len(secret_bytes) > 5120:
        raise ValueError("API 密钥超出 Windows Credential Manager 的通用凭据大小限制。")
    library = _advapi32()
    blob = ctypes.create_string_buffer(secret_bytes)
    credential = CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = _target_name(credential_id)
    credential.Comment = "Social Trend Workflow LLM credential"
    credential.CredentialBlobSize = len(secret_bytes)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = "llm-api-key"
    if not library.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def read_secret(credential_id: str) -> str | None:
    library = _advapi32()
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not library.CredReadW(_target_name(credential_id), CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error_code = ctypes.get_last_error()
        if error_code == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(error_code)
    try:
        credential = pointer.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return raw.decode("utf-8")
    finally:
        library.CredFree(ctypes.cast(pointer, ctypes.c_void_p))


def delete_secret(credential_id: str) -> bool:
    library = _advapi32()
    if library.CredDeleteW(_target_name(credential_id), CRED_TYPE_GENERIC, 0):
        return True
    error_code = ctypes.get_last_error()
    if error_code == ERROR_NOT_FOUND:
        return False
    raise ctypes.WinError(error_code)


def secret_exists(credential_id: str) -> bool:
    return read_secret(credential_id) is not None
