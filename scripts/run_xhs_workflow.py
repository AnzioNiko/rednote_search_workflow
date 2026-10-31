#!/usr/bin/env python3
"""按 input 配置调用 OpenCLI，保存小红书关键词与账号监控的原始结果。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.secure_credentials import safe_child_environment  # noqa: E402
from scripts.aggregate_results import normalize_note_filter  # noqa: E402
from scripts.note_dedup import SeenNoteStore, normalize_seen_file_name, note_dedup_key  # noqa: E402

DEFAULT_INPUT_DIR = ROOT / "input"
DEFAULT_OUTPUT_DIR = ROOT / "output"
UTC_PLUS_8 = timezone(timedelta(hours=8))
MINIMUM_OPENCLI_VERSION = (1, 8, 8)
NOTE_ID_PATTERN = re.compile(r"/(?:search_result|explore|note)/([0-9a-f]{24})(?:/|$)", re.IGNORECASE)
DETAIL_RETRY_DELAYS_SECONDS = (3, 8)

SORT_OPTIONS = {"comprehensive", "latest", "most-liked", "most-commented", "most-collected"}
NOTE_TYPE_OPTIONS = {"all", "video", "image"}
PUBLISH_TIME_OPTIONS = {"anytime", "day", "week", "half-year"}
SCOPE_OPTIONS = {"all", "seen", "unseen", "following"}
LOCATION_OPTIONS = {"all", "same-city", "nearby"}


class WorkflowError(Exception):
    """表示输入配置或 OpenCLI 环境无法继续执行。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_text(value: datetime | None = None) -> str:
    current = (value or utc_now()).astimezone(UTC_PLUS_8)
    return current.strftime("%Y%m%d_%H%M%S.%f")


def iso_timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"找不到输入文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"JSON 格式错误：{path} 第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}") from exc
    except OSError as exc:
        raise WorkflowError(f"无法读取输入文件：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"输入文件顶层必须是 JSON 对象：{path}")
    return value


def normalize_keywords(payload: dict[str, Any]) -> list[str]:
    values = payload.get("keywords")
    if not isinstance(values, list):
        raise WorkflowError("input/keywords.json 中的 keywords 必须是数组。")
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"关键词列表第 {index + 1} 项必须是非空字符串。")
        keyword = value.strip()
        if keyword not in seen:
            result.append(keyword)
            seen.add(keyword)
    return result


def normalize_users(payload: dict[str, Any]) -> list[dict[str, str]]:
    values = payload.get("users")
    if not isinstance(values, list):
        raise WorkflowError("input/xiaohongshu_users.json 中的 users 必须是数组。")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if isinstance(value, str):
            reference = value.strip()
            name = reference
        elif isinstance(value, dict):
            reference = str(value.get("profile_url") or value.get("url") or value.get("id") or "").strip()
            name = str(value.get("name") or reference).strip()
        else:
            raise WorkflowError(f"监控用户列表第 {index + 1} 项必须是字符串或对象。")
        if not reference:
            raise WorkflowError(f"监控用户列表第 {index + 1} 项缺少 profile_url、url 或 id。")
        if reference not in seen:
            result.append({"name": name, "reference": reference})
            seen.add(reference)
    return result


def require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkflowError(f"筛选配置 {label} 必须是 {minimum} 到 {maximum} 之间的整数。")
    return value


def require_choice(value: Any, label: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        choices_text = ", ".join(sorted(choices))
        raise WorkflowError(f"筛选配置 {label} 无效，可选值：{choices_text}。")
    return value


def normalize_filters(payload: dict[str, Any]) -> dict[str, Any]:
    search = payload.get("search", {})
    users = payload.get("users", {})
    details = payload.get("details", {})
    csv_settings = payload.get("csv", {})
    deduplication = payload.get("deduplication", {})
    if not all(isinstance(section, dict) for section in (search, users, details, csv_settings, deduplication)):
        raise WorkflowError("filters.json 中的 search、users、details、csv、deduplication 必须是 JSON 对象。")

    search_limit = require_int(
        search.get("limit", details.get("limit_per_keyword", 10)), "search.limit", 1, 100
    )
    user_limit = require_int(users.get("limit", 10), "users.limit", 1, 100)
    user_detail_limit = require_int(
        details.get("limit_per_user", user_limit), "details.limit_per_user", 1, 100
    )
    timeout_seconds = require_int(payload.get("timeout_seconds", 180), "timeout_seconds", 10, 900)
    include_creator_detail = details.get("include_creator_note_detail", False)
    if type(include_creator_detail) is not bool:
        raise WorkflowError("筛选配置 details.include_creator_note_detail 必须是布尔值。")
    try:
        note_filter = normalize_note_filter(csv_settings.get("note_filter"))
    except ValueError as exc:
        raise WorkflowError(f"筛选配置 csv.note_filter 无效：{exc}") from exc
    dedup_enabled = deduplication.get("enabled", False)
    if type(dedup_enabled) is not bool:
        raise WorkflowError("筛选配置 deduplication.enabled 必须是布尔值。")
    try:
        seen_file = normalize_seen_file_name(deduplication.get("seen_file"))
    except ValueError as exc:
        raise WorkflowError(f"筛选配置 deduplication.seen_file 无效：{exc}") from exc

    return {
        "search": {
            "limit": search_limit,
            "sort": require_choice(search.get("sort", "most-liked"), "search.sort", SORT_OPTIONS),
            "note_type": require_choice(search.get("note_type", "all"), "search.note_type", NOTE_TYPE_OPTIONS),
            "publish_time": require_choice(
                search.get("publish_time", "anytime"), "search.publish_time", PUBLISH_TIME_OPTIONS
            ),
            "scope": require_choice(search.get("scope", "all"), "search.scope", SCOPE_OPTIONS),
            "location": require_choice(search.get("location", "all"), "search.location", LOCATION_OPTIONS),
        },
        "users": {"limit": user_limit},
        "details": {
            "limit_per_user": user_detail_limit,
            "include_creator_note_detail": include_creator_detail,
        },
        "csv": {"note_filter": note_filter},
        "deduplication": {"enabled": dedup_enabled, "seen_file": seen_file},
        "timeout_seconds": timeout_seconds,
    }


def resolve_opencli_launcher(configured_path: str | None) -> tuple[list[str], str]:
    """解析 OpenCLI；Windows 批处理入口改为直接调用 Node，避免 Shell 注入。"""
    candidate_text = configured_path or os.environ.get("OPENCLI_BIN")
    if candidate_text:
        candidate = Path(candidate_text).expanduser()
        if not candidate.is_absolute() and not candidate.exists():
            located = shutil.which(candidate_text)
            if located:
                candidate = Path(located)
    elif os.name == "nt":
        located = shutil.which("opencli")
        if located:
            candidate = Path(located)
            if candidate.suffix == "":
                command_shim = candidate.with_suffix(".CMD")
                if command_shim.is_file():
                    candidate = command_shim
        else:
            app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            candidate = app_data / "npm" / "opencli.cmd"
    else:
        located = shutil.which("opencli")
        if not located:
            raise WorkflowError("找不到 OpenCLI。请安装 OpenCLI，或通过 OPENCLI_BIN 指定可执行文件路径。")
        candidate = Path(located)

    if not candidate.is_file():
        raise WorkflowError(
            "找不到 OpenCLI。请安装 OpenCLI，或通过 OPENCLI_BIN 指定可执行文件路径。"
        )

    if candidate.suffix.lower() in {".cmd", ".bat"}:
        try:
            shim_text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise WorkflowError(f"无法读取 OpenCLI 启动脚本：{candidate}：{exc}") from exc
        match = re.search(
            r'"%(?:~?dp0)%?\\([^"\r\n]*\\@jackwener\\opencli\\dist\\src\\main\.js)"',
            shim_text,
            re.IGNORECASE,
        )
        if not match:
            if "opencli-app.exe" in shim_text.lower():
                raise WorkflowError(
                    "当前 opencli 命令指向 OpenCLI App 的旧版 shim。请将 PATH 调整为优先命中 pnpm/npm 的 OpenCLI 启动脚本，"
                    "或通过 OPENCLI_BIN 指定该脚本。"
                )
            raise WorkflowError(
                f"无法安全解析该批处理入口：{candidate}。请将 OPENCLI_BIN 指向 pnpm/npm 安装的 opencli.cmd。"
            )
        node_path = shutil.which("node.exe") or shutil.which("node")
        if not node_path:
            raise WorkflowError("找不到 Node.js；请安装 Node.js，或将 node 加入 PATH。")
        entry_path = (candidate.parent / match.group(1).replace("\\", os.sep)).resolve()
        if not entry_path.is_file():
            raise WorkflowError(f"OpenCLI 程序入口不存在：{entry_path}")
        return [node_path, str(entry_path)], str(candidate)

    return [str(candidate)], str(candidate)


def decode_process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class OpenCLIClient:
    def __init__(
        self,
        launcher: list[str],
        display_path: str,
        timeout_seconds: int,
        profile: str | None = None,
    ) -> None:
        self.launcher = launcher
        self.display_path = display_path
        self.timeout_seconds = timeout_seconds
        profile_value = profile if profile is not None else os.environ.get("OPENCLI_PROFILE", "")
        self.profile = profile_value.strip()

    def invoke(self, arguments: list[str], parse_json: bool = True) -> dict[str, Any]:
        command = [*self.launcher]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(arguments)
        record: dict[str, Any] = {
            "executable": self.display_path,
            "arguments": arguments,
            "status": "failed",
            "exit_code": None,
            "raw_stdout": "",
            "raw_stderr": "",
        }
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                env=safe_child_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            record.update(
                {
                    "status": "timeout",
                    "raw_stdout": decode_process_text(exc.stdout),
                    "raw_stderr": decode_process_text(exc.stderr),
                    "error": f"命令超过 {self.timeout_seconds} 秒时限。",
                }
            )
            return record
        except OSError as exc:
            record.update({"status": "failed", "error": f"无法启动 OpenCLI：{exc}"})
            return record

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        record.update(
            {
                "exit_code": completed.returncode,
                "raw_stdout": stdout,
                "raw_stderr": stderr,
                "status": "success" if completed.returncode == 0 else "failed",
            }
        )
        if parse_json and stdout.strip():
            try:
                record["return_value"] = json.loads(stdout)
            except json.JSONDecodeError as exc:
                record["status"] = "invalid_json" if completed.returncode == 0 else "failed"
                record["json_parse_error"] = f"OpenCLI 输出不是有效 JSON：{exc.msg}。"
        elif parse_json:
            record["status"] = "empty_output" if completed.returncode == 0 else "failed"
            record["json_parse_error"] = "OpenCLI 没有返回 JSON 内容。"
        return record


def extract_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = record.get("return_value")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("items", "rows", "results", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def note_id_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = NOTE_ID_PATTERN.search(path)
    return match.group(1).lower() if match else None


def add_command_issue(
    run_data: dict[str, Any],
    record: dict[str, Any],
    stage: str,
    source: str,
    optional: bool = False,
) -> None:
    if record.get("status") == "success":
        return
    stderr_text = record.get("raw_stderr", "").strip()
    command_failed = record.get("exit_code") not in (None, 0)
    message = record.get("error")
    returned_value = record.get("return_value")
    returned_error = returned_value.get("error") if isinstance(returned_value, dict) else None
    if not message and isinstance(returned_error, dict):
        error_code = str(returned_error.get("code") or "").strip()
        error_message = str(returned_error.get("message") or "").strip()
        message = f"{error_code}: {error_message}" if error_code and error_message else error_message or error_code
    elif not message and isinstance(returned_error, str):
        message = returned_error.strip()
    if not message and command_failed and stderr_text:
        message = stderr_text
    if not message:
        message = record.get("json_parse_error") or stderr_text
    issue = {
        "stage": stage,
        "source": source,
        "status": record.get("status"),
        "message": message,
        "json_parse_error": record.get("json_parse_error"),
    }
    (run_data["warnings"] if optional else run_data["errors"]).append(issue)


def build_search_arguments(keyword: str, filters: dict[str, Any]) -> list[str]:
    search = filters["search"]
    return [
        "xiaohongshu",
        "search",
        keyword,
        "--limit",
        str(search["limit"]),
        "--sort",
        search["sort"],
        "--note-type",
        search["note_type"],
        "--publish-time",
        search["publish_time"],
        "--scope",
        search["scope"],
        "--location",
        search["location"],
        "-f",
        "json",
    ]


def public_detail_retry_reason(record: dict[str, Any]) -> str | None:
    status = str(record.get("status") or "")
    if status == "success":
        fields = {
            str(item.get("field") or "").strip().lower(): item.get("value")
            for item in extract_rows(record)
        }
        content = fields.get("content", fields.get("description", fields.get("desc", "")))
        return "正文为空" if content is None or not str(content).strip() else None

    if status in {"timeout", "empty_output", "invalid_json"}:
        return status

    error_text = "\n".join(
        str(record.get(key) or "") for key in ("error", "raw_stderr", "json_parse_error")
    ).lower()
    if status == "failed" and "navigation rejected" in error_text:
        return "导航被拒绝"
    return None


def invoke_public_detail_with_retry(client: OpenCLIClient, url: str) -> dict[str, Any]:
    retry_history: list[dict[str, Any]] = []
    total_attempts = len(DETAIL_RETRY_DELAYS_SECONDS) + 1

    for attempt in range(1, total_attempts + 1):
        record = client.invoke(["xiaohongshu", "note", url, "-f", "json"])
        retry_reason = public_detail_retry_reason(record)
        if retry_reason is None or attempt == total_attempts:
            record["attempt_count"] = attempt
            if retry_history:
                record["retry_history"] = retry_history
            if retry_reason == "正文为空":
                record["content_missing_after_retries"] = True
            return record

        retry_history.append(
            {
                "attempt": attempt,
                "status": record.get("status"),
                "exit_code": record.get("exit_code"),
                "reason": retry_reason,
                "raw_stderr": record.get("raw_stderr", ""),
                "error": record.get("error"),
                "json_parse_error": record.get("json_parse_error"),
            }
        )
        time.sleep(DETAIL_RETRY_DELAYS_SECONDS[attempt - 1])

    raise AssertionError("详情重试循环意外结束。")


def enrich_note_rows(
    client: OpenCLIClient,
    rows: list[dict[str, Any]],
    limit: int,
    run_data: dict[str, Any],
    source: str,
    include_creator_detail: bool,
    dedup_store: SeenNoteStore | None = None,
    dedup_stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:limit], start=1):
        url_value = row.get("url") or row.get("note_url") or ""
        url = str(url_value).strip()
        if dedup_store is not None:
            key = note_dedup_key(row, url)
            if key is None:
                if dedup_stats is not None:
                    dedup_stats["unkeyed_count"] += 1
            elif not dedup_store.record(key):
                if dedup_stats is not None:
                    dedup_stats["duplicate_count"] += 1
                continue
            elif dedup_stats is not None:
                dedup_stats["recorded_count"] += 1
        note_id = note_id_from_url(url) if url else None
        note_result: dict[str, Any] = {
            "source_item": row,
            "note_id": note_id,
            "url": url,
            "public_detail": None,
            "creator_note_detail": None,
        }
        if not url:
            note_result["detail_status"] = "skipped_no_url"
            run_data["warnings"].append(
                {"stage": "public_note_detail", "source": source, "item": index, "message": "结果行没有笔记 URL。"}
            )
            enriched.append(note_result)
            continue

        public_detail = invoke_public_detail_with_retry(client, url)
        note_result["public_detail"] = public_detail
        note_result["detail_status"] = public_detail.get("status")
        add_command_issue(run_data, public_detail, "public_note_detail", source)
        if public_detail.get("content_missing_after_retries"):
            run_data["warnings"].append(
                {
                    "stage": "public_note_detail",
                    "source": source,
                    "item": index,
                    "message": "详情命令成功，但连续重试后正文仍为空。",
                }
            )

        if include_creator_detail:
            if note_id:
                creator_detail = client.invoke(
                    ["xiaohongshu", "creator-note-detail", note_id, "-f", "json"]
                )
                note_result["creator_note_detail"] = creator_detail
                add_command_issue(
                    run_data,
                    creator_detail,
                    "creator_note_detail",
                    source,
                    optional=True,
                )
            else:
                note_result["creator_note_detail"] = {
                    "status": "skipped_no_note_id",
                    "message": "无法从笔记 URL 解析 24 位笔记 ID。",
                }
                run_data["warnings"].append(
                    {"stage": "creator_note_detail", "source": source, "item": index, "message": "无法解析笔记 ID。"}
                )
        enriched.append(note_result)
    return enriched


def is_browser_environment_ready(record: dict[str, Any]) -> bool:
    if record.get("status") != "success":
        return False

    raw_stdout = record.get("raw_stdout", "")
    if not isinstance(raw_stdout, str):
        raw_stdout = str(raw_stdout)
    try:
        doctor_payload = json.loads(raw_stdout.strip())
    except json.JSONDecodeError:
        doctor_payload = None

    if isinstance(doctor_payload, dict):
        data = doctor_payload.get("data")
        if isinstance(data, dict) and any(
            key in data for key in ("daemonRunning", "extensionConnected", "connectivity")
        ):
            connectivity = data.get("connectivity")
            return (
                doctor_payload.get("ok") is True
                and data.get("daemonRunning") is True
                and data.get("extensionConnected") is True
                and isinstance(connectivity, dict)
                and connectivity.get("ok") is True
            )

    output = raw_stdout + chr(10) + str(record.get("raw_stderr", ""))
    return (
        "[FAIL]" not in output
        and "Daemon: running" in output
        and "Extension: connected" in output
        and "Connectivity: connected" in output
    )


def check_doctor(client: OpenCLIClient) -> dict[str, Any]:
    record = client.invoke(["doctor"], parse_json=False)
    record["browser_ready"] = is_browser_environment_ready(record)
    if not record["browser_ready"] and not record.get("error"):
        record["error"] = "OpenCLI Doctor 未确认浏览器桥接、扩展和连接均可用。"
    return record


def parse_version(record: dict[str, Any]) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", record.get("raw_stdout", ""))
    return tuple(map(int, match.groups())) if match else None


def save_run(output_dir: Path, run_data: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"xhs_run_{run_data['run_id']}.json"
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(run_data, ensure_ascii=False, indent=2)
    temporary_path.write_text(serialized + "\n", encoding="utf-8")
    temporary_path.replace(path)
    return path


def run_workflow(
    input_dir: Path,
    output_dir: Path,
    configured_opencli: str | None,
    include_media_analysis: bool = True,
    generate_csv: bool = True,
    selected_keywords: list[str] | None = None,
    selected_users: list[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    keyword_payload = read_json_object(input_dir / "keywords.json")
    user_payload = read_json_object(input_dir / "xiaohongshu_users.json")
    filter_payload = read_json_object(input_dir / "filters.json")
    from scripts.media_analysis import DEFAULT_LLM_SETTINGS, get_configured_secret, normalize_llm_settings, process_media_for_run

    if include_media_analysis:
        llm_settings_path = input_dir / "llm_settings.json"
        try:
            llm_payload = read_json_object(llm_settings_path) if llm_settings_path.exists() else DEFAULT_LLM_SETTINGS
            llm_settings = normalize_llm_settings(llm_payload)
            if llm_settings["enabled"]:
                get_configured_secret(llm_settings)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowError(f"LLM 媒体分析配置错误：{exc}") from exc
    else:
        llm_settings = normalize_llm_settings(DEFAULT_LLM_SETTINGS)
    keywords = normalize_keywords(keyword_payload)
    users = normalize_users(user_payload)
    if selected_keywords is not None:
        selected_keyword_set = set(selected_keywords)
        keywords = [keyword for keyword in keywords if keyword in selected_keyword_set]
    if selected_users is not None:
        selected_user_set = set(selected_users)
        users = [
            user for user in users
            if user["reference"] in selected_user_set or user["name"] in selected_user_set
        ]
    filters = normalize_filters(filter_payload)
    if not keywords and not users:
        raise WorkflowError("关键词和监控用户均为空。请先编辑 input/keywords.json 或 input/xiaohongshu_users.json。")

    dedup_settings = filters["deduplication"]
    dedup_stats = {
        "recorded_count": 0,
        "duplicate_count": 0,
        "unkeyed_count": 0,
    }
    dedup_store: SeenNoteStore | None = None
    if dedup_settings["enabled"]:
        try:
            dedup_store = SeenNoteStore(input_dir / dedup_settings["seen_file"])
        except ValueError as exc:
            raise WorkflowError(f"去重记录加载失败：{exc}") from exc

    started = utc_now()
    run_data: dict[str, Any] = {
        "schema_version": 1,
        "run_id": timestamp_text(started),
        "started_at": iso_timestamp(started),
        "finished_at": None,
        "status": "running",
        "opencli": {"executable": None, "version": None, "version_check": None, "doctor": None},
        "inputs": {
            "keywords": keywords,
            "users": users,
            "filters": filters,
            "llm_settings": llm_settings,
        },
        "keyword_searches": [],
        "user_searches": [],
        "media_commands": [],
        "deduplication": {
            **dedup_settings,
            **dedup_stats,
            "seen_count": len(dedup_store.keys) if dedup_store is not None else 0,
            "invalid_line_count": dedup_store.invalid_line_count if dedup_store is not None else 0,
        },
        "errors": [],
        "warnings": [],
    }

    try:
        launcher, display_path = resolve_opencli_launcher(configured_opencli)
        client = OpenCLIClient(launcher, display_path, filters["timeout_seconds"])
        run_data["opencli"]["executable"] = display_path

        version_record = client.invoke(["--version"], parse_json=False)
        run_data["opencli"]["version_check"] = version_record
        version = parse_version(version_record)
        if version:
            run_data["opencli"]["version"] = ".".join(map(str, version))
        if version_record.get("status") != "success" or version is None:
            raise WorkflowError("无法读取 OpenCLI 版本。请检查 OPENCLI_BIN 配置。")
        if version < MINIMUM_OPENCLI_VERSION:
            raise WorkflowError(
                "小红书筛选参数需要 OpenCLI 1.8.8 或更新版本；"
                f"当前解析到 {'.'.join(map(str, version))}。"
            )

        doctor = check_doctor(client)
        run_data["opencli"]["doctor"] = doctor
        if not doctor["browser_ready"]:
            raise WorkflowError(
                "OpenCLI 浏览器环境未就绪。请先运行 opencli doctor，并确保 Chrome、扩展和登录态可用。"
            )

        details = filters["details"]
        for keyword in keywords:
            record = client.invoke(build_search_arguments(keyword, filters))
            search_entry: dict[str, Any] = {
                "keyword": keyword,
                "search": record,
                "notes": [],
            }
            run_data["keyword_searches"].append(search_entry)
            add_command_issue(run_data, record, "keyword_search", keyword)
            if record.get("status") == "success":
                search_entry["notes"] = enrich_note_rows(
                    client,
                    extract_rows(record),
                    filters["search"]["limit"],
                    run_data,
                    f"keyword:{keyword}",
                    details["include_creator_note_detail"],
                    dedup_store,
                    dedup_stats,
                )

        for user in users:
            record = client.invoke(
                ["xiaohongshu", "user", user["reference"], "--limit", str(filters["users"]["limit"]), "-f", "json"]
            )
            user_entry: dict[str, Any] = {
                "name": user["name"],
                "reference": user["reference"],
                "search": record,
                "notes": [],
            }
            run_data["user_searches"].append(user_entry)
            add_command_issue(run_data, record, "user_search", user["name"])
            if record.get("status") == "success":
                user_entry["notes"] = enrich_note_rows(
                    client,
                    extract_rows(record),
                    filters["details"]["limit_per_user"],
                    run_data,
                    f"user:{user['name']}",
                    include_creator_detail=False,
                    dedup_store=dedup_store,
                    dedup_stats=dedup_stats,
                )

        if include_media_analysis and llm_settings["enabled"]:
            process_media_for_run(run_data, output_dir, llm_settings, lambda arguments: client.invoke(arguments))

    except WorkflowError as exc:
        run_data["errors"].append({"stage": "preflight", "message": str(exc)})
    except Exception as exc:  # 保障已有原始采集结果仍能落盘
        run_data["errors"].append({"stage": "workflow", "message": f"未预期错误：{type(exc).__name__}: {exc}"})

    if dedup_store is not None:
        try:
            dedup_store.save()
        except (OSError, ValueError) as exc:
            run_data["warnings"].append({
                "stage": "deduplication",
                "message": f"去重记录保存失败：{type(exc).__name__}: {exc}",
            })
        run_data["deduplication"].update({
            **dedup_stats,
            "seen_count": len(dedup_store.keys),
            "invalid_line_count": dedup_store.invalid_line_count,
        })
        if dedup_store.invalid_line_count:
            run_data["warnings"].append({
                "stage": "deduplication",
                "message": f"去重记录中有 {dedup_store.invalid_line_count} 行格式无效，已忽略。",
            })

    finished = utc_now()
    run_data["finished_at"] = iso_timestamp(finished)
    preflight_records = [
        run_data["opencli"].get("version_check"),
        run_data["opencli"].get("doctor"),
    ]
    operation_records: list[dict[str, Any]] = []
    for entry in run_data["keyword_searches"] + run_data["user_searches"]:
        if isinstance(entry.get("search"), dict):
            operation_records.append(entry["search"])
        for note in entry.get("notes", []):
            operation_records.extend(
                item for item in (note.get("public_detail"), note.get("creator_note_detail"))
                if isinstance(item, dict)
            )
    operation_records.extend(item for item in run_data.get("media_commands", []) if isinstance(item, dict))
    successful_operations = sum(1 for item in operation_records if item.get("status") == "success")
    failed_operations = sum(1 for item in operation_records if item.get("status") != "success")
    successful_preflight = sum(
        1 for item in preflight_records if isinstance(item, dict) and item.get("status") == "success"
    )
    run_data["command_summary"] = {
        "preflight_successful": successful_preflight,
        "successful": successful_operations,
        "failed": failed_operations,
    }
    if run_data["errors"]:
        run_data["status"] = "partial" if successful_operations else "failed"
    elif run_data["warnings"] or failed_operations:
        run_data["status"] = "partial"
    else:
        run_data["status"] = "succeeded"

    saved_path = save_run(output_dir, run_data)
    if generate_csv:
        try:
            from scripts.aggregate_results import aggregate_result_file

            csv_path, summary_rows = aggregate_result_file(saved_path)
            run_data["summary_csv"] = csv_path.name
            run_data["summary_note_count"] = len(summary_rows)
        except Exception as exc:
            run_data["warnings"].append({"stage": "aggregation", "message": f"CSV 整合失败：{type(exc).__name__}: {exc}"})
            if run_data["status"] == "succeeded":
                run_data["status"] = "partial"
    save_run(output_dir, run_data)
    return saved_path, run_data


def main() -> int:
    parser = argparse.ArgumentParser(description="按 input 配置执行小红书关键词与监控用户采集。")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR, help="输入 JSON 文件所在目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="时间戳结果文件保存目录")
    parser.add_argument("--opencli-bin", help="OpenCLI 可执行文件路径；也可设置 OPENCLI_BIN 环境变量")
    parser.add_argument("--skip-media-analysis", action="store_true", help="只采集笔记，不执行媒体与 LLM 分析")
    parser.add_argument("--skip-aggregation", action="store_true", help="只保存采集 JSON，不生成汇总 CSV")
    parser.add_argument("--only-keyword", action="append", help="只采集指定关键词；可重复传入")
    parser.add_argument("--only-user", action="append", help="只采集指定账号名称或主页标识；可重复传入")
    parser.add_argument("--disable-keywords", action="store_true", help="本次不采集关键词")
    parser.add_argument("--disable-users", action="store_true", help="本次不采集监控账号")
    args = parser.parse_args()
    try:
        output_path, run_data = run_workflow(
            args.input_dir,
            args.output_dir,
            args.opencli_bin,
            include_media_analysis=not args.skip_media_analysis,
            generate_csv=not args.skip_aggregation,
            selected_keywords=[] if args.disable_keywords else args.only_keyword,
            selected_users=[] if args.disable_users else args.only_user,
        )
    except WorkflowError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"文件操作失败：{exc}", file=sys.stderr)
        return 2

    first_error = run_data["errors"][0] if run_data["errors"] else None
    first_warning = run_data["warnings"][0] if run_data["warnings"] else None
    try:
        output_reference = str(output_path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        output_reference = str(output_path)
    print(
        json.dumps(
            {
                "status": run_data["status"],
                "run_id": run_data["run_id"],
                "opencli_version": run_data["opencli"].get("version"),
                "output_file": output_reference,
                "summary_csv": run_data.get("summary_csv"),
                "summary_note_count": run_data.get("summary_note_count", 0),
                "keyword_count": len(run_data["keyword_searches"]),
                "user_count": len(run_data["user_searches"]),
                "deduplication": run_data.get("deduplication"),
                "error_count": len(run_data["errors"]),
                "warning_count": len(run_data["warnings"]),
                "first_error": first_error.get("message") if first_error else None,
                "first_warning": first_warning.get("message") if first_warning else None,
            },
            ensure_ascii=True,
        )
    )
    return 0 if run_data["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
