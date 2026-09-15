from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from knowledge_agent_service import api


class SearchApiTests(unittest.TestCase):
    def test_graph_failure_preserves_local_search_results(self) -> None:
        memory_store = Mock()
        memory_store.search_with_meta.return_value = {
            "results": [{"id": "lexical-1"}],
            "scope_match": "exact",
            "scope_suppressed": 0,
            "match_mode": "all",
            "session_scope": "prefer",
        }
        memory_store.get_many.return_value = [{"id": "semantic-1"}]
        vector_store = Mock()
        vector_store.search.return_value = [{"source_id": "semantic-1", "score": 0.9}]
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
        self.assertEqual(result["semantic"], [{"source_id": "semantic-1", "score": 0.9}])
        self.assertEqual(result["semantic_memories"], [{"id": "semantic-1"}])
        self.assertEqual(result["graph"], [])
        self.assertEqual(result["graph_error"], "RuntimeError")
        self.assertEqual(result["scope_match"], "exact")
        self.assertEqual(result["semantic_suppressed"], 0)

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


class SemanticLifecycleTests(unittest.TestCase):
    def test_retired_semantic_hits_are_suppressed_and_counted(self) -> None:
        memory_store = Mock()
        memory_store.search_with_meta.return_value = {
            "results": [],
            "scope_match": "global",
            "scope_suppressed": 0,
            "match_mode": "any",
            "session_scope": "prefer",
        }
        memory_store.get_many.return_value = []
        vector_store = Mock()
        vector_store.search.return_value = [
            {"source_id": "continuity:test-2026-05-27-01", "score": 1.0},
            {"source_id": "memory:live", "score": 0.5},
        ]

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=False)),
            patch.object(api, "memories", memory_store),
            patch.object(api, "vectors", vector_store),
        ):
            result = api.search(api.SearchRequest(query="continuity write path", embedding=[1.0], limit=4))

        self.assertEqual(result["semantic_memories"], [])
        self.assertEqual(result["semantic_suppressed"], 2)
        memory_store.get_many.assert_called_once_with(
            ["continuity:test-2026-05-27-01", "memory:live"], searchable_only=True
        )

    def test_semantic_hits_below_the_similarity_floor_are_dropped(self) -> None:
        memory_store = Mock()
        memory_store.search_with_meta.return_value = {
            "results": [],
            "scope_match": "global",
            "scope_suppressed": 0,
            "match_mode": "any",
            "session_scope": "prefer",
        }
        memory_store.get_many.return_value = []
        vector_store = Mock()
        vector_store.search.return_value = [
            {"source_id": "memory:noise", "score": 0.22},
            {"source_id": "memory:signal", "score": 0.51},
        ]

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=False)),
            patch.object(api, "memories", memory_store),
            patch.object(api, "vectors", vector_store),
        ):
            result = api.search(api.SearchRequest(query="tsod", embedding=[1.0], limit=4, min_similarity=0.35))

        self.assertEqual([item["source_id"] for item in result["semantic"]], ["memory:signal"])


class GraphApiTests(unittest.TestCase):
    """The graph endpoints write additively, so their defaults are part of the contract."""

    def test_search_scopes_document_links_to_entities_the_query_matched(self) -> None:
        memory_store = Mock()
        memory_store.search_with_meta.return_value = {
            "results": [],
            "scope_match": "global",
            "scope_suppressed": 0,
            "match_mode": "any",
            "session_scope": "prefer",
        }
        memory_store.get_many.return_value = []
        graph_store = Mock()
        graph_store.search.return_value = [{"id": "entity-1", "type": "graph:KnowledgeEntity"}]
        graph_store.document_links.return_value = [{"id": "document-link-1"}]

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=True)),
            patch.object(api, "memories", memory_store),
            patch.object(api, "graph", graph_store),
        ):
            result = api.search(api.SearchRequest(query="tsod", limit=4))

        self.assertEqual(
            [item["id"] for item in result["graph"]],
            ["entity-1", "document-link-1"],
        )
        graph_store.document_links.assert_called_once_with(limit=4, entity_ids=["entity-1"])

    def test_search_injects_no_document_link_when_query_matches_no_entity(self) -> None:
        memory_store = Mock()
        memory_store.search_with_meta.return_value = {
            "results": [],
            "scope_match": "global",
            "scope_suppressed": 0,
            "match_mode": "any",
            "session_scope": "prefer",
        }
        memory_store.get_many.return_value = []
        graph_store = Mock()
        graph_store.search.return_value = []
        graph_store.document_links.return_value = []

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=True)),
            patch.object(api, "memories", memory_store),
            patch.object(api, "graph", graph_store),
        ):
            result = api.search(api.SearchRequest(query="zzz qqq", limit=4))

        self.assertEqual(result["graph"], [])
        graph_store.document_links.assert_called_once_with(limit=4, entity_ids=[])

    def test_relink_defaults_to_dry_run_and_forwards_quality_switches(self) -> None:
        graph_store = Mock()
        graph_store.relink_documents.return_value = {"dry_run": True}

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=True)),
            patch.object(api, "graph", graph_store),
        ):
            result = api.relink_documents(api.RelinkRequest())

        self.assertEqual(result, {"dry_run": True})
        graph_store.relink_documents.assert_called_once_with(
            "",
            dry_run=True,
            include_tags=False,
            max_alias_document_frequency=None,
        )

    def test_relink_skips_without_neo4j_instead_of_writing(self) -> None:
        graph_store = Mock()

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=False)),
            patch.object(api, "graph", graph_store),
        ):
            result = api.relink_documents(api.RelinkRequest(dry_run=False))

        self.assertEqual(result, {"skipped": True, "reason": "neo4j-not-configured"})
        graph_store.relink_documents.assert_not_called()

    def test_relink_reports_the_failure_type_instead_of_raising(self) -> None:
        graph_store = Mock()
        graph_store.relink_documents.side_effect = RuntimeError("neo4j down")

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=True)),
            patch.object(api, "graph", graph_store),
        ):
            result = api.relink_documents(api.RelinkRequest(dry_run=False))

        self.assertEqual(result, {"error": "RuntimeError"})

    def test_document_links_returns_links_with_a_count(self) -> None:
        graph_store = Mock()
        graph_store.document_links.return_value = [{"id": "document:aaa<->document:bbb"}]

        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=True)),
            patch.object(api, "graph", graph_store),
        ):
            result = api.document_links(api.RelinkRequest(document_id="document:aaa"))

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["links"], [{"id": "document:aaa<->document:bbb"}])
        graph_store.document_links.assert_called_once_with("document:aaa")

    def test_document_links_degrades_to_an_empty_list_without_neo4j(self) -> None:
        with (
            patch.object(api, "settings", SimpleNamespace(neo4j_configured=False)),
            patch.object(api, "graph", Mock()),
        ):
            result = api.document_links(api.RelinkRequest())

        self.assertEqual(result["links"], [])
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["skipped"])


if __name__ == "__main__":
    unittest.main()