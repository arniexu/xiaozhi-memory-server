from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from knowledge_agent_service import api


class SearchApiTests(unittest.TestCase):
    def test_graph_failure_preserves_local_search_results(self) -> None:
        memory_store = Mock()
        memory_store.search.return_value = [{"id": "lexical-1"}]
        memory_store.get_many.return_value = [{"id": "semantic-1"}]
        vector_store = Mock()
        vector_store.search.return_value = [{"source_id": "semantic-1"}]
        graph_store = Mock()
        graph_store.search.side_effect = RuntimeError("graph unavailable")

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=True)),
            patch.object(api, "memories", memory_store),
            patch.object(api, "vectors", vector_store),
            patch.object(api, "graph", graph_store),
        ):
            result = api.search(
                api.SearchRequest(query="reset", embedding=[1.0], limit=3)
            )

        self.assertEqual(result["lexical"], [{"id": "lexical-1"}])
        self.assertEqual(result["semantic"], [{"source_id": "semantic-1"}])
        self.assertEqual(result["semantic_memories"], [{"id": "semantic-1"}])
        self.assertEqual(result["graph"], [])
        self.assertEqual(result["graph_error"], "RuntimeError")

    def test_continuity_import_forwards_source_scope_and_embeddings(self) -> None:
        configured = SimpleNamespace(neo4j_configured=False)
        request = api.ContinuityImportRequest(
            decisions=[{"id": "decision-1", "question": "Why?"}],
            embeddings=[{"decisionId": "decision-1", "values": [1.0]}],
            source=".continuity/decisions.json",
            workspace_id="workspace-1",
            repo_id="repo-1",
        )
        with (
            patch.object(api, "settings", configured),
            patch.object(api, "migrate_continuity_data", return_value={"created": 1}) as migrate,
        ):
            result = api.import_continuity(request)

        self.assertEqual(result, {"created": 1})
        migrate.assert_called_once_with(
            request.decisions,
            configured,
            source=request.source,
            workspace_id=request.workspace_id,
            repo_id=request.repo_id,
            embeddings=request.embeddings,
            sync_neo4j=False,
        )

    def test_document_import_forwards_scope_and_embeddings(self) -> None:
        configured = SimpleNamespace(neo4j_configured=True)
        request = api.DocumentImportRequest(
            documents=[{"id": "document:1", "filename": "PAS.pdf"}],
            embeddings=[{"chunkId": "chunk:1", "values": [1.0]}],
            workspace_id="workspace-1",
            repo_id="repo-1",
        )
        with (
            patch.object(api, "settings", configured),
            patch.object(api, "migrate_document_data", return_value={"created": 1}) as migrate,
        ):
            result = api.import_documents(request)

        self.assertEqual(result, {"created": 1})
        migrate.assert_called_once_with(
            request.documents,
            configured,
            workspace_id=request.workspace_id,
            repo_id=request.repo_id,
            embeddings=request.embeddings,
            sync_neo4j=True,
        )


if __name__ == "__main__":
    unittest.main()