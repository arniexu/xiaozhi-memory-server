import tempfile
import unittest
from pathlib import Path

from knowledge_agent_service.memory_store import MemoryStore, ascii_terms
from knowledge_agent_service.scope import canonical_workspace_id, workspace_aliases


class MemoryStoreTests(unittest.TestCase):
    def store(self, directory: str) -> MemoryStore:
        root = Path(directory)
        return MemoryStore(root / "memory.sqlite3", root / "events.jsonl")

    def active(self, store: MemoryStore, summary: str, **overrides) -> dict:
        unit = {
            "type": "fact",
            "summary": summary,
            "resolution": f"resolution for {summary}",
            "status": "active",
            "memory_kind": "memory",
            "evidence": [{"kind": "test"}],
        }
        unit.update(overrides)
        return store.upsert(unit)

    def test_lifecycle_and_scope_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            draft = store.upsert({"type": "fact", "summary": "Hidden candidate", "status": "draft", "memory_kind": "candidate", "session_id": "s1"})
            self.assertEqual(store.search("Hidden", session_id="s1"), [])
            active = self.active(store, "Verified BMC fact", session_id="s1")
            self.assertEqual([item["id"] for item in store.search("Verified", session_id="s1")], [active["id"]])
            # 新语义：prefer 允许跨会话召回，strict 保留旧的硬隔离行为。
            self.assertEqual([item["id"] for item in store.search("Verified", session_id="s2")], [active["id"]])
            self.assertEqual(store.search("Verified", session_id="s2", session_scope="strict"), [])
            self.assertEqual(store.search("Verified", session_id="s1", session_scope="strict")[0]["id"], active["id"])
            self.assertNotEqual(draft["id"], active["id"])
            self.assertEqual(store.stats()["continuity"], 0)
            self.assertEqual(store.stats()["searchable"], 1)
            info = store.database_info()
            self.assertEqual(info["engine"], "SQLite")
            self.assertEqual(info["path"], str(Path(directory) / "memory.sqlite3"))
            self.assertGreater(info["size_bytes"], 0)
            self.assertEqual(info["features"], ["FTS5", "WAL", "CJK-char-index"])

    def test_workspace_alias_expansion_reaches_uri_scoped_records(self) -> None:
        """写入用 file:// URI，读取用扩展的 20 位哈希，两者必须命中同一批记录。"""
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            uri = "file:///home/xuqj/openbmc-openbmc"
            digest = canonical_workspace_id(uri)
            self.assertEqual(digest, "f378c72e6fdb2270b506")
            record = self.active(store, "URI scoped OpenBMC fact", workspace_id=uri)
            # 扩展必须同时给出 hash 与 URI 别名，才能精确命中（hash 不可逆）。
            hit = store.search_with_meta("URI scoped", workspace_id=digest, workspace_ids=[uri])
            self.assertEqual([item["id"] for item in hit["results"]], [record["id"]])
            self.assertEqual(hit["scope_match"], "exact")
            # 只给 hash 时仍能通过降级路径拿到结果，但会显式标注 relaxed。
            relaxed = store.search_with_meta("URI scoped", workspace_id=digest)
            self.assertEqual([item["id"] for item in relaxed["results"]], [record["id"]])
            self.assertEqual(relaxed["scope_match"], "relaxed")
            self.assertIn(uri, workspace_aliases(digest, uri))

    def test_suffixed_workspace_hashes_are_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            base = "1aa407ad66f59da7c4dd67c0be6d1c7d"
            record = self.active(store, "Suffixed window fact", workspace_id=f"{base}-1")
            self.assertEqual(store.search("Suffixed", workspace_id=base)[0]["id"], record["id"])

    def test_scope_relaxation_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            record = self.active(store, "Other project fact", workspace_id="some-other-project")
            relaxed = store.search_with_meta("Other project", workspace_id="f378c72e6fdb2270b506")
            self.assertEqual([item["id"] for item in relaxed["results"]], [record["id"]])
            self.assertEqual(relaxed["scope_match"], "relaxed")
            self.assertEqual(relaxed["scope_suppressed"], 1)
            strict = store.search_with_meta("Other project", workspace_id="f378c72e6fdb2270b506", scope_fallback="off")
            self.assertEqual(strict["results"], [])
            self.assertEqual(strict["scope_match"], "exact")

    def test_in_scope_results_outrank_global_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            local = self.active(store, "shared topic local note", workspace_id="local-workspace")
            self.active(store, "shared topic global note", workspace_id="far-away-workspace")
            hit = store.search_with_meta("shared topic", workspace_id="local-workspace")
            self.assertEqual(hit["results"][0]["id"], local["id"])
            self.assertEqual(hit["scope_match"], "mixed")
            self.assertEqual(hit["scope_suppressed"], 1)

    def test_cjk_character_index_matches_short_chinese_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            record = self.active(
                store,
                "阈值与时间窗口必须可配置",
                resolution="已确认需要将阈值和时间窗口设为可配置，并确保掉电不丢失。",
            )
            for query in ("阈值", "时间窗口", "阈值 时间窗口 掉电不丢失"):
                self.assertEqual([item["id"] for item in store.search(query)], [record["id"]], query)
            self.assertEqual(store.search("面包"), [])

    def test_cjk_index_backfill_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.active(store, "回填寄存器读数证据")
            self.assertEqual(store.backfill_cjk_index(dry_run=True)["planned"], 0)
            self.assertEqual(store.backfill_cjk_index()["indexed"], 1)
            self.assertEqual(store.backfill_cjk_index(dry_run=True)["planned"], 0)

    def test_all_terms_preferred_over_any_term(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            both = self.active(store, "alpha beta register evidence")
            only_one = self.active(store, "alpha only evidence")
            hit = store.search_with_meta("alpha beta")
            self.assertEqual(hit["results"][0]["id"], both["id"])
            # AND 与 OR 并联：AND 命中时 OR 通道仍在，只有 alpha 的记录不会被丢。
            self.assertIn("all", hit["match_mode"])
            self.assertEqual({item["id"] for item in hit["results"]}, {both["id"], only_one["id"]})
            fallback = store.search_with_meta("alpha gamma")
            self.assertEqual(fallback["match_mode"], "any")
            self.assertEqual({item["id"] for item in fallback["results"]}, {both["id"], only_one["id"]})

    def test_document_quota_limits_one_document_in_the_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            for page in range(1, 5):
                self.active(store, f"handbook.pdf - page {page}", resolution=f"quota evidence page {page}")
            for page in range(1, 3):
                self.active(store, f"other.pdf - page {page}", resolution=f"quota evidence other {page}")
            results = store.search("quota evidence", limit=6)
            head = [item["summary"] for item in results[:4]]
            self.assertEqual(sum(1 for item in head if item.startswith("handbook.pdf")), 2)

    def test_get_many_searchable_filter_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            retired = store.upsert(
                {
                    "type": "fact",
                    "summary": "Continuity write path healthy?",
                    "resolution": "deprecated probe",
                    "status": "deprecated",
                    "memory_kind": "memory",
                    "evidence": [{"kind": "test"}],
                }
            )
            self.assertEqual(len(store.get_many([retired["id"]])), 1)
            self.assertEqual(store.get_many([retired["id"]], searchable_only=True), [])


    def test_ascii_terms_exclude_cjk(self) -> None:
        """中文不能再被当成 ASCII 词，否则 AND 门会把手册页整批排除。"""
        self.assertEqual(ascii_terms("TSOD 温度阈值"), ["TSOD"])
        self.assertEqual(ascii_terms("PECI 温度读取"), ["PECI"])
        self.assertEqual(ascii_terms("纯中文查询"), [])
        self.assertEqual(ascii_terms("CPLD register 0x24"), ["CPLD", "register", "0x24"])

    def test_mixed_query_keeps_ascii_evidence_without_the_chinese_phrase(self) -> None:
        """手册页只有英文词，中文短语缺席时不得把结果清空。"""
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            page = self.active(store, "hotbook.pdf - page 7", resolution="TSOD thermal sensor definition on DIMM")
            self.active(store, "温度阈值相关的一条记忆", resolution="仅供干扰")
            hit = store.search_with_meta("TSOD 温度阈值")
            self.assertIn(page["id"], [item["id"] for item in hit["results"]])
            self.assertIn("any", hit["match_mode"])

    def test_pure_cjk_query_still_gates_on_the_cjk_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            self.active(store, "alpha beta register evidence")
            hit = store.search_with_meta("量子纠缠退相干")
            self.assertEqual(hit["results"], [])
            self.assertEqual(hit["match_mode"], "none")


if __name__ == "__main__":
    unittest.main()
