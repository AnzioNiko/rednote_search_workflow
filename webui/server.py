#!/usr/bin/env python3
"""本地小红书工作流 WebUI 服务，仅监听回环地址。"""

from __future__ import annotations

import csv
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
KEYWORDS_PATH = INPUT_DIR / "keywords.json"
USERS_PATH = INPUT_DIR / "xiaohongshu_users.json"
FILTERS_PATH = INPUT_DIR / "filters.json"
APP_SETTINGS_PATH = INPUT_DIR / "app_settings.json"
LLM_SETTINGS_PATH = INPUT_DIR / "llm_settings.json"
WORKFLOWS_PATH = INPUT_DIR / "workflows.json"
RUN_SCRIPT = ROOT / "scripts" / "run_xhs_workflow.py"
INDEX_PATH = Path(__file__).resolve().parent / "index.html"
DEFAULT_APP_SETTINGS = {"opencli_bin": "", "opencli_profile": ""}
CSRF_TOKEN = secrets.token_urlsafe(32)
MAX_BODY_BYTES = 512 * 1024
MAX_PREVIEW_ROWS = 300
RESULT_FILE_PATTERN = re.compile(
    r"^xhs_run_(?:[0-9]{8}T[0-9]{6}\.[0-9]{6}Z|[0-9]{8}_[0-9]{6}\.[0-9]{6}(?:\+0800)?)\.json$"
)
SUMMARY_FILE_PATTERN = re.compile(
    r"^xhs_summary_(?:[0-9]{8}T[0-9]{6}\.[0-9]{6}Z|[0-9]{8}_[0-9]{6}\.[0-9]{6}(?:\+0800)?)\.csv$"
)
CONFIG_LOCK = threading.RLock()
JOB_LOCK = threading.RLock()
WORKFLOW_LOCK = threading.RLock()
JOB_STATE: dict[str, Any] = {
    "job_id": None,
    "status": "idle",
    "operation": None,
    "origin": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "summary": None,
    "log_tail": "",
    "error": None,
    "workflow_active": False,
    "dependencies_updating": False,
}

sys.path.insert(0, str(ROOT))
from scripts.aggregate_results import (  # noqa: E402
    aggregate_result_file,
    field_value,
    flatten_fields,
    merge_result_files,
    normalize_note_filter,
    plain_summary_text,
    safe_csv_cell,
)
from scripts.filter_results import filter_result_file  # noqa: E402
from scripts.media_analysis import (  # noqa: E402
    DEFAULT_LLM_SETTINGS,
    analyze_note_media,
    get_available_models,
    get_configured_secret,
    normalize_llm_settings,
    test_llm_connection,
)
try:
    from scripts.key_transport import decrypt_and_store, public_key_payload  # noqa: E402
except ImportError as exc:
    decrypt_and_store = None
    public_key_payload = None
    KEY_TRANSPORT_IMPORT_ERROR = str(exc)
else:
    KEY_TRANSPORT_IMPORT_ERROR = None
from scripts.secure_credentials import safe_child_environment, secret_exists, validate_credential_id  # noqa: E402
from scripts.run_xhs_workflow import (  # noqa: E402
    OpenCLIClient,
    WorkflowError,
    is_browser_environment_ready,
    normalize_filters,
    normalize_keywords,
    normalize_users,
    resolve_opencli_launcher,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def job_is_active() -> bool:
    return (
        JOB_STATE.get("status") in {"queued", "running"}
        or bool(JOB_STATE.get("workflow_active"))
        or bool(JOB_STATE.get("dependencies_updating"))
    )


def read_object(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists() and default is not None:
        return default.copy()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise WorkflowError(f"找不到配置文件：{path}")
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"配置 JSON 格式错误：{path.name} 第 {exc.lineno} 行，第 {exc.colno} 列。")
    if not isinstance(value, dict):
        raise WorkflowError(f"配置文件顶层必须是 JSON 对象：{path.name}")
    return value


def validate_app_settings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WorkflowError("app_settings.json 必须是 JSON 对象。")
    opencli_bin = value.get("opencli_bin", "")
    opencli_profile = value.get("opencli_profile", "")
    if not isinstance(opencli_bin, str) or len(opencli_bin) > 1000:
        raise WorkflowError("OpenCLI 路径必须是 1000 字符以内的文本。")
    if not isinstance(opencli_profile, str) or len(opencli_profile) > 200:
        raise WorkflowError("浏览器 Profile 名称必须是 200 字符以内的文本。")
    return {"opencli_bin": opencli_bin.strip(), "opencli_profile": opencli_profile.strip()}


def load_config_state() -> dict[str, Any]:
    with CONFIG_LOCK:
        keyword_source = read_object(KEYWORDS_PATH)
        user_source = read_object(USERS_PATH)
        filter_source = read_object(FILTERS_PATH)
        settings_source = read_object(APP_SETTINGS_PATH, DEFAULT_APP_SETTINGS)
        llm_settings_source = read_object(LLM_SETTINGS_PATH, DEFAULT_LLM_SETTINGS)
        keywords = normalize_keywords(keyword_source)
        users = normalize_users(user_source)
        filters = normalize_filters(filter_source)
        settings = validate_app_settings(settings_source)
        llm_settings = normalize_llm_settings(llm_settings_source)
        return {
            "keywords": keywords,
            "users": [{"name": row["name"], "profile_url": row["reference"]} for row in users],
            "filters": filters,
            "app_settings": settings,
            "llm_settings": llm_settings,
        }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def persist_workflow_execution(file_name: str | None, execution: dict[str, Any]) -> str | None:
    """将工作流逐步状态写回对应采集 JSON；写入失败不应覆盖真实任务错误。"""
    if not file_name:
        return None
    try:
        path = result_json_path(file_name)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise WorkflowError("采集 JSON 顶层必须是对象，无法保存工作流执行记录。")
        data["workflow_execution"] = json.loads(json.dumps(execution, ensure_ascii=False))
        atomic_write_json(path, data)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def load_workflows() -> list[dict[str, Any]]:
    with WORKFLOW_LOCK:
        payload = read_object(WORKFLOWS_PATH, {"workflows": []})
        workflows = payload.get("workflows", [])
        if not isinstance(workflows, list):
            raise WorkflowError("workflows.json 中 workflows 必须是数组。")
        return workflows


def validate_workflows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflows"), list):
        raise WorkflowError("工作流内容格式无效。")
    current = load_config_state()
    keyword_values = set(current["keywords"])
    user_values = {row["profile_url"] for row in current["users"]}
    existing = {item.get("id"): item for item in load_workflows() if isinstance(item, dict)}
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if len(payload["workflows"]) > 50:
        raise WorkflowError("最多保存 50 个工作流。")
    for raw in payload["workflows"]:
        if not isinstance(raw, dict):
            raise WorkflowError("工作流必须是对象。")
        workflow_id = raw.get("id")
        name = raw.get("name")
        enabled = raw.get("enabled", False)
        schedule = raw.get("schedule", {})
        tasks = raw.get("tasks", [])
        if not isinstance(workflow_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workflow_id) or workflow_id in seen_ids:
            raise WorkflowError("工作流标识无效或重复。")
        seen_ids.add(workflow_id)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise WorkflowError("工作流名称不能为空且不能超过 100 个字符。")
        if not isinstance(enabled, bool) or not isinstance(schedule, dict):
            raise WorkflowError("工作流启用状态或定时设置无效。")
        frequency = schedule.get("frequency", "daily")
        clock_text = schedule.get("time", "09:00")
        weekday = schedule.get("weekday", 0)
        if frequency not in {"daily", "weekly"} or not isinstance(clock_text, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock_text):
            raise WorkflowError("定时频率或时间格式无效。")
        if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
            raise WorkflowError("每周运行的星期设置无效。")
        if not isinstance(tasks, list) or not tasks or len(tasks) > 30:
            raise WorkflowError("工作流至少需要一个任务，且最多包含 30 个任务。")
        normalized_tasks: list[dict[str, Any]] = []
        prior_json = False
        prior_aggregate = False
        for task in tasks:
            if not isinstance(task, dict):
                raise WorkflowError("任务必须是对象。")
            kind = task.get("type")
            task_id = task.get("id")
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
                raise WorkflowError("任务标识无效。")
            if kind == "scrape":
                keywords = task.get("keywords")
                users = task.get("users")
                if keywords is not None and (not isinstance(keywords, list) or any(item not in keyword_values for item in keywords)):
                    raise WorkflowError("提取任务包含不存在的关键词，请重新选择并保存。")
                if users is not None and (not isinstance(users, list) or any(item not in user_values for item in users)):
                    raise WorkflowError("提取任务包含不存在的账号，请重新选择并保存。")
                chosen_keywords = None if keywords is None else list(dict.fromkeys(keywords))
                chosen_users = None if users is None else list(dict.fromkeys(users))
                effective_keywords = current["keywords"] if chosen_keywords is None else chosen_keywords
                effective_users = user_values if chosen_users is None else chosen_users
                if not effective_keywords and not effective_users:
                    raise WorkflowError("提取任务至少要选择一个关键词或账号。")
                normalized_tasks.append({"id": task_id, "type": kind, "keywords": chosen_keywords, "users": chosen_users})
                prior_json = True
            elif kind == "filter":
                if prior_aggregate:
                    raise WorkflowError("笔记筛选步骤必须放在 CSV 整理之前。")
                source = task.get("source", {})
                if not isinstance(source, dict) or source.get("kind") not in {"workflow", "workflow_all", "file"}:
                    raise WorkflowError("笔记筛选任务必须选择结果来源。")
                if source["kind"] in {"workflow", "workflow_all"}:
                    if not prior_json:
                        raise WorkflowError("笔记筛选引用本工作流生成的文件时，前面必须先有脚本提取或笔记筛选任务。")
                    normalized_source = {"kind": source["kind"]}
                else:
                    file_name = source.get("file")
                    result_json_path(file_name if isinstance(file_name, str) else "")
                    normalized_source = {"kind": "file", "file": file_name}
                try:
                    note_filter = normalize_note_filter(task.get("note_filter"))
                except ValueError as exc:
                    raise WorkflowError(f"笔记筛选任务的条件无效：{exc}") from exc
                if not note_filter["enabled"]:
                    raise WorkflowError("笔记筛选任务需要启用筛选并至少添加一条条件。")
                normalized_tasks.append({
                    "id": task_id,
                    "type": kind,
                    "source": normalized_source,
                    "note_filter": note_filter,
                })
                prior_json = True
            elif kind in {"aggregate", "analyze"}:
                source = task.get("source", {})
                allowed_source_kinds = {"workflow", "file"}
                if kind == "aggregate":
                    allowed_source_kinds.add("workflow_all")
                if not isinstance(source, dict) or source.get("kind") not in allowed_source_kinds:
                    raise WorkflowError("CSV 整理和大模型分析任务必须选择结果来源。")
                if source["kind"] in {"workflow", "workflow_all"}:
                    if kind == "aggregate" and not prior_json:
                        raise WorkflowError("CSV 整理引用本工作流生成的文件时，前面必须先有脚本提取或笔记筛选任务。")
                    if kind == "analyze" and not prior_aggregate:
                        raise WorkflowError("大模型分析引用本工作流生成的文件时，前面必须先有 CSV 整理任务。")
                    normalized_source = {"kind": source["kind"]}
                else:
                    file_name = source.get("file")
                    if kind == "aggregate":
                        result_json_path(file_name if isinstance(file_name, str) else "")
                    else:
                        summary_csv_path(file_name if isinstance(file_name, str) else "")
                    normalized_source = {"kind": "file", "file": file_name}
                normalized_tasks.append({"id": task_id, "type": kind, "source": normalized_source})
                if kind == "aggregate":
                    prior_aggregate = True
            else:
                raise WorkflowError("仅支持脚本提取、笔记筛选、CSV 整理和大模型分析任务。")
        old = existing.get(workflow_id, {})
        result.append({
            "id": workflow_id,
            "name": name.strip(),
            "enabled": enabled,
            "schedule": {"frequency": frequency, "time": clock_text, "weekday": weekday},
            "tasks": normalized_tasks,
            "last_triggered_key": old.get("last_triggered_key"),
        })
    return result


def save_workflows(workflows: list[dict[str, Any]]) -> None:
    with WORKFLOW_LOCK:
        atomic_write_json(WORKFLOWS_PATH, {"workflows": workflows})


def contains_secret_field(value: Any) -> bool:
    forbidden = ("apikey", "token", "secret", "password", "authorization")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(part in normalized for part in forbidden) or contains_secret_field(item):
                return True
    elif isinstance(value, list):
        return any(contains_secret_field(item) for item in value)
    return False


def validate_and_save(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise WorkflowError("请求内容必须是 JSON 对象。")
    if contains_secret_field(payload):
        raise WorkflowError("WebUI 配置接口不接收密钥值；请使用隐藏输入的密钥管理命令。")
    keywords = normalize_keywords({"keywords": payload.get("keywords")})
    users = normalize_users({"users": payload.get("users")})
    filters = normalize_filters(payload.get("filters", {}))
    settings = validate_app_settings(payload.get("app_settings", {}))
    llm_source = payload.get("llm_settings")
    if llm_source is None:
        llm_source = read_object(LLM_SETTINGS_PATH, DEFAULT_LLM_SETTINGS)
    llm_settings = normalize_llm_settings(llm_source)
    saved_users = [{"name": row["name"], "profile_url": row["reference"]} for row in users]
    # 与启动任务使用同一把锁，避免保存配置和开始采集发生竞态。
    with JOB_LOCK:
        if job_is_active():
            raise WorkflowError("采集正在运行，暂时不能修改配置。")
        with CONFIG_LOCK:
            atomic_write_json(KEYWORDS_PATH, {"keywords": keywords})
            atomic_write_json(USERS_PATH, {"users": saved_users})
            atomic_write_json(FILTERS_PATH, filters)
            atomic_write_json(APP_SETTINGS_PATH, settings)
            atomic_write_json(LLM_SETTINGS_PATH, llm_settings)
    return {"keywords": keywords, "users": saved_users, "filters": filters, "app_settings": settings, "llm_settings": llm_settings}



def latest_run_file() -> Path | None:
    if not OUTPUT_DIR.is_dir():
        return None
    files = sorted(OUTPUT_DIR.glob("xhs_run_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def result_json_path(file_name: str) -> Path:
    if not RESULT_FILE_PATTERN.fullmatch(file_name):
        raise WorkflowError("结果文件名无效，只支持 xhs_run_*.json。")
    path = OUTPUT_DIR / file_name
    if not path.is_file():
        raise WorkflowError("指定的采集 JSON 文件不存在。")
    return path


def resolve_workflow_output_file(reference: Any) -> str | None:
    """将采集脚本返回的路径安全地规范成 output 目录内的结果文件名。"""
    if not isinstance(reference, str) or not reference.strip():
        return None

    reference = reference.strip()
    candidate = Path(reference)
    if candidate.is_absolute():
        path = candidate.resolve()
    elif RESULT_FILE_PATTERN.fullmatch(reference):
        path = (OUTPUT_DIR / candidate).resolve()
    else:
        path = (ROOT / candidate).resolve()

    output_dir = OUTPUT_DIR.resolve()
    try:
        path.relative_to(output_dir)
    except ValueError:
        return None
    if path.parent != output_dir or not RESULT_FILE_PATTERN.fullmatch(path.name) or not path.is_file():
        return None
    return path.name


def summary_csv_name(result_name: str) -> str:
    if not RESULT_FILE_PATTERN.fullmatch(result_name):
        raise WorkflowError("结果文件名无效，只支持 xhs_run_*.json。")
    return "xhs_summary_" + result_name.removeprefix("xhs_run_").removesuffix(".json") + ".csv"


def summary_csv_path(file_name: str) -> Path:
    if not SUMMARY_FILE_PATTERN.fullmatch(file_name):
        raise WorkflowError("CSV 文件名无效，只支持 xhs_summary_*.csv。")
    path = OUTPUT_DIR / file_name
    if not path.is_file() or path.parent.resolve() != OUTPUT_DIR.resolve():
        raise WorkflowError("指定的 CSV 文件不存在。")
    return path


def result_name_for_summary_csv(file_name: str) -> str:
    summary_csv_path(file_name)
    return "xhs_run_" + file_name.removeprefix("xhs_summary_").removesuffix(".csv") + ".json"


def canonical_note_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.netloc:
        return text
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def note_source_url(note: dict[str, Any]) -> str:
    source_item = note.get("source_item") if isinstance(note.get("source_item"), dict) else {}
    fields = flatten_fields(source_item)
    for key in ("public_detail", "creator_note_detail"):
        record = note.get(key)
        if isinstance(record, dict):
            fields.update(flatten_fields(record.get("return_value")))
    return str(note.get("url") or field_value(fields, "url") or source_item.get("url") or "").strip()


def notes_by_canonical_url(run_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    lookup: dict[str, list[dict[str, Any]]] = {}
    for collection_name in ("keyword_searches", "user_searches"):
        entries = run_data.get(collection_name, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            notes = entry.get("notes", [])
            if not isinstance(notes, list):
                continue
            for note in notes:
                if not isinstance(note, dict):
                    continue
                key = canonical_note_url(note_source_url(note))
                if key:
                    lookup.setdefault(key, []).append(note)
    return lookup


def analysis_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def apply_analysis_to_csv_row(row: dict[str, str], result: dict[str, Any]) -> None:
    row["媒体摘要"] = plain_summary_text(result.get("summary"))
    row["画面文字"] = analysis_value(result.get("visible_text"))
    row["视频关键点"] = analysis_value(result.get("key_moments"))
    row["音频转写"] = analysis_value(result.get("audio_transcript"))
    row["媒体解析状态"] = analysis_value(result.get("status"))


def write_summary_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({
            column: safe_csv_cell(
                plain_summary_text(row.get(column))
                if column == "媒体摘要"
                else str(row.get(column) or "")
            )
            for column in fieldnames
        } for row in rows)
    temporary_path.replace(path)


def opencli_chrome_payload(client: OpenCLIClient, method: str, arguments: list[Any]) -> dict[str, Any] | None:
    record = client.invoke([
        "browser", "chrome", "call", method,
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    ])
    if record.get("status") != "success":
        return None
    payload = record.get("return_value")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return payload


def opencli_chrome_result(client: OpenCLIClient, method: str, arguments: list[Any]) -> Any:
    payload = opencli_chrome_payload(client, method, arguments)
    if payload is None:
        return None
    return payload.get("data")


def close_opencli_blank_tabs(client: OpenCLIClient) -> int:
    windows = opencli_chrome_result(client, "windows.getAll", [{"populate": True}])
    if not isinstance(windows, list):
        return 0
    closed = 0
    for window in windows:
        if not isinstance(window, dict):
            continue
        tabs = window.get("tabs")
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict) or tab.get("url") != "about:blank":
                continue
            tab_id = tab.get("id")
            if not isinstance(tab_id, int):
                continue
            if opencli_chrome_payload(client, "tabs.remove", [tab_id]) is not None:
                closed += 1
    return closed


def read_aggregation_preview(result_name: str) -> dict[str, Any]:
    source_path = result_json_path(result_name)
    run_data = json.loads(source_path.read_text(encoding="utf-8-sig"))
    if isinstance(run_data, dict) and run_data.get("csv_stale"):
        raise WorkflowError("LLM 分析已更新此 JSON；请重新执行 CSV 整合后查看。")
    csv_name = summary_csv_name(result_name)
    csv_path = OUTPUT_DIR / csv_name
    if not csv_path.is_file() or not SUMMARY_FILE_PATTERN.fullmatch(csv_name):
        raise WorkflowError("此采集文件尚未生成 CSV，请先点击“整合所选文件”。")
    rows: list[dict[str, str]] = []
    row_count = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        for row in reader:
            row_count += 1
            if len(rows) < MAX_PREVIEW_ROWS:
                rows.append({key: value or "" for key, value in row.items() if key is not None})
    return {
        "source_file": source_path.name,
        "csv_file": csv_name,
        "columns": columns,
        "row_count": row_count,
        "preview_limit": MAX_PREVIEW_ROWS,
        "rows": rows,
    }


def list_result_files() -> list[dict[str, Any]]:
    if not OUTPUT_DIR.is_dir():
        return []
    files = sorted(
        (path for path in OUTPUT_DIR.glob("xhs_run_*.json") if RESULT_FILE_PATTERN.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:100]
    results: list[dict[str, Any]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        csv_name = summary_csv_name(path.name)
        csv_exists = (OUTPUT_DIR / csv_name).is_file()
        csv_stale = bool(data.get("csv_stale"))
        results.append({
            "file": path.name,
            "status": data.get("status", "未知"),
            "operation": data.get("operation"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "note_count": data.get("summary_note_count"),
            "filter_source": data.get("filter_source"),
            "csv_file": csv_name if csv_exists and not csv_stale else None,
            "csv_status": "已整合" if csv_exists and not csv_stale else "需重整" if csv_exists and csv_stale else "待整合",
            "csv_stale": csv_stale,
        })
    return results


def list_summary_csv_files() -> list[dict[str, Any]]:
    if not OUTPUT_DIR.is_dir():
        return []
    files = sorted(
        (path for path in OUTPUT_DIR.glob("xhs_summary_*.csv") if SUMMARY_FILE_PATTERN.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    results: list[dict[str, Any]] = []
    for path in files:
        source_name = "xhs_run_" + path.name.removeprefix("xhs_summary_").removesuffix(".csv") + ".json"
        source_path = OUTPUT_DIR / source_name
        source_status = None
        stale = False
        csv_status = "未关联"
        if source_path.is_file():
            source_valid = False
            try:
                data = json.loads(source_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                data = {}
            else:
                source_valid = isinstance(data, dict)
            if source_valid:
                stale = bool(data.get("csv_stale"))
                source_status = data.get("status")
                csv_status = "需重整" if stale else "已整合"
            else:
                csv_status = "源文件异常"
        results.append({
            "file": path.name,
            "source_file": source_name if source_path.is_file() else None,
            "source_status": source_status,
            "csv_status": csv_status,
            "stale": stale,
        })
    return results


def read_csv_preview(file_name: str) -> dict[str, Any]:
    path = summary_csv_path(file_name)
    rows: list[dict[str, str]] = []
    row_count = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames or []
            for row in reader:
                row_count += 1
                if len(rows) < MAX_PREVIEW_ROWS:
                    normalized = {key: value or "" for key, value in row.items() if key is not None}
                    if "媒体摘要" in normalized:
                        normalized["媒体摘要"] = plain_summary_text(normalized["媒体摘要"])
                    rows.append(normalized)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise WorkflowError(f"无法读取 CSV 文件：{exc}") from exc
    return {
        "csv_file": path.name,
        "columns": columns,
        "row_count": row_count,
        "preview_limit": MAX_PREVIEW_ROWS,
        "rows": rows,
    }


def opencli_error_detail(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    returned_value = record.get("return_value")
    if not isinstance(returned_value, dict):
        raw_stdout = record.get("raw_stdout")
        if isinstance(raw_stdout, str) and raw_stdout.strip():
            try:
                returned_value = json.loads(raw_stdout)
            except json.JSONDecodeError:
                returned_value = None
    if not isinstance(returned_value, dict):
        return None
    error = returned_value.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        detail = f"{code}: {message}" if code and message else message or code
        return detail or None
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def latest_run_issue(data: dict[str, Any], collection: str) -> str | None:
    issues = data.get(collection, [])
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict) and str(issue.get("message") or "").strip():
                return str(issue["message"]).strip()
    if collection == "errors":
        for group in ("keyword_searches", "user_searches"):
            entries = data.get(group, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for record in entry.values():
                    if isinstance(record, dict) and record.get("status") != "success":
                        detail = opencli_error_detail(record)
                        if detail:
                            return detail
    return None


def latest_run_summary() -> dict[str, Any] | None:
    path = latest_run_file()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    csv_name = summary_csv_name(path.name)
    return {
        "job_id": data.get("run_id"),
        "status": data.get("status"),
        "operation": data.get("operation"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "output_file": path.name,
        "summary_csv": csv_name if (OUTPUT_DIR / csv_name).is_file() and not data.get("csv_stale") else None,
        "summary_note_count": data.get("summary_note_count"),
        "input_note_count": (data.get("filter_source") or {}).get("input_note_count"),
        "output_note_count": (data.get("filter_source") or {}).get("output_note_count"),
        "command_summary": data.get("command_summary"),
        "error_count": len(data.get("errors", [])),
        "warning_count": len(data.get("warnings", [])),
        "first_error": latest_run_issue(data, "errors"),
        "first_warning": latest_run_issue(data, "warnings"),
    }


def get_job_state() -> dict[str, Any]:
    with JOB_LOCK:
        state = dict(JOB_STATE)
    if state.get("workflow_active"):
        state["status"] = "running"
        state["operation"] = "workflow"
    if state.get("status") != "idle":
        state["latest_run"] = latest_run_summary()
        return state
    return {
        "job_id": None,
        "status": "idle",
        "operation": None,
        "origin": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "summary": None,
        "log_tail": "",
        "error": None,
        "latest_run": latest_run_summary(),
    }


def run_job(
    job_id: str,
    settings: dict[str, str],
    selected_keywords: list[str] | None = None,
    selected_users: list[str] | None = None,
) -> None:
    with JOB_LOCK:
        JOB_STATE.update({"status": "running", "operation": "web_scrape", "started_at": now_iso(), "error": None})
    command = [
        sys.executable,
        str(RUN_SCRIPT),
        "--input-dir",
        str(INPUT_DIR),
        "--output-dir",
        str(OUTPUT_DIR),
        "--skip-media-analysis",
        "--skip-aggregation",
    ]
    if selected_keywords is not None:
        if selected_keywords:
            for keyword in selected_keywords:
                command.extend(["--only-keyword", keyword])
        else:
            command.append("--disable-keywords")
    if selected_users is not None:
        if selected_users:
            for user in selected_users:
                command.extend(["--only-user", user])
        else:
            command.append("--disable-users")
    if settings.get("opencli_bin"):
        command.extend(["--opencli-bin", settings["opencli_bin"]])
    environment = safe_child_environment()
    if settings.get("opencli_profile"):
        environment["OPENCLI_PROFILE"] = settings["opencli_profile"]
    else:
        environment.pop("OPENCLI_PROFILE", None)
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        output, _ = process.communicate()
        lines = [line for line in (output or "").splitlines() if line.strip()]
        summary: dict[str, Any] | None = None
        if lines:
            try:
                candidate = json.loads(lines[-1])
                if isinstance(candidate, dict):
                    summary = candidate
            except json.JSONDecodeError:
                pass
        status = str((summary or {}).get("status") or ("succeeded" if process.returncode == 0 else "failed"))
        with JOB_LOCK:
            JOB_STATE.update({
                "status": status,
                "operation": "web_scrape",
                "finished_at": now_iso(),
                "exit_code": process.returncode,
                "summary": summary,
                "log_tail": (output or "")[-12000:],
                "error": None,
            })
    except Exception as exc:
        with JOB_LOCK:
            JOB_STATE.update({
                "status": "failed",
                "operation": "web_scrape",
                "finished_at": now_iso(),
                "exit_code": None,
                "summary": None,
                "log_tail": "",
                "error": f"{type(exc).__name__}: {exc}",
            })


def run_analysis_job(job_id: str, csv_file_name: str, app_settings: dict[str, str]) -> None:
    client: OpenCLIClient | None = None
    with JOB_LOCK:
        started_at = now_iso()
        JOB_STATE.update({
            "status": "running",
            "operation": "llm_analysis",
            "started_at": started_at,
            "error": None,
        })
    try:
        csv_path = summary_csv_path(csv_file_name)
        source_json_name = result_name_for_summary_csv(csv_file_name)
        source_path = OUTPUT_DIR / source_json_name
        if source_path.is_file():
            run_data = json.loads(source_path.read_text(encoding="utf-8-sig"))
            if not isinstance(run_data, dict):
                raise WorkflowError("对应的采集 JSON 顶层必须是对象。")
        else:
            run_data = {
                "run_id": source_json_name.removeprefix("xhs_run_").removesuffix(".json"),
                "warnings": [],
            }
        run_data["run_id"] = analysis_value(run_data.get("run_id")) or source_json_name.removeprefix("xhs_run_").removesuffix(".json")
        if not isinstance(run_data.get("warnings"), list):
            run_data["warnings"] = []
        if not isinstance(run_data.get("media_commands"), list):
            run_data["media_commands"] = []

        llm_settings = normalize_llm_settings(read_object(LLM_SETTINGS_PATH, DEFAULT_LLM_SETTINGS))
        if not llm_settings.get("enabled"):
            raise WorkflowError("请先在“设置”中启用媒体与 LLM 分析并保存。")
        get_configured_secret(llm_settings)

        filters = normalize_filters(read_object(FILTERS_PATH, {}))
        launcher, display_path = resolve_opencli_launcher(app_settings.get("opencli_bin") or None)
        client = OpenCLIClient(
            launcher,
            display_path,
            filters["timeout_seconds"],
            profile=app_settings.get("opencli_profile", ""),
        )

        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                fieldnames = reader.fieldnames or []
                rows = [{key: value or "" for key, value in row.items() if key is not None} for row in reader]
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise WorkflowError(f"无法读取 CSV 文件：{exc}") from exc
        required_columns = {"媒体摘要", "画面文字", "视频关键点", "音频转写", "媒体解析状态"}
        missing_columns = sorted(required_columns.difference(fieldnames))
        if missing_columns:
            raise WorkflowError(f"CSV 缺少媒体分析结果列：{'、'.join(missing_columns)}。")

        note_lookup = notes_by_canonical_url(run_data)
        previous_warnings = len(run_data.get("warnings", []))
        analyzed_count = 0
        failure_count = 0
        target_count = 0
        first_error: str | None = None
        for row_index, row in enumerate(rows, start=1):
            if str(row.get("媒体解析状态") or "").strip().lower() == "success":
                continue
            target_count += 1
            note_url = str(row.get("笔记链接") or "").strip()
            note: dict[str, Any] = {
                "url": note_url,
                "source_item": {
                    "url": note_url,
                    "title": row.get("笔记标题") or "",
                    "author": row.get("作者") or "",
                    "content": row.get("正文") or "",
                    "tags": row.get("标签") or "",
                    "rank": row.get("采集排名") or "",
                },
            }
            note_id = urlparse(note_url).path.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[A-Fa-f0-9]{16,32}", note_id):
                note["note_id"] = note_id
            context = {
                "source_type": "csv",
                "topic": row.get("主题") or "",
                "account": row.get("来源账号") or "",
            }
            if not note_url:
                result = {"status": "failed", "assets": [], "error": "缺少笔记链接。"}
                run_data["warnings"].append({"stage": "media_analysis", "source": context.get("topic"), "message": f"第 {row_index} 行缺少笔记链接。"})
            else:
                try:
                    result = analyze_note_media(
                        note,
                        context,
                        OUTPUT_DIR,
                        run_data,
                        row_index,
                        llm_settings,
                        lambda arguments: client.invoke(arguments),
                    )
                except Exception as exc:
                    result = {"status": "failed", "assets": [], "error": f"{type(exc).__name__}: {exc}"}
                    run_data["warnings"].append({"stage": "media_analysis", "source": context.get("topic"), "message": f"第 {row_index} 行媒体处理失败：{exc}"})
            apply_analysis_to_csv_row(row, result)
            analyzed_count += 1
            if result.get("status") != "success":
                failure_count += 1
                if not first_error:
                    first_error = analysis_value(result.get("error")) or "媒体分析未成功完成。"
            matched_notes = note_lookup.get(canonical_note_url(note_url), [])
            for matched_note in matched_notes:
                matched_note["media_analysis"] = json.loads(json.dumps(result, ensure_ascii=False))

        new_warnings = run_data.get("warnings", [])[previous_warnings:]
        first_warning = next(
            (
                str(item.get("message")).strip()
                for item in new_warnings
                if isinstance(item, dict) and item.get("message")
            ),
            None,
        )
        status = "partial" if failure_count else "succeeded"
        run_data["media_analysis_run"] = {
            "started_at": started_at,
            "finished_at": now_iso(),
            "status": status,
            "source_csv": csv_file_name,
            "analyzed_count": analyzed_count,
            "failed_count": failure_count,
            "target_count": target_count,
        }
        run_data["summary_csv"] = csv_file_name
        run_data["summary_note_count"] = len(rows)
        run_data["csv_stale"] = False
        if source_path.is_file():
            atomic_write_json(source_path, run_data)
        write_summary_csv(csv_path, fieldnames, rows)
        summary = {
            "status": status,
            "operation": "llm_analysis",
            "output_file": source_path.name if source_path.is_file() else None,
            "summary_csv": csv_file_name,
            "row_count": len(rows),
            "target_count": target_count,
            "analyzed_count": analyzed_count,
            "failed_count": failure_count,
            "note": "没有待分析内容。" if not target_count else "分析结果已直接写回所选 CSV。",
        }
        if first_warning:
            summary["first_warning"] = first_warning
        if first_error:
            summary["first_error"] = first_error
        with JOB_LOCK:
            JOB_STATE.update({
                "job_id": job_id,
                "status": status,
                "operation": "llm_analysis",
                "finished_at": now_iso(),
                "exit_code": 0 if status == "succeeded" else 1,
                "summary": summary,
                "log_tail": json.dumps(summary, ensure_ascii=False, indent=2),
                "error": None,
            })
    except Exception as exc:
        with JOB_LOCK:
            JOB_STATE.update({
                "job_id": job_id,
                "status": "failed",
                "operation": "llm_analysis",
                "finished_at": now_iso(),
                "exit_code": 1,
                "summary": None,
                "log_tail": "",
                "error": f"{type(exc).__name__}: {exc}",
            })
    finally:
        if client is not None:
            try:
                closed = close_opencli_blank_tabs(client)
                if closed:
                    print(f"[webui] 已自动关闭 {closed} 个空白标签页。", flush=True)
            except Exception as exc:
                print(f"[webui] 自动关闭空白标签页失败：{type(exc).__name__}: {exc}", flush=True)


def run_filter_job(job_id: str, file_name: str) -> None:
    with JOB_LOCK:
        JOB_STATE.update({
            "status": "running",
            "operation": "note_filter",
            "started_at": now_iso(),
            "error": None,
        })
    try:
        source_path = result_json_path(file_name)
        note_filter = normalize_filters(read_object(FILTERS_PATH, {}))["csv"]["note_filter"]
        output_path, _, counts = filter_result_file(source_path, OUTPUT_DIR, note_filter)
        summary = {
            "status": "succeeded",
            "operation": "note_filter",
            "source_file": source_path.name,
            "output_file": output_path.name,
            "input_note_count": counts["input_note_count"],
            "output_note_count": counts["output_note_count"],
            "note_filter": note_filter,
        }
        with JOB_LOCK:
            JOB_STATE.update({
                "job_id": job_id,
                "status": "succeeded",
                "operation": "note_filter",
                "finished_at": now_iso(),
                "exit_code": 0,
                "summary": summary,
                "log_tail": json.dumps(summary, ensure_ascii=False, indent=2),
                "error": None,
            })
    except Exception as exc:
        with JOB_LOCK:
            JOB_STATE.update({
                "job_id": job_id,
                "status": "failed",
                "operation": "note_filter",
                "finished_at": now_iso(),
                "exit_code": 1,
                "summary": {"source_file": file_name},
                "log_tail": "",
                "error": f"{type(exc).__name__}: {exc}",
            })


def run_aggregation_job(job_id: str, file_name: str) -> None:
    with JOB_LOCK:
        JOB_STATE.update({
            "status": "running",
            "operation": "csv_aggregation",
            "started_at": now_iso(),
            "error": None,
        })
    try:
        source_path = result_json_path(file_name)
        csv_path, rows = aggregate_result_file(source_path)
        run_data = json.loads(source_path.read_text(encoding="utf-8-sig"))
        if isinstance(run_data, dict):
            run_data["summary_csv"] = csv_path.name
            run_data["summary_note_count"] = len(rows)
            run_data["csv_stale"] = False
            atomic_write_json(source_path, run_data)
        summary = {
            "status": "succeeded",
            "operation": "csv_aggregation",
            "output_file": source_path.name,
            "summary_csv": csv_path.name,
            "row_count": len(rows),
        }
        with JOB_LOCK:
            JOB_STATE.update({
                "job_id": job_id,
                "status": "succeeded",
                "operation": "csv_aggregation",
                "finished_at": now_iso(),
                "exit_code": 0,
                "summary": summary,
                "log_tail": json.dumps(summary, ensure_ascii=False, indent=2),
                "error": None,
            })
    except Exception as exc:
        with JOB_LOCK:
            JOB_STATE.update({
                "job_id": job_id,
                "status": "failed",
                "operation": "csv_aggregation",
                "finished_at": now_iso(),
                "exit_code": 1,
                "summary": {"output_file": file_name},
                "log_tail": "",
                "error": f"{type(exc).__name__}: {exc}",
            })


def workflow_worker(job_id: str, workflow: dict[str, Any]) -> None:
    latest_generated: str | None = None
    latest_json_file: str | None = None
    merge_input_files: list[str] = []
    latest_output_file: str | None = None
    latest_csv_file: str | None = None
    completed: list[dict[str, Any]] = []
    partial = False
    active_step: dict[str, Any] | None = None
    execution: dict[str, Any] = {
        "job_id": job_id,
        "workflow": workflow.get("name"),
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "current_task": None,
        "steps": [],
    }

    def publish_progress() -> None:
        execution["updated_at"] = now_iso()
        persistence_error = persist_workflow_execution(latest_output_file, execution)
        if persistence_error:
            warnings = execution.setdefault("recording_warnings", [])
            message = f"无法将工作流进度写入采集 JSON：{persistence_error}"
            if message not in warnings:
                warnings.append(message)
        snapshot = json.loads(json.dumps(execution, ensure_ascii=False))
        with JOB_LOCK:
            JOB_STATE["workflow_execution"] = snapshot

    try:
        app_settings = validate_app_settings(read_object(APP_SETTINGS_PATH, DEFAULT_APP_SETTINGS))
        for index, task in enumerate(workflow["tasks"], start=1):
            kind = task["type"]
            labels = {"scrape": "脚本提取", "filter": "笔记筛选", "aggregate": "CSV 整理", "analyze": "大模型分析"}
            active_step = {
                "index": index,
                "task": labels[kind],
                "type": kind,
                "status": "running",
                "started_at": now_iso(),
            }
            execution["current_task"] = labels[kind]
            execution["steps"].append(active_step)
            with JOB_LOCK:
                JOB_STATE.update({
                    "status": "running",
                    "operation": "workflow",
                    "started_at": JOB_STATE.get("started_at") or now_iso(),
                    "summary": {
                        "workflow": workflow["name"],
                        "task": index,
                        "task_count": len(workflow["tasks"]),
                        "task_name": labels[kind],
                        "completed_tasks": list(completed),
                    },
                    "log_tail": f"工作流：{workflow['name']}\n正在执行第 {index}/{len(workflow['tasks'])} 步：{labels[kind]}",
                    "error": None,
                })
            publish_progress()
            if kind == "scrape":
                run_job(job_id, app_settings, task.get("keywords"), task.get("users"))
                with JOB_LOCK:
                    task_state = dict(JOB_STATE)
                summary = task_state.get("summary") or {}
                status = task_state.get("status")
                task_summary = summary
                generated = summary.get("output_file")
                latest_generated = resolve_workflow_output_file(generated)

                # 有些旧版/异常子进程会留下正确 JSON，却没有可靠地回传输出路径。
                # run_id 与文件名一一对应，可安全地从该次运行编号恢复交接路径。
                run_id = summary.get("run_id")
                if not latest_generated and isinstance(run_id, str) and run_id:
                    latest_generated = resolve_workflow_output_file(f"xhs_run_{run_id}.json")
                    if latest_generated:
                        active_step["output_path_recovered_from_run_id"] = True
                if latest_generated:
                    latest_output_file = latest_generated

                # JSON 是采集脚本的持久结果；若子进程状态回报与同一 run_id 的 JSON 冲突，
                # 以 JSON 中的完成状态为准，避免有效采集结果被误判后跳过 CSV 整理。
                if latest_generated and status not in {"succeeded", "partial"}:
                    try:
                        artifact = json.loads(result_json_path(latest_generated).read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError, WorkflowError):
                        artifact = None
                    artifact_status = artifact.get("status") if isinstance(artifact, dict) else None
                    artifact_run_id = artifact.get("run_id") if isinstance(artifact, dict) else None
                    same_run = not run_id or not artifact_run_id or str(run_id) == str(artifact_run_id)
                    if same_run and artifact_status in {"succeeded", "partial"}:
                        status = artifact_status
                        task_summary = {**summary, "status": artifact_status, "output_file": latest_generated}
                        active_step["status_recovered_from_json"] = True

                if status == "partial":
                    partial = True
                if status not in {"succeeded", "partial"}:
                    detail = task_state.get("error") or summary.get("first_error") or summary.get("first_warning")
                    if detail:
                        raise WorkflowError(f"采集子进程返回失败状态：{detail}")
                    raise WorkflowError(f"采集子进程返回失败状态：{status or '未知'}。")
                if not latest_generated:
                    raise WorkflowError(f"采集已完成，但未能定位本次输出 JSON：{generated!r}。")
                active_step["output_file"] = latest_generated
                latest_json_file = latest_generated
                if latest_json_file not in merge_input_files:
                    merge_input_files.append(latest_json_file)
                latest_csv_file = None
            elif kind == "filter":
                source = task["source"]
                source_files: list[str] = []
                if source["kind"] == "workflow_all":
                    source_files = list(dict.fromkeys(merge_input_files))
                    if not source_files and latest_json_file:
                        source_files = [latest_json_file]
                    if not source_files:
                        raise WorkflowError("当前工作流尚未生成可供合并筛选的 JSON 文件。")
                    source_paths = [result_json_path(name) for name in source_files]
                    merged_path, _ = merge_result_files(source_paths, OUTPUT_DIR)
                    file_name = merged_path.name
                    if len(source_files) == 1:
                        active_step["input_file"] = file_name
                    else:
                        active_step["input_files"] = source_files
                else:
                    file_name = latest_json_file if source["kind"] == "workflow" else source["file"]
                    if not file_name:
                        raise WorkflowError("当前工作流尚未生成可供笔记筛选使用的 JSON 文件。")
                    result_json_path(file_name)
                    active_step["input_file"] = file_name
                output_path, _, counts = filter_result_file(
                    result_json_path(file_name),
                    OUTPUT_DIR,
                    task["note_filter"],
                )
                latest_json_file = output_path.name
                if source["kind"] == "workflow_all":
                    merge_input_files = [latest_json_file]
                elif source["kind"] == "workflow" and merge_input_files:
                    merge_input_files[-1] = latest_json_file
                elif latest_json_file not in merge_input_files:
                    merge_input_files.append(latest_json_file)
                latest_output_file = output_path.name
                latest_csv_file = None
                task_summary = {
                    "status": "succeeded",
                    "operation": "note_filter",
                    "source_file": file_name,
                    "output_file": output_path.name,
                    "input_note_count": counts["input_note_count"],
                    "output_note_count": counts["output_note_count"],
                    "note_filter": task["note_filter"],
                }
                if source["kind"] == "workflow_all":
                    task_summary["source_files"] = source_files
                active_step["output_file"] = output_path.name
                active_step["input_note_count"] = counts["input_note_count"]
                active_step["output_note_count"] = counts["output_note_count"]
                with JOB_LOCK:
                    JOB_STATE.update({
                        "status": "succeeded",
                        "operation": "workflow",
                        "exit_code": 0,
                        "summary": task_summary,
                        "log_tail": json.dumps(task_summary, ensure_ascii=False, indent=2),
                        "error": None,
                    })
            elif kind == "aggregate":
                source = task["source"]
                source_files: list[str] = []
                if source["kind"] == "workflow_all":
                    source_files = list(dict.fromkeys(merge_input_files))
                    if not source_files and latest_json_file:
                        source_files = [latest_json_file]
                    if not source_files:
                        raise WorkflowError("当前工作流尚未生成可供合并整理的 JSON 文件。")
                    source_paths = [result_json_path(name) for name in source_files]
                    merged_path, _ = merge_result_files(source_paths, OUTPUT_DIR)
                    file_name = merged_path.name
                    latest_json_file = file_name
                    merge_input_files = [file_name]
                    if len(source_files) == 1:
                        active_step["input_file"] = file_name
                    else:
                        active_step["input_files"] = source_files
                else:
                    file_name = latest_json_file if source["kind"] == "workflow" else source["file"]
                    if not file_name:
                        raise WorkflowError("当前工作流尚未生成可供 CSV 整理使用的 JSON 文件。")
                    result_json_path(file_name)
                    active_step["input_file"] = file_name
                csv_path, rows = aggregate_result_file(result_json_path(file_name))
                run_data = json.loads(result_json_path(file_name).read_text(encoding="utf-8-sig"))
                if isinstance(run_data, dict):
                    run_data["summary_csv"] = csv_path.name
                    run_data["summary_note_count"] = len(rows)
                    run_data["csv_stale"] = False
                    atomic_write_json(result_json_path(file_name), run_data)
                task_summary = {
                    "status": "succeeded",
                    "operation": "csv_aggregation",
                    "output_file": file_name,
                    "summary_csv": csv_path.name,
                    "row_count": len(rows),
                }
                if source["kind"] == "workflow_all":
                    task_summary["source_files"] = source_files
                latest_output_file = file_name
                latest_csv_file = csv_path.name
                active_step["output_file"] = csv_path.name
                active_step["row_count"] = len(rows)
                with JOB_LOCK:
                    JOB_STATE.update({"status": "succeeded", "operation": "workflow", "exit_code": 0, "summary": task_summary, "log_tail": json.dumps(task_summary, ensure_ascii=False, indent=2), "error": None})
            else:
                source = task["source"]
                csv_name = latest_csv_file if source["kind"] == "workflow" else source["file"]
                if not csv_name:
                    raise WorkflowError("当前工作流尚未生成可供大模型分析使用的 CSV 文件。")
                summary_csv_path(csv_name)
                active_step["input_file"] = csv_name
                run_analysis_job(job_id, csv_name, app_settings)
                with JOB_LOCK:
                    task_state = dict(JOB_STATE)
                if task_state.get("status") == "partial":
                    partial = True
                if task_state.get("status") not in {"succeeded", "partial"}:
                    raise WorkflowError(task_state.get("error") or "大模型分析失败，已停止后续步骤。")
                task_summary = task_state.get("summary") or {}
                latest_csv_file = csv_name
                json_name = result_name_for_summary_csv(csv_name)
                latest_output_file = json_name if (OUTPUT_DIR / json_name).is_file() else None
                active_step["output_file"] = csv_name
            completed_task = {
                "task": labels[kind],
                "status": "partial" if task_summary.get("status") == "partial" else "succeeded",
                "output_file": task_summary.get("output_file") or active_step.get("output_file") or latest_generated,
            }
            if task_summary.get("first_warning"):
                completed_task["first_warning"] = task_summary["first_warning"]
            completed.append(completed_task)
            active_step["status"] = completed_task["status"]
            active_step["finished_at"] = now_iso()
            if task_summary.get("first_warning"):
                active_step["first_warning"] = task_summary["first_warning"]
            execution["current_task"] = None
            execution["completed_tasks"] = list(completed)
            publish_progress()
        final_status = "partial" if partial else "succeeded"
        execution["status"] = final_status
        execution["finished_at"] = now_iso()
        summary = {"status": final_status, "operation": "workflow", "workflow": workflow["name"], "completed_tasks": completed, "output_file": latest_output_file, "summary_csv": latest_csv_file}
        first_warning = next(
            (item.get("first_warning") for item in completed if item.get("first_warning")),
            None,
        )
        if first_warning:
            summary["first_warning"] = first_warning
        summary["workflow_execution"] = execution
        publish_progress()
        with JOB_LOCK:
            JOB_STATE.update({"job_id": job_id, "status": final_status, "operation": "workflow", "workflow_active": False, "finished_at": execution["finished_at"], "exit_code": 0 if final_status == "succeeded" else 1, "summary": summary, "log_tail": json.dumps(execution, ensure_ascii=False, indent=2), "error": None})
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        failed_task = active_step.get("task") if active_step else "工作流初始化"
        error_text = f"{failed_task}失败：{detail}"
        if active_step and active_step.get("status") == "running":
            active_step.update({
                "status": "failed",
                "finished_at": now_iso(),
                "error": detail,
                "traceback": traceback.format_exc(),
            })
        execution.update({
            "status": "failed",
            "finished_at": now_iso(),
            "current_task": None,
            "failed_task": failed_task,
            "error": error_text,
            "completed_tasks": list(completed),
        })
        publish_progress()
        failure_summary = {
            "status": "failed",
            "operation": "workflow",
            "workflow": workflow.get("name"),
            "completed_tasks": completed,
            "failed_task": failed_task,
            "first_error": error_text,
            "output_file": latest_output_file,
            "summary_csv": latest_csv_file,
            "workflow_execution": execution,
        }
        with JOB_LOCK:
            JOB_STATE.update({"job_id": job_id, "status": "failed", "operation": "workflow", "workflow_active": False, "finished_at": execution["finished_at"], "exit_code": 1, "summary": failure_summary, "log_tail": json.dumps(execution, ensure_ascii=False, indent=2), "error": error_text})


def start_workflow_job(
    workflow: dict[str, Any],
    trigger: str = "manual",
    origin: str = "workflows",
) -> str | None:
    with JOB_LOCK:
        if job_is_active():
            return None
        job_id = uuid.uuid4().hex
        JOB_STATE.update({"job_id": job_id, "status": "queued", "operation": "workflow", "origin": origin, "workflow_active": True, "workflow_execution": None, "started_at": None, "finished_at": None, "exit_code": None, "summary": {"workflow": workflow["name"], "trigger": trigger}, "log_tail": "", "error": None})
        threading.Thread(target=workflow_worker, args=(job_id, workflow), daemon=True).start()
        return job_id


def workflow_scheduler() -> None:
    while True:
        try:
            now = datetime.now().astimezone()
            for workflow in load_workflows():
                if not workflow.get("enabled"):
                    continue
                schedule = workflow.get("schedule", {})
                if schedule.get("frequency") == "weekly" and now.weekday() != schedule.get("weekday"):
                    continue
                hour, minute = map(int, schedule.get("time", "09:00").split(":"))
                if (now.hour, now.minute) < (hour, minute):
                    continue
                key = now.strftime("%Y-%m-%d")
                if workflow.get("schedule", {}).get("frequency") == "weekly":
                    key = f"{key}-W{now.weekday()}"
                if workflow.get("last_triggered_key") == key:
                    continue
                job_id = start_workflow_job(workflow, "scheduled")
                if job_id:
                    with WORKFLOW_LOCK:
                        latest = read_object(WORKFLOWS_PATH, {"workflows": []})
                        for item in latest.get("workflows", []):
                            if item.get("id") == workflow.get("id"):
                                item["last_triggered_key"] = key
                                break
                        atomic_write_json(WORKFLOWS_PATH, latest)
                    break
        except Exception as exc:
            print(f"[webui] 工作流定时调度异常：{type(exc).__name__}: {exc}", flush=True)
        time.sleep(15)


def run_doctor(settings: dict[str, str]) -> dict[str, Any]:
    launcher, display_path = resolve_opencli_launcher(settings.get("opencli_bin") or None)
    client = OpenCLIClient(launcher, display_path, 60, profile=settings.get("opencli_profile", ""))
    record = client.invoke(["doctor"], parse_json=False)
    output = str(record.get("raw_stdout", "")) + chr(10) + str(record.get("raw_stderr", ""))
    return {
        "ready": is_browser_environment_ready(record),
        "version": display_path,
        "output": output.strip(),
        "error": record.get("error"),
    }


def run_dependency_command(command: list[str], timeout_seconds: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": result.stdout.strip()[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"ok": False, "returncode": None, "error": f"命令超时（{timeout_seconds} 秒）。\n{output[-12000:]}"}
    except OSError as exc:
        return {"ok": False, "returncode": None, "error": f"无法启动命令：{exc}"}


def check_node_runtime() -> dict[str, Any]:
    node_path = shutil.which("node.exe") or shutil.which("node")
    if not node_path:
        return {"ok": False, "error": "找不到 Node.js。"}
    result = run_dependency_command([node_path, "--version"], 20)
    return {"ok": result["ok"], "version": result.get("output", ""), "path": node_path, "error": result.get("error")}


def check_opencli_runtime(settings: dict[str, str]) -> dict[str, Any]:
    try:
        launcher, display_path = resolve_opencli_launcher(settings.get("opencli_bin") or None)
    except (OSError, WorkflowError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    result = run_dependency_command([*launcher, "--version"], 30)
    return {
        "ok": result["ok"],
        "version": result.get("output", ""),
        "path": display_path,
        "error": result.get("error"),
    }


def package_manager_for_opencli(path_text: str) -> str | None:
    normalized = Path(path_text).resolve().as_posix().lower()
    parts = {part for part in normalized.split("/") if part}
    if "pnpm" in parts:
        return "pnpm"
    if "npm" in parts or "node_modules" in parts:
        return "npm"
    return None


def package_manager_launcher(manager: str) -> list[str] | None:
    manager_path = shutil.which(manager) or shutil.which(f"{manager}.cmd")
    if not manager_path:
        return None
    path = Path(manager_path)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
        script = path.with_suffix(".ps1")
        if powershell and script.is_file():
            return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    return [str(path)]


def maintain_project_dependencies(settings: dict[str, str]) -> dict[str, Any]:
    python_update = run_dependency_command(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "-r", str(ROOT / "requirements.txt")],
        600,
    )
    pip_check = run_dependency_command([sys.executable, "-m", "pip", "check"], 120)
    node = check_node_runtime()
    opencli_before = check_opencli_runtime(settings)

    opencli_update: dict[str, Any]
    manager = package_manager_for_opencli(opencli_before["path"]) if opencli_before.get("path") else None
    manager_launcher = package_manager_launcher(manager) if manager else None
    if not opencli_before["ok"]:
        opencli_update = {"status": "skipped", "output": "OpenCLI 当前不可用，未执行更新。"}
    elif not manager:
        opencli_update = {"status": "skipped", "output": "无法从启动路径确认 npm/pnpm 安装来源，未自动修改该安装。"}
    elif not manager_launcher:
        opencli_update = {"status": "skipped", "output": f"找不到 {manager}，未执行 OpenCLI 更新。"}
    else:
        args = (
            [*manager_launcher, "add", "--global", "@jackwener/opencli@latest"]
            if manager == "pnpm"
            else [*manager_launcher, "install", "--global", "@jackwener/opencli@latest"]
        )
        update_result = run_dependency_command(args, 600)
        opencli_update = {
            "status": "updated" if update_result["ok"] else "failed",
            "output": update_result.get("output", ""),
            "error": update_result.get("error"),
        }

    opencli_after = check_opencli_runtime(settings)
    return {
        "python_version": sys.version.split()[0],
        "python_updated": python_update["ok"],
        "pip_update": python_update,
        "pip_check": pip_check,
        "node": node,
        "opencli_before": opencli_before,
        "opencli_update": opencli_update,
        "opencli_after": opencli_after,
    }


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "SocialTrendLocalUI/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        print("[webui] " + format_string % args, flush=True)

    def _send_bytes(self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _host_is_local(self) -> bool:
        host = self.headers.get("Host", "").lower()
        allowed = {"127.0.0.1", "localhost"}
        hostname = host.rsplit(":", 1)[0].strip("[]")
        return hostname in allowed

    def _origin_is_local(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == self.server.server_port

    def _mutation_allowed(self) -> bool:
        return (
            self._host_is_local()
            and self._origin_is_local()
            and secrets.compare_digest(self.headers.get("X-Local-Token", ""), CSRF_TOKEN)
        )

    def _read_json_body(self) -> Any:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise WorkflowError("请求长度无效。") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise WorkflowError("请求内容超过本地 WebUI 限制。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowError("请求 JSON 无效。") from exc

    def do_GET(self) -> None:
        if not self._host_is_local():
            self._send_json(403, {"error": "仅允许本机访问。"})
            return
        path = urlparse(self.path).path
        if path == "/":
            html = INDEX_PATH.read_text(encoding="utf-8").replace("__LOCAL_TOKEN__", CSRF_TOKEN)
            self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._send_json(200, load_config_state())
            except WorkflowError as exc:
                self._send_json(500, {"error": str(exc)})
        elif path == "/api/run/status":
            self._send_json(200, get_job_state())
        elif path == "/api/results":
            self._send_json(200, {"files": list_result_files()})
        elif path == "/api/csv-files":
            self._send_json(200, {"files": list_summary_csv_files()})
        elif path == "/api/csv-preview":
            file_name = parse_qs(urlparse(self.path).query).get("file", [""])[0]
            try:
                self._send_json(200, read_csv_preview(file_name))
            except WorkflowError as exc:
                self._send_json(404, {"error": str(exc)})
        elif path == "/api/workflows":
            try:
                config = load_config_state()
                self._send_json(200, {
                    "workflows": load_workflows(),
                    "keywords": config["keywords"],
                    "users": config["users"],
                    "files": list_result_files(),
                    "csv_files": list_summary_csv_files(),
                })
            except WorkflowError as exc:
                self._send_json(500, {"error": str(exc)})
        elif path == "/api/llm/credential-public-key":
            if public_key_payload is None:
                self._send_json(503, {"error": f"缺少项目加密依赖，请在设置中更新依赖并重启 WebUI：{KEY_TRANSPORT_IMPORT_ERROR}"})
            else:
                self._send_json(200, public_key_payload())
        elif path == "/api/llm/key-status":
            credential_id = parse_qs(urlparse(self.path).query).get("credential_id", [""])[0]
            try:
                if credential_id:
                    validate_credential_id(credential_id)
                self._send_json(200, {"configured": secret_exists(credential_id) if credential_id else False})
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(400, {"configured": False, "error": str(exc)})
        elif path == "/api/aggregation":
            file_name = parse_qs(urlparse(self.path).query).get("file", [""])[0]
            try:
                self._send_json(200, read_aggregation_preview(file_name))
            except WorkflowError as exc:
                self._send_json(404, {"error": str(exc)})
        elif path.startswith("/output/"):
            name = unquote(path.removeprefix("/output/"))
            if RESULT_FILE_PATTERN.fullmatch(name):
                content_type = "application/json; charset=utf-8"
            elif SUMMARY_FILE_PATTERN.fullmatch(name):
                content_type = "text/csv; charset=utf-8"
            else:
                self._send_json(400, {"error": "结果文件名无效。"})
                return
            file_path = OUTPUT_DIR / name
            if not file_path.is_file():
                self._send_json(404, {"error": "结果文件不存在。"})
                return
            self._send_bytes(
                200,
                file_path.read_bytes(),
                content_type,
                {"Content-Disposition": f'attachment; filename="{name}"'},
            )
        else:
            self._send_json(404, {"error": "路径不存在。"})

    def do_POST(self) -> None:
        if not self._mutation_allowed():
            self._send_json(403, {"error": "本机令牌或来源校验失败，请刷新页面后重试。"})
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                payload = self._read_json_body()
                saved = validate_and_save(payload)
                self._send_json(200, {"saved": True, "config": saved})
            elif path == "/api/dependencies":
                settings = validate_app_settings(self._read_json_body())
                with JOB_LOCK:
                    if job_is_active():
                        self._send_json(409, {"error": "采集或依赖任务正在运行，请稍后重试。"})
                        return
                    JOB_STATE["dependencies_updating"] = True
                try:
                    result = maintain_project_dependencies(settings)
                finally:
                    with JOB_LOCK:
                        JOB_STATE["dependencies_updating"] = False
                self._send_json(200, result)
            elif path == "/api/workflows":
                workflows = validate_workflows(self._read_json_body())
                save_workflows(workflows)
                self._send_json(200, {"saved": True, "workflows": workflows})
            elif path == "/api/workflows/run":
                payload = self._read_json_body()
                if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
                    raise WorkflowError("请选择要运行的工作流。")
                workflow = next((item for item in load_workflows() if item.get("id") == payload["id"]), None)
                if workflow is None:
                    raise WorkflowError("找不到指定的工作流。")
                job_id = start_workflow_job(workflow)
                if not job_id:
                    self._send_json(409, {"error": "已有后台任务正在运行。"})
                    return
                self._send_json(202, {"job_id": job_id, "status": "queued"})
            elif path == "/api/run-all":
                llm_settings = normalize_llm_settings(read_object(LLM_SETTINGS_PATH, DEFAULT_LLM_SETTINGS))
                if not llm_settings.get("enabled"):
                    raise WorkflowError("一键运行全部环节需要先在“设置”中启用大模型分析并保存。")
                get_configured_secret(llm_settings)
                note_filter = normalize_filters(read_object(FILTERS_PATH, {}))["csv"]["note_filter"]
                tasks = [{"id": uuid.uuid4().hex, "type": "scrape", "keywords": None, "users": None}]
                if note_filter["enabled"]:
                    tasks.append({
                        "id": uuid.uuid4().hex,
                        "type": "filter",
                        "source": {"kind": "workflow"},
                        "note_filter": note_filter,
                    })
                tasks.extend([
                    {"id": uuid.uuid4().hex, "type": "aggregate", "source": {"kind": "workflow"}},
                    {"id": uuid.uuid4().hex, "type": "analyze", "source": {"kind": "workflow"}},
                ])
                workflow = {
                    "id": uuid.uuid4().hex,
                    "name": "一键运行全部环节",
                    "tasks": tasks,
                }
                job_id = start_workflow_job(workflow, "manual_all", "run")
                if not job_id:
                    self._send_json(409, {"error": "已有后台任务正在运行。"})
                    return
                self._send_json(202, {"job_id": job_id, "status": "queued", "operation": "workflow"})
            elif path == "/api/run":
                payload = self._read_json_body()
                origin = "workflows" if isinstance(payload, dict) and payload.get("origin") == "workflows" else "run"
                with JOB_LOCK:
                    if job_is_active():
                        self._send_json(409, {"error": "已有采集任务正在运行。"})
                        return
                    settings = validate_app_settings(read_object(APP_SETTINGS_PATH, DEFAULT_APP_SETTINGS))
                    job_id = uuid.uuid4().hex
                    JOB_STATE.update({
                        "job_id": job_id,
                        "status": "queued",
                        "operation": "web_scrape",
                        "origin": origin,
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "summary": None,
                        "log_tail": "",
                        "error": None,
                    })
                    threading.Thread(target=run_job, args=(job_id, settings), daemon=True).start()
                self._send_json(202, {"job_id": job_id, "status": "queued"})
            elif path == "/api/analyze":
                payload = self._read_json_body()
                if not isinstance(payload, dict) or not isinstance(payload.get("file"), str):
                    raise WorkflowError("请求中必须提供要分析的 CSV 文件名。")
                file_name = payload["file"]
                origin = "workflows" if payload.get("origin") == "workflows" else "run"
                summary_csv_path(file_name)
                with JOB_LOCK:
                    if job_is_active():
                        self._send_json(409, {"error": "已有任务正在运行。"})
                        return
                    app_settings = validate_app_settings(read_object(APP_SETTINGS_PATH, DEFAULT_APP_SETTINGS))
                    job_id = uuid.uuid4().hex
                    JOB_STATE.update({
                        "job_id": job_id,
                        "status": "queued",
                        "operation": "llm_analysis",
                        "origin": origin,
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "summary": None,
                        "log_tail": "",
                        "error": None,
                    })
                    threading.Thread(
                        target=run_analysis_job,
                        args=(job_id, file_name, app_settings),
                        daemon=True,
                    ).start()
                self._send_json(202, {"job_id": job_id, "status": "queued", "operation": "llm_analysis"})
            elif path == "/api/llm/credential-store":
                if decrypt_and_store is None:
                    raise WorkflowError("缺少项目加密依赖，请在设置中更新依赖并重启 WebUI。")
                payload = self._read_json_body()
                credential_id = decrypt_and_store(payload)
                self._send_json(200, {"saved": True, "credential_id": credential_id})
            elif path == "/api/llm/test":
                payload = self._read_json_body()
                result = test_llm_connection(payload)
                self._send_json(200, result)
            elif path == "/api/llm/models":
                payload = self._read_json_body()
                self._send_json(200, {"models": get_available_models(payload)})
            elif path == "/api/filter":
                payload = self._read_json_body()
                if not isinstance(payload, dict) or not isinstance(payload.get("file"), str):
                    raise WorkflowError("请求中必须提供要筛选的采集 JSON 文件名。")
                file_name = payload["file"]
                result_json_path(file_name)
                origin = "workflows" if payload.get("origin") == "workflows" else "run"
                note_filter = normalize_filters(read_object(FILTERS_PATH, {}))["csv"]["note_filter"]
                if not note_filter["enabled"]:
                    raise WorkflowError("请先启用指标筛选并至少添加一条条件。")
                with JOB_LOCK:
                    if job_is_active():
                        raise WorkflowError("请等待当前后台任务结束后再筛选笔记。")
                    job_id = uuid.uuid4().hex
                    JOB_STATE.update({
                        "job_id": job_id,
                        "status": "queued",
                        "operation": "note_filter",
                        "origin": origin,
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "summary": {"source_file": file_name},
                        "log_tail": "",
                        "error": None,
                    })
                    threading.Thread(target=run_filter_job, args=(job_id, file_name), daemon=True).start()
                self._send_json(202, {"job_id": job_id, "status": "queued", "operation": "note_filter"})
            elif path == "/api/aggregate":
                payload = self._read_json_body()
                if not isinstance(payload, dict) or not isinstance(payload.get("file"), str):
                    raise WorkflowError("请求中必须提供采集 JSON 文件名。")
                file_name = payload["file"]
                result_json_path(file_name)
                origin = "workflows" if payload.get("origin") == "workflows" else "run"
                with JOB_LOCK:
                    if job_is_active():
                        raise WorkflowError("请等待当前后台任务结束后再整合 CSV。")
                    job_id = uuid.uuid4().hex
                    JOB_STATE.update({
                        "job_id": job_id,
                        "status": "queued",
                        "operation": "csv_aggregation",
                        "origin": origin,
                        "started_at": None,
                        "finished_at": None,
                        "exit_code": None,
                        "summary": {"output_file": file_name},
                        "log_tail": "",
                        "error": None,
                    })
                    threading.Thread(target=run_aggregation_job, args=(job_id, file_name), daemon=True).start()
                self._send_json(202, {"job_id": job_id, "status": "queued", "operation": "csv_aggregation"})
            elif path == "/api/doctor":
                settings = validate_app_settings(self._read_json_body())
                self._send_json(200, run_doctor(settings))
            else:
                self._send_json(404, {"error": "路径不存在。"})
        except WorkflowError as exc:
            self._send_json(400, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="启动仅供本机使用的小红书采集工作流 WebUI。")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("端口必须在 1024 到 65535 之间。")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), LocalHandler)
    server.daemon_threads = True
    threading.Thread(target=workflow_scheduler, daemon=True, name="workflow-scheduler").start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Local WebUI: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nWebUI stopped.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
