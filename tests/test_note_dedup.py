from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.note_dedup import SEEN_FILE_HEADER, SEEN_KEY_PATTERN, SeenNoteStore, note_dedup_key
from scripts.run_xhs_workflow import WorkflowError, enrich_note_rows, normalize_filters


class NoteDedupTests(unittest.TestCase):
    def test_note_key_prefers_platform_id_and_ignores_url_query(self) -> None:
        note_id = "0123456789abcdef01234567"
        from_field = note_dedup_key({"note_id": note_id.upper()})
        from_url = note_dedup_key(
            {},
            f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=first",
        )
        from_same_url_with_other_query = note_dedup_key(
            {},
            f"https://xiaohongshu.com/explore/{note_id}/?xsec_token=second",
        )
        self.assertEqual(from_field, f"i:{note_id}")
        self.assertEqual(from_field, from_url)
        self.assertEqual(from_url, from_same_url_with_other_query)

    def test_url_fallback_key_is_compact_and_stable(self) -> None:
        first = note_dedup_key(
            {},
            "https://www.xiaohongshu.com/discovery/item/not-a-platform-id?xsec_token=a",
        )
        second = note_dedup_key(
            {},
            "https://xiaohongshu.com/discovery/item/not-a-platform-id/?xsec_token=b",
        )
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertRegex(first or "", SEEN_KEY_PATTERN)
        self.assertLessEqual(len(first or ""), 25)

    def test_seen_store_round_trips_compact_sorted_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "seen_notes.txt"
            path.write_text(f"{SEEN_FILE_HEADER}\ninvalid-line\n", encoding="utf-8")
            store = SeenNoteStore(path)
            self.assertEqual(store.invalid_line_count, 1)

            first_key = "i:0123456789abcdef01234567"
            second_key = "i:abcdefabcdefabcdefabcdef"
            self.assertTrue(store.record(first_key))
            self.assertFalse(store.record(first_key))
            self.assertTrue(store.record(second_key))
            self.assertTrue(store.save())

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, [SEEN_FILE_HEADER, first_key, second_key])
            reloaded = SeenNoteStore(path)
            self.assertEqual(reloaded.keys, {first_key, second_key})
            self.assertFalse(reloaded.save())
            self.assertFalse(reloaded.record(second_key))

    def test_filter_config_normalizes_deduplication_settings(self) -> None:
        filters = normalize_filters({
            "deduplication": {
                "enabled": True,
                "seen_file": "custom_seen.txt",
            }
        })
        self.assertEqual(
            filters["deduplication"],
            {"enabled": True, "seen_file": "custom_seen.txt"},
        )
        with self.assertRaises(WorkflowError):
            normalize_filters({
                "deduplication": {
                    "enabled": True,
                    "seen_file": "../outside.txt",
                }
            })

    def test_enrichment_skips_duplicates_before_reading_details(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, arguments: list[str]) -> dict:
                self.calls += 1
                return {
                    "status": "success",
                    "return_value": [{"field": "content", "value": "正文"}],
                }

        first_id = "0123456789abcdef01234567"
        second_id = "abcdefabcdefabcdefabcdef"
        rows = [
            {"url": f"https://xiaohongshu.com/explore/{first_id}"},
            {"url": f"https://www.xiaohongshu.com/explore/{first_id}?xsec_token=other"},
            {"url": f"https://xiaohongshu.com/explore/{second_id}"},
        ]
        stats = {"recorded_count": 0, "duplicate_count": 0, "unkeyed_count": 0}
        run_data = {"warnings": [], "errors": []}
        with tempfile.TemporaryDirectory() as temporary:
            store = SeenNoteStore(Path(temporary) / "seen_notes.txt")
            client = FakeClient()
            enriched = enrich_note_rows(
                client,
                rows,
                3,
                run_data,
                "test",
                False,
                store,
                stats,
            )

        self.assertEqual(len(enriched), 2)
        self.assertEqual(client.calls, 2)
        self.assertEqual(
            stats,
            {"recorded_count": 2, "duplicate_count": 1, "unkeyed_count": 0},
        )


if __name__ == "__main__":
    unittest.main()
