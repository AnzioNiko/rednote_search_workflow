#!/usr/bin/env python3
"""将一次小红书采集 JSON 整理为方便查看和分析的 CSV。"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

RUN_FILE_PATTERN = re.compile(
    r"^xhs_run_(?:[0-9]{8}T[0-9]{6}\.[0-9]{6}Z|[0-9]{8}_[0-9]{6}\.[0-9]{6}(?:\+0800)?)\.json$"
)
CSV_COLUMNS = [
    "主题",
    "笔记标题",
    "作者",
    "笔记链接",
    "发布时间",
    "正文",
    "标签",
    "点赞数",
    "收藏数",
    "评论数",
    "分享数",
    "浏览量",
    "曝光量",
    "观看量",
    "来源账号",
    "采集排名",
    "详情状态",
    "媒体摘要",
    "画面文字",
    "视频关键点",
    "音频转写",
    "媒体解析状态",
]

FIELD_ALIASES = {
    "title": ("title", "note_title", "笔记标题", "标题"),
    "author": ("author", "nickname", "user_name", "作者", "昵称"),
    "url": ("url", "note_url", "link", "share_link", "笔记链接", "链接"),
    "published_at": ("published_at", "publish_time", "create_time", "time", "发布时间"),
    "content": ("content", "note_content", "note_desc", "note_text", "desc", "description", "body", "text", "正文", "正文内容", "笔记内容", "内容"),
    "tags": ("tags", "tag", "topics", "hashtags", "标签"),
    "likes": ("likes", "like_count", "liked_count", "like", "点赞", "点赞数"),
    "collects": ("collects", "collect_count", "collected_count", "favorite_count", "收藏", "收藏数"),
    "comments": ("comments", "comment_count", "comments_count", "评论", "评论数"),
    "shares": ("shares", "share_count", "shared_count", "转发", "分享", "分享数"),
    "views": ("views", "view_count", "views_count", "read_count", "浏览量"),
    "exposures": ("exposures", "exposure", "impressions", "exposure_count", "曝光", "曝光量"),
    "watch_count": ("watch_count", "watched_count", "观看量", "观看次数", "播放量", "play_count", "playback_count"),
}

NOTE_FILTER_FIELDS = {
    "likes": "点赞数",
    "collects": "收藏数",
    "comments": "评论数",
}
NOTE_FILTER_OPERATORS = {"gt", "gte", "eq", "lte", "lt"}
MAX_NOTE_FILTER_GROUPS = 10
MAX_NOTE_FILTER_CONDITIONS = 10


def default_note_filter() -> dict[str, Any]:
    return {"enabled": False, "groups": []}


def normalize_note_filter(payload: Any) -> dict[str, Any]:
    """校验并规范化笔记指标筛选；条件组之间为“或”，组内条件为“且”。"""
    if payload is None:
        return default_note_filter()
    if not isinstance(payload, dict):
        raise ValueError("笔记指标筛选必须是 JSON 对象。")

    enabled = payload.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("笔记指标筛选的 enabled 必须是布尔值。")
    groups = payload.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("笔记指标筛选的 groups 必须是数组。")
    if len(groups) > MAX_NOTE_FILTER_GROUPS:
        raise ValueError(f"笔记指标筛选最多支持 {MAX_NOTE_FILTER_GROUPS} 个条件组。")

    normalized_groups: list[dict[str, Any]] = []
    for group_index, raw_group in enumerate(groups):
        if not isinstance(raw_group, dict):
            raise ValueError(f"笔记指标筛选第 {group_index + 1} 个条件组格式无效。")
        conditions = raw_group.get("conditions", [])
        if not isinstance(conditions, list):
            raise ValueError(f"笔记指标筛选第 {group_index + 1} 个条件组的 conditions 必须是数组。")
        if not conditions:
            raise ValueError(f"笔记指标筛选第 {group_index + 1} 个条件组至少需要一条条件。")
        if len(conditions) > MAX_NOTE_FILTER_CONDITIONS:
            raise ValueError(
                f"笔记指标筛选每个条件组最多支持 {MAX_NOTE_FILTER_CONDITIONS} 条条件。"
            )

        normalized_conditions: list[dict[str, Any]] = []
        for condition_index, raw_condition in enumerate(conditions):
            label = f"第 {group_index + 1} 组第 {condition_index + 1} 条条件"
            if not isinstance(raw_condition, dict):
                raise ValueError(f"笔记指标筛选{label}格式无效。")
            field = raw_condition.get("field")
            operator = raw_condition.get("operator")
            value = raw_condition.get("value")
            if field not in NOTE_FILTER_FIELDS:
                raise ValueError(f"笔记指标筛选{label}的指标无效。")
            if operator not in NOTE_FILTER_OPERATORS:
                raise ValueError(f"笔记指标筛选{label}的比较方式无效。")
            if type(value) is not int or not 0 <= value <= 2_147_483_647:
                raise ValueError(f"笔记指标筛选{label}的数值必须是 0 到 2147483647 之间的整数。")
            normalized_conditions.append({"field": field, "operator": operator, "value": value})
        normalized_groups.append({"conditions": normalized_conditions})

    if enabled and not normalized_groups:
        raise ValueError("启用笔记指标筛选后，至少需要添加一条条件和数值。")
    return {"enabled": enabled, "groups": normalized_groups}


def parse_metric_number(value: Any) -> float | None:
    """把 OpenCLI 返回的数字或“7.5万”这类文本转换为可比较的数值。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("，", "")
        if not text:
            return None
        match = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(亿|万|w|千|k)?\s*\+?",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        number = float(match.group(1))
        unit = (match.group(2) or "").lower()
        multiplier = {"亿": 100_000_000, "万": 10_000, "w": 10_000, "千": 1_000, "k": 1_000}.get(unit, 1)
        number *= multiplier
    return number if math.isfinite(number) else None


def _row_matches_normalized_filter(row: dict[str, Any], normalized: dict[str, Any]) -> bool:
    if not normalized["enabled"]:
        return True

    for group in normalized["groups"]:
        group_matches = True
        for condition in group["conditions"]:
            actual = parse_metric_number(row.get(NOTE_FILTER_FIELDS[condition["field"]]))
            if actual is None:
                group_matches = False
                break
            expected = condition["value"]
            operator = condition["operator"]
            if operator == "gt" and not actual > expected:
                group_matches = False
            elif operator == "gte" and not actual >= expected:
                group_matches = False
            elif operator == "eq" and not actual == expected:
                group_matches = False
            elif operator == "lte" and not actual <= expected:
                group_matches = False
            elif operator == "lt" and not actual < expected:
                group_matches = False
            if not group_matches:
                break
        if group_matches:
            return True
    return False


def row_matches_note_filter(row: dict[str, Any], note_filter: dict[str, Any] | None) -> bool:
    """判断一行是否满足指标筛选；条件组之间取“或”。"""
    return _row_matches_normalized_filter(row, normalize_note_filter(note_filter))


def flatten_fields(value: Any) -> dict[str, Any]:
    """读取 OpenCLI 常见的 field/value 列表或嵌套对象。"""
    result: dict[str, Any] = {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                field = item.get("field") or item.get("name") or item.get("key")
                if field is not None and "value" in item:
                    result[str(field)] = item.get("value")
                else:
                    result.update(flatten_fields(item))
    elif isinstance(value, dict):
        field = value.get("field") or value.get("name") or value.get("key")
        if field is not None and "value" in value:
            result[str(field)] = value.get("value")
        else:
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    nested = flatten_fields(item)
                    if nested:
                        result.update(nested)
                    elif isinstance(item, list) and item:
                        result[str(key)] = item
                else:
                    result[str(key)] = item
    return result


def field_value(fields: dict[str, Any], alias_key: str) -> Any:
    normalized = {re.sub(r"[\s_-]+", "", key).lower(): value for key, value in fields.items()}
    for alias in FIELD_ALIASES[alias_key]:
        key = re.sub(r"[\s_-]+", "", alias).lower()
        value = normalized.get(key)
        if value is not None and value != "":
            return value
    return ""


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def append_unique(values: list[str], value: Any) -> None:
    text = as_text(value)
    if text and text not in values:
        values.append(text)


def plain_summary_text(value: Any) -> str:
    """把模型返回的 JSON、代码围栏或普通摘要统一成可读文本。"""
    if isinstance(value, dict):
        for key in ("summary", "overall_summary", "description", "text", "content", "raw_text"):
            if value.get(key) not in (None, ""):
                return plain_summary_text(value.get(key))
        return ""
    if isinstance(value, list):
        return "；".join(text for item in value if (text := plain_summary_text(item)))

    text = as_text(value)
    if not text:
        return ""
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|markdown|text)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```[\s\S]*$", "", text)
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None and parsed != value:
        if isinstance(parsed, (dict, list)):
            return plain_summary_text(parsed)
        if isinstance(parsed, str):
            return plain_summary_text(parsed)

    match = re.search(r'["\']summary["\']\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(f'"{match.group(1)}"').strip()
        except json.JSONDecodeError:
            pass
    partial = re.search(r'["\']summary["\']\s*:\s*["\'](.*)$', text, flags=re.DOTALL)
    if partial:
        content = re.split(r'["\']\s*,\s*["\'](?:visible_text|observed_elements|key_moments|uncertainties)["\']', partial.group(1), maxsplit=1)[0]
        content = content.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
        return content.rstrip(' \t\r\n"\'},').strip()
    return text


def note_identity(note: dict[str, Any], source: dict[str, Any], url: str, fallback: str) -> str:
    note_id = as_text(note.get("note_id") or source.get("note_id") or note.get("id") or source.get("id"))
    if note_id:
        return f"id:{note_id.lower()}"
    if url:
        try:
            parsed = urlsplit(url)
            if parsed.path:
                return f"url:{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
        except ValueError:
            pass
    return fallback


def combined_note_fields(note: dict[str, Any]) -> dict[str, Any]:
    """合并搜索结果与可用的公开/创作者详情字段。"""
    source_item = note.get("source_item")
    if not isinstance(source_item, dict):
        source_item = {}
    public_detail = note.get("public_detail")
    if not isinstance(public_detail, dict):
        public_detail = {}
    detail_fields = flatten_fields(public_detail.get("return_value"))
    creator_detail = note.get("creator_note_detail")
    if isinstance(creator_detail, dict) and creator_detail.get("status") == "success":
        creator_fields = flatten_fields(creator_detail.get("return_value"))
        creator_fields.update(detail_fields)
        detail_fields = creator_fields
    combined_fields = flatten_fields(source_item)
    combined_fields.update(detail_fields)
    return combined_fields


def aggregate_run_data(data: dict[str, Any]) -> list[dict[str, str]]:
    """统一关键词和账号搜索结果，并按笔记标识去重。"""
    rows: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    groups = (
        ("keyword_searches", "keyword"),
        ("user_searches", "user"),
    )
    for collection_name, source_type in groups:
        entries = data.get(collection_name, [])
        if not isinstance(entries, list):
            continue
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            theme = as_text(entry.get("keyword")) if source_type == "keyword" else ""
            account = as_text(entry.get("name")) if source_type == "user" else ""
            notes = entry.get("notes", [])
            if not isinstance(notes, list):
                continue
            for note_index, note in enumerate(notes):
                if not isinstance(note, dict):
                    continue
                source_item = note.get("source_item")
                if not isinstance(source_item, dict):
                    source_item = {}
                public_detail = note.get("public_detail")
                if not isinstance(public_detail, dict):
                    public_detail = {}
                combined_fields = combined_note_fields(note)

                url = as_text(field_value(combined_fields, "url") or note.get("url"))
                fallback = f"{collection_name}:{entry_index}:{note_index}"
                identity = note_identity(note, source_item, url, fallback)
                title = as_text(field_value(combined_fields, "title"))
                author = as_text(field_value(combined_fields, "author"))
                topic = f"账号：{account}" if account else theme
                tags = as_text(field_value(combined_fields, "tags"))
                if not tags:
                    tags = as_text(note.get("tags"))
                media_analysis = note.get("media_analysis")
                if not isinstance(media_analysis, dict):
                    media_analysis = {}
                media_visible_text = media_analysis.get("visible_text", [])
                media_key_moments = media_analysis.get("key_moments", [])
                values = {
                    "主题": [topic] if topic else [],
                    "笔记标题": title,
                    "作者": author,
                    "笔记链接": url,
                    "发布时间": as_text(field_value(combined_fields, "published_at") or note.get("published_at")),
                    "正文": as_text(field_value(combined_fields, "content")),
                    "标签": tags,
                    "点赞数": as_text(field_value(combined_fields, "likes")),
                    "收藏数": as_text(field_value(combined_fields, "collects")),
                    "评论数": as_text(field_value(combined_fields, "comments")),
                    "分享数": as_text(field_value(combined_fields, "shares")),
                    "浏览量": as_text(field_value(combined_fields, "views")),
                    "曝光量": as_text(field_value(combined_fields, "exposures")),
                    "观看量": as_text(field_value(combined_fields, "watch_count")),
                    "来源账号": [account] if account else [],
                    "采集排名": as_text(source_item.get("rank") or note.get("rank")),
                    "详情状态": as_text(note.get("detail_status") or public_detail.get("status")),
                    "媒体摘要": plain_summary_text(media_analysis.get("summary")),
                    "画面文字": as_text(media_visible_text),
                    "视频关键点": as_text(media_key_moments),
                    "音频转写": as_text(media_analysis.get("audio_transcript")),
                    "媒体解析状态": as_text(media_analysis.get("status")),
                }
                if identity not in indexes:
                    indexes[identity] = len(rows)
                    rows.append(values)
                    continue

                existing = rows[indexes[identity]]
                for column in ("主题", "来源账号"):
                    append_unique(existing[column], values[column][0] if values[column] else "")
                for column, value in values.items():
                    if column in {"主题", "来源账号"}:
                        continue
                    if not existing.get(column) and value:
                        existing[column] = value
                    elif column == "详情状态" and value == "success":
                        existing[column] = value
                    elif column == "标签" and value and value not in existing[column]:
                        existing[column] = f"{existing[column]}；{value}" if existing[column] else value

    result: list[dict[str, str]] = []
    for row in rows:
        flattened = dict(row)
        flattened["主题"] = "；".join(row["主题"])
        flattened["来源账号"] = "；".join(row["来源账号"])
        result.append({column: as_text(flattened.get(column)) for column in CSV_COLUMNS})
    return result


def safe_csv_cell(value: str) -> str:
    """避免采集文本被电子表格软件当作公式执行。"""
    stripped = value.lstrip(" \t\r\n")
    if stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_path_for(source_path: Path) -> Path:
    if not RUN_FILE_PATTERN.fullmatch(source_path.name):
        raise ValueError("只支持整合 xhs_run_*.json 采集结果文件。")
    suffix = source_path.stem.removeprefix("xhs_run_")
    return source_path.with_name(f"xhs_summary_{suffix}.csv")


def note_matches_filter(note: dict[str, Any], note_filter: dict[str, Any]) -> bool:
    fields = combined_note_fields(note)
    row = {
        "点赞数": as_text(field_value(fields, "likes")),
        "收藏数": as_text(field_value(fields, "collects")),
        "评论数": as_text(field_value(fields, "comments")),
    }
    return _row_matches_normalized_filter(row, normalize_note_filter(note_filter))


def filter_run_data(
    data: dict[str, Any],
    note_filter: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    """复制采集数据并只保留达到指标阈值的笔记。"""
    normalized = normalize_note_filter(note_filter)
    if not normalized["enabled"]:
        raise ValueError("笔记筛选尚未启用，请先添加条件并启用筛选。")

    filtered = copy.deepcopy(data)
    input_count = 0
    output_count = 0
    for collection_name in ("keyword_searches", "user_searches"):
        entries = filtered.get(collection_name, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            notes = entry.get("notes", [])
            if not isinstance(notes, list):
                continue
            kept_notes: list[Any] = []
            for note in notes:
                if not isinstance(note, dict):
                    continue
                input_count += 1
                fields = combined_note_fields(note)
                if _row_matches_normalized_filter(
                    {
                        "点赞数": as_text(field_value(fields, "likes")),
                        "收藏数": as_text(field_value(fields, "collects")),
                        "评论数": as_text(field_value(fields, "comments")),
                    },
                    normalized,
                ):
                    kept_notes.append(note)
                    output_count += 1
            entry["notes"] = kept_notes
    return filtered, {"input_note_count": input_count, "output_note_count": output_count}


def filter_result_file(
    source_path: Path,
    output_dir: Path,
    note_filter: dict[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, int]]:
    """将筛选结果保存为新的 xhs_run_*.json，保留原始采集文件。"""
    source_path = source_path.resolve()
    if not source_path.is_file() or not RUN_FILE_PATTERN.fullmatch(source_path.name):
        raise FileNotFoundError("采集结果文件不存在，或文件名不是 xhs_run_*.json。")
    try:
        data = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列。") from exc
    if not isinstance(data, dict):
        raise ValueError("采集结果 JSON 顶层必须是对象。")

    normalized = normalize_note_filter(note_filter)
    filtered, counts = filter_run_data(data, normalized)
    output_dir.mkdir(parents=True, exist_ok=True)
    current = datetime.now(timezone(timedelta(hours=8)))
    run_id = current.strftime("%Y%m%d_%H%M%S.%f")
    output_path = output_dir / f"xhs_run_{run_id}.json"
    suffix = 1
    while output_path.exists():
        current += timedelta(microseconds=suffix)
        run_id = current.strftime("%Y%m%d_%H%M%S.%f")
        output_path = output_dir / f"xhs_run_{run_id}.json"
        suffix += 1

    filtered["run_id"] = run_id
    filtered["started_at"] = current.isoformat(timespec="milliseconds")
    filtered["finished_at"] = current.isoformat(timespec="milliseconds")
    filtered["status"] = "succeeded"
    filtered["operation"] = "note_filter"
    filtered["filter_source"] = {
        "file": source_path.name,
        "run_id": data.get("run_id"),
        "status": data.get("status"),
        "source_error_count": len(data.get("errors", [])) if isinstance(data.get("errors"), list) else 0,
        "source_warning_count": len(data.get("warnings", [])) if isinstance(data.get("warnings"), list) else 0,
        "input_note_count": counts["input_note_count"],
        "output_note_count": counts["output_note_count"],
    }
    inputs = filtered.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
        filtered["inputs"] = inputs
    filter_inputs = inputs.get("filters")
    if not isinstance(filter_inputs, dict):
        filter_inputs = {}
        inputs["filters"] = filter_inputs
    filter_inputs["csv"] = {"note_filter": normalized}
    filtered["summary_note_count"] = counts["output_note_count"]
    filtered["errors"] = []
    filtered["warnings"] = []
    filtered.pop("summary_csv", None)
    filtered.pop("csv_stale", None)
    filtered.pop("workflow_execution", None)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path, filtered, counts


def merge_result_files(
    source_paths: list[Path],
    output_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """把多份采集或筛选 JSON 合并成一份新的采集结果。"""
    if not source_paths:
        raise ValueError("至少需要一份采集结果 JSON。")

    resolved_paths = [path.resolve() for path in source_paths]
    source_data: list[tuple[Path, dict[str, Any]]] = []
    for source_path in resolved_paths:
        if not source_path.is_file() or not RUN_FILE_PATTERN.fullmatch(source_path.name):
            raise FileNotFoundError("采集结果文件不存在，或文件名不是 xhs_run_*.json。")
        try:
            data = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列。") from exc
        if not isinstance(data, dict):
            raise ValueError("采集结果 JSON 顶层必须是对象。")
        source_data.append((source_path, data))

    if len(source_data) == 1:
        return source_data[0]

    output_dir.mkdir(parents=True, exist_ok=True)
    current = datetime.now(timezone(timedelta(hours=8)))
    run_id = current.strftime("%Y%m%d_%H%M%S.%f")
    output_path = output_dir / f"xhs_run_{run_id}.json"
    collision = 1
    while output_path.exists():
        current += timedelta(microseconds=collision)
        run_id = current.strftime("%Y%m%d_%H%M%S.%f")
        output_path = output_dir / f"xhs_run_{run_id}.json"
        collision += 1

    merged: dict[str, Any] = {
        "run_id": run_id,
        "started_at": current.isoformat(timespec="milliseconds"),
        "finished_at": current.isoformat(timespec="milliseconds"),
        "status": "succeeded",
        "operation": "merge_results",
        "inputs": {},
        "keyword_searches": [],
        "user_searches": [],
        "opencli": [],
        "media_commands": [],
        "errors": [],
        "warnings": [],
        "merged_sources": [],
    }
    for source_path, data in source_data:
        for collection_name in ("keyword_searches", "user_searches", "opencli", "media_commands", "errors", "warnings"):
            values = data.get(collection_name, [])
            if isinstance(values, list):
                merged[collection_name].extend(copy.deepcopy(values))
        merged["merged_sources"].append({
            "file": source_path.name,
            "run_id": data.get("run_id"),
            "status": data.get("status"),
        })
        if data.get("status") not in {None, "succeeded"}:
            merged["status"] = "partial"

    merged["input_note_count"] = sum(
        len(entry.get("notes", []))
        for collection_name in ("keyword_searches", "user_searches")
        for entry in merged[collection_name]
        if isinstance(entry, dict) and isinstance(entry.get("notes"), list)
    )
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path, merged


def aggregate_result_file(source_path: Path) -> tuple[Path, list[dict[str, str]]]:
    source_path = source_path.resolve()
    if not source_path.is_file() or not RUN_FILE_PATTERN.fullmatch(source_path.name):
        raise FileNotFoundError("采集结果文件不存在，或文件名不是 xhs_run_*.json。")
    try:
        data = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列。") from exc
    if not isinstance(data, dict):
        raise ValueError("采集结果 JSON 顶层必须是对象。")

    rows = aggregate_run_data(data)
    csv_path = csv_path_for(source_path)
    temporary_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: safe_csv_cell(value) for key, value in row.items()} for row in rows)
    temporary_path.replace(csv_path)
    return csv_path, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="把一次小红书采集 JSON 整理为 CSV。")
    parser.add_argument("result_json", type=Path, help="xhs_run_*.json 文件路径")
    args = parser.parse_args()
    try:
        csv_path, rows = aggregate_result_file(args.result_json)
    except (OSError, ValueError) as exc:
        print(f"整合失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"csv_file": str(csv_path), "note_count": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
