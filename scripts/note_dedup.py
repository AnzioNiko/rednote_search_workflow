"""跨采集运行的小红书笔记去重键与紧凑记录文件。"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NOTE_ID_PATTERN = re.compile(r"/(?:search_result|explore|note)/([0-9a-f]{24})(?:/|$)", re.IGNORECASE)
NOTE_ID_FIELD_PATTERN = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)
SEEN_KEY_PATTERN = re.compile(r"^(?:i:[0-9a-f]{24}|u:[A-Za-z0-9_-]{22})$")
DEFAULT_SEEN_FILE = "seen_notes.txt"
SEEN_FILE_HEADER = "# xhs-seen-notes-v1"


def normalize_seen_file_name(value: Any) -> str:
    """只允许 input 目录下的单个文本文件名。"""
    name = DEFAULT_SEEN_FILE if value in (None, "") else str(value).strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name)
        or name in {".", ".."}
        or not name.lower().endswith(".txt")
    ):
        raise ValueError("去重记录文件名必须是 input 目录下的单个 .txt 文件名。")
    return name


def _note_id_candidate(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if NOTE_ID_FIELD_PATTERN.fullmatch(text) else None


def _note_id_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = NOTE_ID_PATTERN.search(path)
    return match.group(1).lower() if match else None


def _canonical_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    path = parsed.path.rstrip("/") or "/"
    if not host:
        return path if path != "/" else None
    return f"{host}{path}"


def _fallback_key(namespace: str, value: str) -> str:
    digest = hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).digest()[:16]
    return "u:" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def note_dedup_key(row: dict[str, Any], url: str = "") -> str | None:
    """生成稳定且紧凑的笔记键，优先使用小红书 24 位笔记 ID。"""
    source_item = row.get("source_item")
    if not isinstance(source_item, dict):
        source_item = {}
    for candidate in (
        row.get("note_id"),
        source_item.get("note_id"),
        row.get("id"),
        source_item.get("id"),
        _note_id_from_url(url or str(row.get("url") or row.get("note_url") or "")),
    ):
        note_id = _note_id_candidate(candidate)
        if note_id:
            return f"i:{note_id}"

    fallback_url = url or str(row.get("url") or row.get("note_url") or "")
    canonical = _canonical_url(fallback_url) if fallback_url else None
    return _fallback_key("url", canonical) if canonical else None


class SeenNoteStore:
    """加载、查询并原子重写已验证的紧凑去重键。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: set[str] = set()
        self.new_keys: set[str] = set()
        self.invalid_line_count = 0
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise ValueError(f"无法读取去重记录文件：{exc}") from exc
        for line in lines:
            key = line.strip()
            if not key or key.startswith("#"):
                continue
            if SEEN_KEY_PATTERN.fullmatch(key):
                self.keys.add(key)
            else:
                self.invalid_line_count += 1

    def record(self, key: str) -> bool:
        """记录首次出现的键；已存在时返回 False。"""
        if not SEEN_KEY_PATTERN.fullmatch(key):
            raise ValueError("去重键格式无效。")
        if key in self.keys:
            return False
        self.keys.add(key)
        self.new_keys.add(key)
        return True

    def save(self) -> bool:
        """重写为排序后的紧凑文本；没有新键时不写磁盘。"""
        if not self.new_keys:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join([SEEN_FILE_HEADER, *sorted(self.keys)]) + "\n"
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(self.path)
        self.new_keys.clear()
        return True
