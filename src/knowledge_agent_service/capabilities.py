"""Structured, read-only discovery of human-approved skills.

A *skill* is a workflow memory unit that has been explicitly marked as a
capability and promoted through the lifecycle by a human. This module only
**reads** such units; it never extracts candidates, promotes records, or
executes skills. Candidate extraction and promotion belong to future
extension/agent-side work.

Publishing eligibility (the default provider contract)
------------------------------------------------------
A memory unit is publishable as a skill only when **all** of the following hold:

* ``type == "workflow"``
* ``status == "active"``
* ``memory_kind == "memory"``
* ``context.capability.kind == "skill"``
* it has ``evidence`` **or** ``source_refs``

``candidate``/``raw``/``contested``/``superseded``/``deprecated``/``rejected``
units are never returned by the default provider.
"""

from __future__ import annotations

from typing import Any

from .memory_store import MemoryStore

CAPABILITY_KIND = "skill"
DEFAULT_VERSION = "1.0.0"

# --- Strict bounds: never stream unbounded historical text back to callers. ---
NAME_MAX = 120
VERSION_MAX = 40
DESCRIPTION_MAX = 2000
APPLICABILITY_MAX = 2000
INSTRUCTIONS_MAX = 8000
VALIDATION_MAX = 4000
SAFETY_MAX = 4000
PREREQUISITES_MAX = 20
PREREQUISITE_MAX = 200
REQUIRED_TOOLS_MAX = 20
REQUIRED_TOOL_MAX = 120
SCOPE_MAX = 10
SCOPE_ITEM_MAX = 80
EVIDENCE_MAX = 10
EVIDENCE_KIND_MAX = 40
EVIDENCE_ID_MAX = 200
SOURCE_REFS_MAX = 10
SOURCE_REF_MAX = 200

# Upper bound on the number of candidate rows fetched before in-Python filtering.
CANDIDATE_FETCH_MAX = 500
# Hard cap on the number of skills a list endpoint may return.
LIST_LIMIT_MAX = 50


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded_list(items: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if len(result) >= limit:
            break
        result.append(_truncate(item, item_limit))
    return result


def capability_of(unit: dict[str, Any]) -> dict[str, Any]:
    """Return the ``context.capability`` object of a unit, or ``{}``."""
    context = unit.get("context")
    if not isinstance(context, dict):
        return {}
    capability = context.get("capability")
    return capability if isinstance(capability, dict) else {}


def is_publishable(unit: dict[str, Any]) -> bool:
    """Decide whether one memory unit may be published as a skill."""
    if str(unit.get("type") or "") != "workflow":
        return False
    if str(unit.get("status") or "") != "active":
        return False
    if str(unit.get("memory_kind") or "") != "memory":
        return False
    if str(capability_of(unit).get("kind") or "") != CAPABILITY_KIND:
        return False
    evidence = unit.get("evidence")
    source_refs = unit.get("source_refs")
    has_evidence = bool(isinstance(evidence, list) and evidence)
    has_source_refs = bool(isinstance(source_refs, list) and source_refs)
    return has_evidence or has_source_refs


def _bounded_evidence(evidence: Any) -> list[dict[str, str]]:
    """Whitelist evidence entries to ``{kind, id}`` so nothing else leaks."""
    if not isinstance(evidence, list):
        return []
    result: list[dict[str, str]] = []
    for item in evidence:
        if len(result) >= EVIDENCE_MAX:
            break
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "kind": _truncate(item.get("kind"), EVIDENCE_KIND_MAX),
                "id": _truncate(item.get("id"), EVIDENCE_ID_MAX),
            }
        )
    return result


def build_skill_manifest(unit: dict[str, Any]) -> dict[str, Any]:
    """Render a publishable unit into a bounded skill manifest.

    Field semantics follow the provider contract (id/name/description/version/
    scope/applicability/prerequisites/requiredTools/instructions/validation/
    safety/evidence/sourceRefs/updatedAt). Every string and array is length
    bounded so arbitrary historical text is never returned verbatim.
    """
    capability = capability_of(unit)
    summary = str(unit.get("summary") or "")
    resolution = str(unit.get("resolution") or "")
    return {
        "id": str(unit.get("id") or ""),
        "name": _truncate(capability.get("name") or summary, NAME_MAX),
        "description": _truncate(capability.get("description") or resolution, DESCRIPTION_MAX),
        "version": _truncate(capability.get("version") or DEFAULT_VERSION, VERSION_MAX),
        "scope": _bounded_list(capability.get("scope"), SCOPE_MAX, SCOPE_ITEM_MAX),
        "applicability": _truncate(capability.get("applicability"), APPLICABILITY_MAX),
        "prerequisites": _bounded_list(capability.get("prerequisites"), PREREQUISITES_MAX, PREREQUISITE_MAX),
        "requiredTools": _bounded_list(capability.get("required_tools"), REQUIRED_TOOLS_MAX, REQUIRED_TOOL_MAX),
        "instructions": _truncate(capability.get("instructions"), INSTRUCTIONS_MAX),
        "validation": _truncate(capability.get("validation"), VALIDATION_MAX),
        "safety": _truncate(capability.get("safety"), SAFETY_MAX),
        "evidence": _bounded_evidence(unit.get("evidence")),
        "sourceRefs": _bounded_list(unit.get("source_refs"), SOURCE_REFS_MAX, SOURCE_REF_MAX),
        "updatedAt": str(unit.get("updated_at") or ""),
    }


class SkillProvider:
    """Read-only provider over ``MemoryStore`` for approved workflow skills."""

    def __init__(self, store: MemoryStore):
        self._store = store

    def list_skills(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), LIST_LIMIT_MAX))
        candidates = self._store.list_skill_candidates(CANDIDATE_FETCH_MAX)
        manifests = [build_skill_manifest(unit) for unit in candidates if is_publishable(unit)]
        needle = query.strip().lower()
        if needle:
            manifests = [
                item
                for item in manifests
                if needle in item["name"].lower() or needle in item["description"].lower()
            ]
        return manifests[:limit]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        """Exact read by stable ID; returns ``None`` for unknown or ineligible IDs."""
        unit = self._store.get_unit(skill_id)
        if unit is None or not is_publishable(unit):
            return None
        return build_skill_manifest(unit)
