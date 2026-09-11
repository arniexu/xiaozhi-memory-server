import unittest

from knowledge_agent_service.entity_linker import Gazetteer, link_chunks, normalize


def entity(entity_id: str, label: str, **extra) -> dict:
    return {"id": entity_id, "label": label, **extra}


class NormalizeTests(unittest.TestCase):
    def test_casefolds_and_collapses_whitespace_without_losing_punctuation(self) -> None:
        self.assertEqual(normalize("  I2C-1 / IPMI\u00a0  over   LAN "), "i2c-1 / ipmi over lan")

    def test_applies_nfkc_compatibility_folding(self) -> None:
        self.assertEqual(normalize("（ＢＭＣ）"), "(bmc)")


class GazetteerTests(unittest.TestCase):
    def test_skips_short_and_stopword_aliases(self) -> None:
        catalog = Gazetteer.from_entities([
            entity("e1", "BMC"),
            entity("e2", "at"),
            entity("e3", "the"),
            entity("e4", "IPMI"),
        ])
        self.assertEqual(sorted(alias.alias for alias in catalog.aliases), ["bmc", "ipmi"])

    def test_accepts_single_cjk_pair_and_rejects_single_character(self) -> None:
        catalog = Gazetteer.from_entities([entity("e1", "风扇"), entity("e2", "泵")])
        self.assertEqual([alias.alias for alias in catalog.aliases], ["风扇"])

    def test_tags_are_not_treated_as_entity_names_by_default(self) -> None:
        catalog = Gazetteer.from_entities([entity("e1", "Other", tags=["hardware", "implementation"])])
        self.assertEqual(catalog.size, 1)
        self.assertEqual([alias.alias for alias in catalog.aliases], ["other"])

    def test_tags_can_be_opted_in(self) -> None:
        catalog = Gazetteer.from_entities([entity("e1", "Other", tags=["hardware"])], include_tags=True)
        self.assertEqual(sorted(alias.alias for alias in catalog.aliases), ["hardware", "other"])

    def test_rejects_path_like_surfaces(self) -> None:
        catalog = Gazetteer.from_entities([
            entity("e1", ".github/agents/yocto.agent.md"),
            entity("e2", "tasks.json"),
            entity("e3", ".copilot-kb"),
            entity("e4", "i2c-1"),
            entity("e5", "OpenBMC"),
        ])
        self.assertEqual(sorted(alias.alias for alias in catalog.aliases), ["i2c-1", "openbmc"])

    def test_ambiguous_alias_resolves_to_label_then_reports_alternatives(self) -> None:
        catalog = Gazetteer.from_entities([
            entity("e-tag", "Other", tags=["bmc"]),
            entity("e-label", "BMC"),
        ], include_tags=True)
        resolved = {alias.alias: alias.entity_id for alias in catalog.aliases}
        self.assertEqual(resolved["bmc"], "e-label")
        self.assertEqual(catalog.entity_count, 2)
        self.assertEqual(catalog.ambiguous["bmc"], ["e-label", "e-tag"])

    def test_ignores_entities_without_identifier(self) -> None:
        catalog = Gazetteer.from_entities([entity("", "BMC")])
        self.assertEqual(catalog.size, 0)
        self.assertEqual(catalog.skipped, 1)


class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = Gazetteer.from_entities([
            entity("e-ipmi", "IPMI"),
            entity("e-lan", "IPMI over LAN", aliases=["RMCP+"]),
            entity("e-bmc", "BMC"),
            entity("e-fan", "风扇", tags=["cooling"]),
        ], include_tags=True)

    def test_requires_ascii_word_boundaries(self) -> None:
        mentions = self.catalog.match("ipmitool talks to the bmc")
        self.assertEqual([mention.entity_id for mention in mentions], ["e-bmc"])

    def test_longest_alias_wins_inside_one_span(self) -> None:
        mentions = self.catalog.match("IPMI over LAN is required")
        self.assertEqual([mention.entity_id for mention in mentions], ["e-lan"])

    def test_counts_occurrences_and_keeps_first_position(self) -> None:
        mentions = self.catalog.match("BMC then bmc again")
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].occurrences, 2)
        self.assertEqual(mentions[0].position, 0)
        self.assertEqual(mentions[0].confidence, "high")

    def test_single_occurrence_tag_match_is_downgraded(self) -> None:
        mentions = self.catalog.match("cooling loop temperature")
        self.assertEqual([mention.entity_id for mention in mentions], ["e-fan"])
        self.assertEqual(mentions[0].alias, "cooling")
        self.assertEqual(mentions[0].source, "tag")
        self.assertEqual(mentions[0].confidence, "low")

    def test_matches_cjk_substring_without_word_boundaries(self) -> None:
        mentions = self.catalog.match("风扇控制器故障")
        self.assertEqual([mention.entity_id for mention in mentions], ["e-fan"])

    def test_output_is_deterministic_and_bounded(self) -> None:
        text = " ".join(f"BMC IPMI segment {index}" for index in range(40))
        first = self.catalog.match(text, max_matches=3)
        second = self.catalog.match(text, max_matches=3)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(self.catalog.match(text, max_matches=1)), 1)

    def test_empty_inputs_return_no_mentions(self) -> None:
        self.assertEqual(self.catalog.match(""), [])
        self.assertEqual(Gazetteer.from_entities([]).match("BMC"), [])


class LinkChunksTests(unittest.TestCase):
    def test_flattens_mentions_into_graph_ready_records(self) -> None:
        catalog = Gazetteer.from_entities([entity("e-bmc", "BMC")])
        records = link_chunks(
            [
                {"id": "document:abc:page:1:chunk:0", "text": "BMC reset completed"},
                {"id": "", "text": "BMC without id"},
                {"id": "document:abc:page:1:chunk:1", "text": ""},
            ],
            catalog,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0], {
            "chunk_id": "document:abc:page:1:chunk:0",
            "entity_id": "e-bmc",
            "alias": "bmc",
            "source": "label",
            "occurrences": 1,
            "confidence": "medium",
        })


class PruneTests(unittest.TestCase):
    def test_small_corpora_are_left_unchanged(self) -> None:
        catalog = Gazetteer.from_entities([entity("e-bmc", "BMC")])
        self.assertIs(catalog.prune_common_aliases(["bmc", "bmc", "bmc"]), catalog)

    def test_drops_aliases_that_appear_in_most_chunks(self) -> None:
        catalog = Gazetteer.from_entities([entity("e-common", "register"), entity("e-rare", "GemMountain PAS")])
        corpus = ["register table" for _ in range(40)] + ["GemMountain PAS overview"]
        pruned = catalog.prune_common_aliases(corpus, max_ratio=0.25)
        self.assertEqual([alias.alias for alias in pruned.aliases], ["gemmountain pas"])
        self.assertEqual(pruned.pruned, 1)

    def test_keeps_ambiguous_report_only_for_surviving_aliases(self) -> None:
        catalog = Gazetteer.from_entities([
            entity("e-label", "BMC"),
            entity("e-tag", "Other", tags=["bmc"]),
            entity("e-rare", "GemMountain PAS"),
        ], include_tags=True)
        corpus = ["bmc everywhere" for _ in range(40)] + ["GemMountain PAS overview"]
        pruned = catalog.prune_common_aliases(corpus, max_ratio=0.25)
        self.assertNotIn("bmc", pruned.ambiguous)
        self.assertEqual([alias.alias for alias in pruned.aliases], ["gemmountain pas", "other"])


if __name__ == "__main__":
    unittest.main()
