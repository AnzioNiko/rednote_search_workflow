#!/usr/bin/env python3
"""按点赞、收藏和评论阈值筛选一次采集 JSON，并生成新的结果文件。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.aggregate_results import filter_result_file, normalize_note_filter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="按指标条件筛选小红书采集 JSON。")
    parser.add_argument("result_json", type=Path, help="待筛选的 xhs_run_*.json 文件路径")
    parser.add_argument(
        "--filters",
        type=Path,
        default=ROOT / "input" / "filters.json",
        help="筛选配置路径，默认 input/filters.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output",
        help="筛选结果保存目录，默认 output",
    )
    args = parser.parse_args()
    try:
        filters_payload = json.loads(args.filters.read_text(encoding="utf-8-sig"))
        if not isinstance(filters_payload, dict):
            raise ValueError("筛选配置顶层必须是 JSON 对象。")
        csv_settings = filters_payload.get("csv", {})
        if not isinstance(csv_settings, dict):
            raise ValueError("筛选配置 csv 必须是 JSON 对象。")
        note_filter = normalize_note_filter(csv_settings.get("note_filter"))
        output_path, _, counts = filter_result_file(args.result_json, args.output_dir, note_filter)
    except (OSError, ValueError) as exc:
        print(f"筛选失败：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output_file": str(output_path),
                "input_note_count": counts["input_note_count"],
                "output_note_count": counts["output_note_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
