from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_agent_service import api
from knowledge_agent_service.capabilities import (
    CAPABILITY_KIND,
    SkillProvider,
    build_skill_manifest,
    is_publishable,
)
from knowledge_agent_service.memory_store import MemoryStore


def skill_unit(**overrides: object) -> dict:
    unit: dict = {
        "id": "skill:cpld-recovery",
        "type": "workflow",
        "summary": "CPLD recovery",
        "resolution": "How to recover a failed CPLD update",
        "status": "active",
        "memory_kind": "memory",
        "context": {
            "capability": {
                "kind": CAPABILITY_KIND,
                "name": "CPLD Updater Recovery",
                "description": "Recover a failed CPLD update from an unreachable BMC.",
                "version": "1.2.0",
                "scope": ["bdc"],
                "applicability": "Intel AST2600 BMCs",
                "prerequisites": ["UART access", "recovery image"],
                "required_tools": ["ipmitool", "socflash"],
                "instructions": "1. Attach UART. 2. Enter recovery mode.",
                "validation": "BMC reaches ready state",
                "safety": "Do not power-cycle mid-flash",
            }
        },
        "evidence": [{"kind": "turn", "id": "turn:1"}],
        "source_refs": ["turn:1"],
        "tags": ["capability:skill", "version:1.2.0"],
        "session_id": "",
        "workspace_id": "",
        "repo_id": "",
        "created_at": "2026-09-11T00:00:00Z",
        "updated_at": "2026-09-11T01:00:00Z",
    }
    unit.update(overrides)
    return unit


class SkillEligibilityTests(unittest.TestCase):
    def test_publishable_requires_all_conditions(self) -> None:
        self.assertTrue(is_publishable(skill_unit()))
        self.assertFalse(is_publishable(skill_unit(type="fact")))
        self.assertFalse(is_publishable(skill_unit(status="draft")))
        self.assertFalse(is_publishable(skill_unit(memory_kind="candidate")))
        self.assertFalse(is_publishable(skill_unit(context={"capability": {"kind": "concept"}})))

    def test_inactive_lifecycle_never_publishable(self) -> None:
        for status in ("candidate", "raw", "contested", "superseded", "deprecated", "rejected", "draft"):
            with self.subTest(status=status):
                unit = skill_unit(status=status)
                unit["memory_kind"] = "candidate" if status in ("candidate", "raw", "draft") else "memory"
                self.assertFalse(is_publishable(unit))

    def test_evidence_or_source_refs_required(self) -> None:
        self.assertTrue(is_publishable(skill_unit(evidence=[], source_refs=["turn:1"])))
        self.assertTrue(is_publishable(skill_unit(evidence=[{"kind": "turn", "id": "turn:1"}], source_refs=[])))
        self.assertFalse(is_publishable(skill_unit(evidence=[], source_refs=[])))


class SkillManifestTests(unittest.TestCase):
    def test_manifest_semantics_are_present(self) -> None:
        manifest = build_skill_manifest(skill_unit())
        for key in (
            "id",
            "name",
            "description",
            "version",
            "scope",
            "applicability",
            "prerequisites",
            "requiredTools",
            "instructions",
            "validation",
            "safety",
            "evidence",
            "sourceRefs",
            "updatedAt",
        ):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["version"], "1.2.0")
        self.assertEqual(manifest["scope"], ["bdc"])
        self.assertEqual(manifest["requiredTools"], ["ipmitool", "socflash"])

    def test_manifest_bounds_long_fields_and_arrays(self) -> None:
        manifest = build_skill_manifest(
            skill_unit(
                context={
                    "capability": {
                        "kind": CAPABILITY_KIND,
                        "name": "n" * 500,
                        "description": "d" * 5000,
                        "instructions": "i" * 20000,
                        "prerequisites": ["p" * 500] * 100,
                        "required_tools": ["t" * 500] * 100,
                        "scope": ["s" * 500] * 100,
                    }
                },
                evidence=[{"kind": "k" * 200, "id": "id" * 500}] * 100,
                source_refs=["r" * 500] * 100,
            )
        )
        self.assertLessEqual(len(manifest["name"]), 120)
        self.assertLessEqual(len(manifest["description"]), 2000)
        self.assertLessEqual(len(manifest["instructions"]), 8000)
        self.assertLessEqual(len(manifest["prerequisites"]), 20)
        self.assertLessEqual(len(manifest["requiredTools"]), 20)
        self.assertLessEqual(len(manifest["scope"]), 10)
        self.assertLessEqual(len(manifest["evidence"]), 10)
        self.assertLessEqual(len(manifest["sourceRefs"]), 10)

    def test_manifest_whitelists_evidence_keys(self) -> None:
        manifest = build_skill_manifest(
            skill_unit(evidence=[{"kind": "turn", "id": "turn:1", "secret": "password", "path": "/db/memory.sqlite3"}])
        )
        self.assertEqual(manifest["evidence"], [{"kind": "turn", "id": "turn:1"}])

    def test_manifest_defaults_version_when_missing(self) -> None:
        manifest = build_skill_manifest(skill_unit(context={"capability": {"kind": CAPABILITY_KIND}}))
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["name"], "CPLD recovery")


class SkillProviderTests(unittest.TestCase):
    def _provider(self, directory: Path) -> SkillProvider:
        store = MemoryStore(directory / "memory.sqlite3", directory / "events.jsonl")
        store.upsert(skill_unit())
        store.upsert(skill_unit(id="skill:draft", status="draft", memory_kind="candidate", summary="Draft skill"))
        store.upsert(skill_unit(id="skill:no-evidence", evidence=[], source_refs=[]))
        store.upsert(skill_unit(id="memory:fact", type="fact", summary="A plain fact"))
        store.upsert(skill_unit(id="skill:other", context={"capability": {"kind": CAPABILITY_KIND, "name": "Other Skill", "description": "Unrelated"}}))
        return SkillProvider(store)

    def test_list_skills_returns_only_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = self._provider(Path(directory))
            items = provider.list_skills(limit=20)
        ids = [item["id"] for item in items]
        self.assertIn("skill:cpld-recovery", ids)
        self.assertNotIn("skill:draft", ids)
        self.assertNotIn("skill:no-evidence", ids)
        self.assertNotIn("memory:fact", ids)

    def test_list_skills_filters_by_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = self._provider(Path(directory))
            self.assertEqual([item["id"] for item in provider.list_skills(query="unreachable", limit=20)], ["skill:cpld-recovery"])
            self.assertEqual(provider.list_skills(query="does-not-exist", limit=20), [])

    def test_get_skill_is_exact_and_never_substitutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = self._provider(Path(directory))
            self.assertEqual(provider.get_skill("skill:cpld-recovery")["id"], "skill:cpld-recovery")
            self.assertIsNone(provider.get_skill("skill:draft"))
            self.assertIsNone(provider.get_skill("skill:no-evidence"))
            self.assertIsNone(provider.get_skill("memory:fact"))
            self.assertIsNone(provider.get_skill("skill:does-not-exist"))


class SkillApiTests(unittest.TestCase):
    def test_list_and_get_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "memory.sqlite3", root / "events.jsonl")
            store.upsert(skill_unit())
            provider = SkillProvider(store)
            with patch.object(api, "skills", provider):
                listed = api.list_skills(query="", limit=20)
                found = api.get_skill("skill:cpld-recovery")
                missing = api.get_skill("skill:nope")

        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["skills"][0]["id"], "skill:cpld-recovery")
        self.assertEqual(found["found"], True)
        self.assertEqual(found["skill"]["id"], "skill:cpld-recovery")
        self.assertEqual(missing, {"found": False, "skill": None})


if __name__ == "__main__":
    unittest.main()
