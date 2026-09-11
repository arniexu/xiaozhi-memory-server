"""Deterministic entity linking for imported documents.

This module is deliberately model-free: it resolves mentions of already-known
knowledge entities inside document chunks using alias matching only. The result
is reproducible, cheap, auditable, and safe to run on every import, which is why
it is the always-on layer that connects documents to the knowledge graph.

Design contract
---------------
* No network, no model, no randomness: the same input always yields the same output.
* Longest alias wins; overlapping mentions are suppressed so one surface form maps
  to one entity per span.
* Ambiguous aliases (one alias, several entities) resolve deterministically to the
  highest-weight, then lowest-id entity, and are reported for review.
* Bounded output: at most ``max_matches`` entity mentions per chunk.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

# --- tuning constants -------------------------------------------------------

MIN_ASCII_ALIAS = 3
MIN_CJK_ALIAS = 2
MAX_ALIAS_LENGTH = 80
MAX_MATCHES_PER_CHUNK = 24

#: Aliases that carry no discriminating power on their own.
STOPWORD_ALIASES = frozenset({
    "and", "the", "for", "with", "that", "this", "from", "into", "when", "then",
    "there", "here", "over", "under", "about", "using", "used", "use", "case",
    "value", "values", "data", "item", "items", "note", "notes", "test", "tests",
    "code", "line", "lines", "page", "pages", "issue", "issues", "list", "step",
    "steps", "结果", "问题", "方法", "内容", "说明", "测试", "注意", "如下", "其中",
})

WEIGHT_LABEL = 3
WEIGHT_ALIAS = 2
WEIGHT_TAG = 1

_SOURCE_WEIGHT = {"label": WEIGHT_LABEL, "alias": WEIGHT_ALIAS, "tag": WEIGHT_TAG}
_CONFIDENCE_BY_WEIGHT = {WEIGHT_LABEL: "high", WEIGHT_ALIAS: "high", WEIGHT_TAG: "medium"}
_CONFIDENCE_ORDER = ["low", "medium", "high"]

_ASCII_WORD = re.compile(r"[a-z0-9_]")
_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_WHITESPACE = re.compile(r"\s+")
#: File and symbol paths that surface as entity labels but never name an entity.
_PATH_LIKE = re.compile(
    r"[/\\]|^\.[\w.-]+$|\.(?:md|markdown|json|jsonl|ts|tsx|js|mjs|py|sh|bash|cfg|conf|ini|ya?ml|toml|txt|log|csv|xml)\b",
    re.IGNORECASE,
)


def looks_like_path(alias: str) -> bool:
    """Report whether a surface form is a filesystem or symbol path, not an entity name."""

    return bool(_PATH_LIKE.search(alias or ""))


def normalize(text: str) -> str:
    """Casefold and collapse whitespace without destroying technical punctuation."""

    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", folded).strip()


def _has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR.search(text))


def _edge_is_boundary(character: str) -> bool:
    """CJK has no word separators, so only ASCII/digit/underscore neighbours bind."""

    return not _ASCII_WORD.match(character)


@dataclass(frozen=True)
class Alias:
    """One normalized surface form that points at one entity."""

    entity_id: str
    alias: str
    display: str
    source: str
    weight: int


@dataclass(frozen=True)
class Mention:
    """A resolved entity mention inside a piece of text."""

    entity_id: str
    alias: str
    display: str
    source: str
    occurrences: int
    confidence: str
    position: int


@dataclass
class Gazetteer:
    """Alias index built from the currently active knowledge entities."""

    aliases: list[Alias] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    skipped: int = 0
    pruned: int = 0

    @classmethod
    def from_entities(
        cls,
        entities: Iterable[dict[str, Any]],
        *,
        include_tags: bool = False,
        include_aliases: bool = True,
    ) -> "Gazetteer":
        """Build an alias index, ignoring unusable or ambiguous entries deterministically.

        ``include_tags`` defaults to ``False``. Tags are topical labels attached to
        sessions and decisions ("hardware", "implementation", "git"), not entity
        names. Measured on a four-document corpus, indexing tags multiplied links by
        roughly six while adding almost no distinct entities, and the extra links were
        generic words that match nearly every page. Callers that really want their
        tags treated as names can opt in.
        """

        candidates: dict[str, list[Alias]] = {}
        skipped = 0
        for entity in entities:
            entity_id = str(entity.get("id") or "").strip()
            if not entity_id:
                skipped += 1
                continue
            surfaces: list[tuple[str, str]] = [("label", str(entity.get("label") or ""))]
            if include_aliases:
                raw_aliases = entity.get("aliases") or []
                if isinstance(raw_aliases, (list, tuple, set)):
                    surfaces.extend(("alias", str(item)) for item in raw_aliases)
                elif isinstance(raw_aliases, str):
                    surfaces.append(("alias", raw_aliases))
            if include_tags:
                raw_tags = entity.get("tags") or []
                if isinstance(raw_tags, (list, tuple, set)):
                    surfaces.extend(("tag", str(item)) for item in raw_tags)
            for source, surface in surfaces:
                alias = normalize(surface)
                if not _alias_is_usable(alias) or looks_like_path(surface.strip()):
                    skipped += 1
                    continue
                candidates.setdefault(alias, []).append(
                    Alias(
                        entity_id=entity_id,
                        alias=alias,
                        display=surface.strip(),
                        source=source,
                        weight=_SOURCE_WEIGHT[source],
                    )
                )

        resolved: list[Alias] = []
        ambiguous: dict[str, list[str]] = {}
        for alias in sorted(candidates):
            group = sorted(
                candidates[alias],
                key=lambda item: (-item.weight, item.entity_id, item.source),
            )
            resolved.append(group[0])
            distinct = sorted({item.entity_id for item in group})
            if len(distinct) > 1:
                ambiguous[alias] = distinct
        return cls(aliases=resolved, ambiguous=ambiguous, skipped=skipped)

    @property
    def size(self) -> int:
        """Number of distinct alias surfaces in the index."""

        return len(self.aliases)

    @property
    def entity_count(self) -> int:
        """Number of distinct linkable entities in the index."""

        return len({alias.entity_id for alias in self.aliases})

    def prune_common_aliases(
        self,
        texts: Iterable[str],
        *,
        max_ratio: float = 0.25,
        min_corpus: int = 20,
    ) -> "Gazetteer":
        """Drop aliases too common in this corpus to discriminate between documents.

        A surface form that appears on most pages of every document tells us nothing
        about which documents are related, and it is the main source of low quality
        links: generic words such as "register" or "status" match almost every chunk.
        Filtering by document frequency keeps the graph explainable and is still
        deterministic, since it depends only on the corpus being linked.

        Corpora smaller than ``min_corpus`` are returned unchanged; a handful of
        documents cannot support a meaningful frequency estimate.
        """

        normalized = [normalize(text) for text in texts]
        normalized = [text for text in normalized if text]
        if len(normalized) < min_corpus:
            return self
        threshold = max(2.0, max_ratio * len(normalized))
        kept: list[Alias] = []
        dropped = 0
        for alias in self.aliases:
            matches = sum(1 for text in normalized if alias.alias in text)
            if matches > threshold:
                dropped += 1
                continue
            kept.append(alias)
        if not dropped:
            return self
        kept_forms = {alias.alias for alias in kept}
        return Gazetteer(
            aliases=kept,
            ambiguous={key: value for key, value in self.ambiguous.items() if key in kept_forms},
            skipped=self.skipped,
            pruned=self.pruned + dropped,
        )

    def match(self, text: str, *, max_matches: int = MAX_MATCHES_PER_CHUNK) -> list[Mention]:
        """Return bounded, overlap-free entity mentions for ``text``."""

        haystack = normalize(text)
        if not haystack or not self.aliases or max_matches <= 0:
            return []
        spans: list[tuple[int, int, Alias]] = []
        for alias in self.aliases:
            if alias.alias not in haystack:
                continue
            spans.extend(_find_spans(haystack, alias))
        if not spans:
            return []

        # Longest match wins; ties break on earliest position then stable entity id.
        spans.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2].entity_id))
        accepted: list[tuple[int, int, Alias]] = []
        for span in spans:
            if _overlaps(accepted, span):
                continue
            accepted.append(span)

        grouped: dict[str, dict[str, Any]] = {}
        for start, _end, alias in accepted:
            bucket = grouped.get(alias.entity_id)
            if bucket is None:
                grouped[alias.entity_id] = {"alias": alias, "position": start, "occurrences": 1}
                continue
            bucket["occurrences"] += 1
            bucket["position"] = min(bucket["position"], start)
            best = _alias_preference(alias)
            current = _alias_preference(bucket["alias"])
            if best > current or (best == current and alias.alias < bucket["alias"].alias):
                bucket["alias"] = alias

        mentions = [
            Mention(
                entity_id=bucket["alias"].entity_id,
                alias=bucket["alias"].alias,
                display=bucket["alias"].display,
                source=bucket["alias"].source,
                occurrences=int(bucket["occurrences"]),
                confidence=_confidence(bucket["alias"], int(bucket["occurrences"])),
                position=int(bucket["position"]),
            )
            for bucket in grouped.values()
        ]
        mentions.sort(key=lambda mention: (mention.position, mention.entity_id, mention.alias))
        return mentions[:max_matches]


def link_chunks(
    chunks: Iterable[dict[str, Any]],
    gazetteer: Gazetteer,
    *,
    chunk_text_key: str = "text",
    chunk_id_key: str = "id",
) -> list[dict[str, Any]]:
    """Flatten per-chunk mentions into graph-ready records."""

    records: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_id = str(chunk.get(chunk_id_key) or "")
        text = str(chunk.get(chunk_text_key) or "")
        if not chunk_id or not text:
            continue
        for mention in gazetteer.match(text):
            records.append(
                {
                    "chunk_id": chunk_id,
                    "entity_id": mention.entity_id,
                    "alias": mention.alias,
                    "source": mention.source,
                    "occurrences": mention.occurrences,
                    "confidence": mention.confidence,
                }
            )
    return records


def _alias_is_usable(alias: str) -> bool:
    if not alias or len(alias) > MAX_ALIAS_LENGTH:
        return False
    if alias in STOPWORD_ALIASES:
        return False
    if _has_cjk(alias):
        return len(alias) >= MIN_CJK_ALIAS
    if _ASCII_WORD.search(alias):
        return len(alias) >= MIN_ASCII_ALIAS
    return False


def _find_spans(haystack: str, alias: Alias) -> list[tuple[int, int, Alias]]:
    """Find non-overlapping-with-self occurrences of one alias, boundaries enforced."""

    needle = alias.alias
    length = len(needle)
    spans: list[tuple[int, int, Alias]] = []
    cursor = 0
    while True:
        start = haystack.find(needle, cursor)
        if start < 0:
            return spans
        end = start + length
        if _boundary_ok(haystack, start, end, needle):
            spans.append((start, end, alias))
        cursor = end


def _boundary_ok(haystack: str, start: int, end: int, needle: str) -> bool:
    if _ASCII_WORD.match(needle[0]) and start > 0:
        if not _edge_is_boundary(haystack[start - 1]):
            return False
    if _ASCII_WORD.match(needle[-1]) and end < len(haystack):
        if not _edge_is_boundary(haystack[end]):
            return False
    return True


def _overlaps(accepted: list[tuple[int, int, Alias]], candidate: tuple[int, int, Alias]) -> bool:
    start, end, _alias = candidate
    return any(start < other[1] and other[0] < end for other in accepted)


def _alias_preference(alias: Alias) -> tuple[int, int]:
    """Prefer label over alias over tag, then the longer surface form."""

    return (alias.weight, len(alias.alias))


def _confidence(alias: Alias, occurrences: int) -> str:
    index = _CONFIDENCE_ORDER.index(_CONFIDENCE_BY_WEIGHT[alias.weight])
    if occurrences < 2:
        index -= 1
    if alias.source == "tag" and len(alias.alias) <= 4:
        index -= 1
    return _CONFIDENCE_ORDER[max(0, index)]
