from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.aggregate_results import (
    aggregate_result_file,
    filter_result_file,
    filter_run_data,
    merge_result_files,
    normalize_note_filter,
    parse_metric_number,
    row_matches_note_filter,
)


class NoteFilterTests(unittest.TestCase):
    def test_parse_metric_number_supports_chinese_units(self) -> None:
        self.assertEqual(parse_metric_number("7.5万"), 75_000)
        self.assertEqual(parse_metric_number("1.2w+"), 12_000)
        self.assertEqual(parse_metric_number("3千"), 3_000)
        self.assertIsNone(parse_metric_number("暂无"))

    def test_filter_groups_use_and_within_group_and_or_between_groups(self) -> None:
        note_filter = normalize_note_filter({
            "enabled": True,
            "groups": [
                {
                    "conditions": [
                        {"field": "likes", "operator": "gt", "value": 1000},
                        {"field": "comments", "operator": "gt", "value": 50},
                    ]
                },
                {
                    "conditions": [
                        {"field": "likes", "operator": "gt", "value": 10000},
                    ]
                },
            ],
        })
        self.assertTrue(row_matches_note_filter({"点赞数": "7.5万", "收藏数": "100", "评论数": "60"}, note_filter))
        self.assertTrue(row_matches_note_filter({"点赞数": "2万", "收藏数": "100", "评论数": "10"}, note_filter))
        self.assertFalse(row_matches_note_filter({"点赞数": "9000", "收藏数": "100", "评论数": "40"}, note_filter))
        self.assertFalse(row_matches_note_filter({"点赞数": "", "收藏数": "100", "评论数": "60"}, note_filter))

    def test_filter_run_data_counts_notes_and_keeps_source_unchanged(self) -> None:
        source = self.sample_run_data()
        note_filter = normalize_note_filter({
            "enabled": True,
            "groups": [{
                "conditions": [
                    {"field": "likes", "operator": "gte", "value": 10000},
                    {"field": "collects", "operator": "gt", "value": 1000},
                ]
            }],
        })
        filtered, counts = filter_run_data(source, note_filter)
        self.assertEqual(counts, {"input_note_count": 4, "output_note_count": 2})
        self.assertEqual(len(source["keyword_searches"][0]["notes"]), 4)
        self.assertEqual(len(filtered["keyword_searches"][0]["notes"]), 2)
        self.assertEqual(filtered["keyword_searches"][0]["notes"][0]["note_id"], "note-a")
        self.assertEqual(filtered["keyword_searches"][0]["notes"][1]["note_id"], "note-b")

    def test_filtered_result_can_be_aggregated_without_overwriting_source(self) -> None:
        source = self.sample_run_data()
        note_filter = normalize_note_filter({
            "enabled": True,
            "groups": [{
                "conditions": [{"field": "comments", "operator": "gt", "value": 50}]
            }],
        })
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source_path = directory / "xhs_run_20260101_120000.000000.json"
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            output_path, filtered, counts = filter_result_file(source_path, directory, note_filter)
            self.assertTrue(source_path.is_file())
            self.assertNotEqual(output_path, source_path)
            self.assertEqual(filtered["operation"], "note_filter")
            self.assertEqual(counts["output_note_count"], 3)

            csv_path, rows = aggregate_result_file(output_path)
            self.assertEqual(len(rows), 3)
            with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))
            self.assertEqual(len(csv_rows), 3)

    def test_merge_result_files_combines_sources_and_deduplicates_csv_notes(self) -> None:
        first = self.sample_run_data()
        first["status"] = "succeeded"
        second = self.sample_run_data()
        second["run_id"] = "20260101_130000.000000"
        second["status"] = "partial"
        second["keyword_searches"][0]["keyword"] = "第二个关键词"
        second["keyword_searches"][0]["notes"] = second["keyword_searches"][0]["notes"][:2]
        second["keyword_searches"][0]["notes"].append({
            "note_id": "note-e",
            "source_item": {
                "note_id": "note-e",
                "likes": "30000",
                "collects": "20",
                "comments": "100",
            },
            "public_detail": {"status": "success", "return_value": {}},
        })

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first_path = directory / "xhs_run_20260101_120000.000000.json"
            second_path = directory / "xhs_run_20260101_130000.000000.json"
            first_path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
            second_path.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")

            merged_path, merged = merge_result_files([first_path, second_path], directory)
            self.assertNotIn(merged_path, {first_path, second_path})
            self.assertEqual(merged["status"], "partial")
            self.assertEqual(merged["input_note_count"], 7)
            self.assertEqual(len(merged["merged_sources"]), 2)

            csv_path, rows = aggregate_result_file(merged_path)
            self.assertEqual(len(rows), 5)
            with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))
            self.assertEqual(len(csv_rows), 5)

            note_filter = normalize_note_filter({
                "enabled": True,
                "groups": [{
                    "conditions": [
                        {"field": "likes", "operator": "gte", "value": 10000},
                        {"field": "comments", "operator": "gt", "value": 50},
                    ]
                }],
            })
            filtered_path, _, counts = filter_result_file(merged_path, directory, note_filter)
            self.assertEqual(counts, {"input_note_count": 7, "output_note_count": 3})
            _, filtered_rows = aggregate_result_file(filtered_path)
            self.assertEqual(len(filtered_rows), 2)

    @staticmethod
    def sample_run_data() -> dict:
        def note(note_id: str, likes: str, collects: str, comments: str) -> dict:
            source_item = {
                "note_id": note_id,
                "xsec_token": "token",
                "likes": likes,
                "collects": collects,
                "comments": comments,
            }
            return {
                "note_id": note_id,
                "source_item": source_item,
                "public_detail": {"status": "success", "return_value": source_item},
            }

        return {
            "run_id": "20260101_120000.000000",
            "status": "succeeded",
            "inputs": {"filters": {}},
            "keyword_searches": [{
                "keyword": "测试",
                "notes": [
                    note("note-a", "7.5万", "2000", "60"),
                    note("note-b", "2万", "3000", "10"),
                    note("note-c", "9000", "2000", "100"),
                    note("note-d", "", "5000", "100"),
                ],
            }],
            "user_searches": [],
            "errors": [],
            "warnings": [],
        }


if __name__ == "__main__":
    unittest.main()
