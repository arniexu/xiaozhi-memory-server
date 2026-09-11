import tempfile
import unittest
from pathlib import Path

from knowledge_agent_service.vector_store import VectorStore


class VectorStoreTests(unittest.TestCase):
    def test_vector_search_and_idempotent_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = VectorStore(Path(directory) / "vectors.sqlite3")
            store.upsert("a", "knowledge", [1.0, 0.0], {"label": "A"}, "now")
            store.upsert("b", "knowledge", [0.0, 1.0], {"label": "B"}, "now")
            store.upsert("a", "knowledge", [1.0, 0.0], {"label": "A2"}, "later")
            self.assertEqual(store.stats(), {"total": 2, "dimensions": 2})
            self.assertEqual(store.search([0.9, 0.1], limit=1)[0]["source_id"], "a")
            info = store.database_info()
            self.assertEqual(info["engine"], "SQLite")
            self.assertEqual(info["path"], str(Path(directory) / "vectors.sqlite3"))
            self.assertGreater(info["size_bytes"], 0)
            self.assertEqual(info["features"], ["Exact cosine", "WAL"])


if __name__ == "__main__":
    unittest.main()