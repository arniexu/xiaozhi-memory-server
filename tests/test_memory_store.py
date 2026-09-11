import tempfile
import unittest
from pathlib import Path

from knowledge_agent_service.memory_store import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def test_lifecycle_and_scope_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "memory.sqlite3", root / "events.jsonl")
            draft = store.upsert({"type": "fact", "summary": "Hidden candidate", "status": "draft", "memory_kind": "candidate", "session_id": "s1"})
            self.assertEqual(store.search("Hidden", session_id="s1"), [])
            active = store.upsert({"type": "fact", "summary": "Verified BMC fact", "status": "active", "memory_kind": "memory", "evidence": [{"kind": "test"}], "session_id": "s1"})
            self.assertEqual([item["id"] for item in store.search("Verified", session_id="s1")], [active["id"]])
            self.assertEqual(store.search("Verified", session_id="s2"), [])
            self.assertNotEqual(draft["id"], active["id"])
            self.assertEqual(store.stats()["continuity"], 0)
            info = store.database_info()
            self.assertEqual(info["engine"], "SQLite")
            self.assertEqual(info["path"], str(root / "memory.sqlite3"))
            self.assertGreater(info["size_bytes"], 0)
            self.assertEqual(info["features"], ["FTS5", "WAL"])


if __name__ == "__main__":
    unittest.main()