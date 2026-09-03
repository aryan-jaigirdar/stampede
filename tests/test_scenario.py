"""Tests for scenario loading and weighted request selection."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from stampede.scenario import (
    RequestSpec,
    ScenarioError,
    WeightedPicker,
    load_scenario,
    make_spec,
    merge_headers,
)


class LoadScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, content: object) -> Path:
        path = self.dir / "scenario.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_loads_object_with_requests_list(self) -> None:
        path = self.write(
            {
                "requests": [
                    {"url": "http://localhost:8080/a"},
                    {
                        "method": "post",
                        "url": "http://localhost:8080/b",
                        "headers": {"Content-Type": "application/json"},
                        "body": "{\"k\": 1}",
                        "weight": 3,
                    },
                ]
            }
        )
        specs = load_scenario(path)
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].method, "GET")
        self.assertEqual(specs[0].weight, 1.0)
        self.assertIsNone(specs[0].body)
        self.assertEqual(specs[1].method, "POST")
        self.assertEqual(specs[1].body, b'{"k": 1}')
        self.assertEqual(specs[1].weight, 3.0)
        self.assertEqual(specs[1].headers, {"Content-Type": "application/json"})

    def test_loads_bare_list(self) -> None:
        path = self.write([{"url": "http://localhost/x"}])
        specs = load_scenario(path)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].url, "http://localhost/x")

    def test_missing_file(self) -> None:
        with self.assertRaises(ScenarioError):
            load_scenario(self.dir / "nope.json")

    def test_invalid_json(self) -> None:
        path = self.dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ScenarioError):
            load_scenario(path)

    def test_empty_requests_list_rejected(self) -> None:
        path = self.write({"requests": []})
        with self.assertRaises(ScenarioError):
            load_scenario(path)

    def test_missing_url_names_the_entry(self) -> None:
        path = self.write({"requests": [{"url": "http://ok/"}, {"method": "GET"}]})
        with self.assertRaisesRegex(ScenarioError, "request #2"):
            load_scenario(path)

    def test_unknown_key_rejected(self) -> None:
        path = self.write({"requests": [{"url": "http://ok/", "wieght": 2}]})
        with self.assertRaisesRegex(ScenarioError, "wieght"):
            load_scenario(path)

    def test_bad_weight_rejected(self) -> None:
        for weight in (0, -1, "heavy", True):
            path = self.write({"requests": [{"url": "http://ok/", "weight": weight}]})
            with self.assertRaises(ScenarioError):
                load_scenario(path)

    def test_bad_method_rejected(self) -> None:
        path = self.write({"requests": [{"url": "http://ok/", "method": "TRACE"}]})
        with self.assertRaises(ScenarioError):
            load_scenario(path)

    def test_bad_scheme_rejected(self) -> None:
        path = self.write({"requests": [{"url": "ftp://ok/"}]})
        with self.assertRaises(ScenarioError):
            load_scenario(path)

    def test_non_string_headers_rejected(self) -> None:
        path = self.write({"requests": [{"url": "http://ok/", "headers": {"X-N": 5}}]})
        with self.assertRaises(ScenarioError):
            load_scenario(path)


class MakeSpecTests(unittest.TestCase):
    def test_normalizes_method_case(self) -> None:
        spec = make_spec("http://localhost/", method="delete")
        self.assertEqual(spec.method, "DELETE")

    def test_rejects_empty_url(self) -> None:
        with self.assertRaises(ScenarioError):
            make_spec("")

    def test_rejects_infinite_weight(self) -> None:
        with self.assertRaises(ScenarioError):
            make_spec("http://localhost/", weight=float("inf"))


class MergeHeadersTests(unittest.TestCase):
    def test_defaults_apply_when_no_overrides(self) -> None:
        self.assertEqual(
            merge_headers({"X-Env": "staging", "X-Common": "1"}, {}),
            {"X-Env": "staging", "X-Common": "1"},
        )

    def test_override_wins_over_matching_default(self) -> None:
        self.assertEqual(
            merge_headers({"X-Env": "staging"}, {"X-Env": "prod"}),
            {"X-Env": "prod"},
        )

    def test_override_is_case_insensitive_and_keeps_override_casing(self) -> None:
        merged = merge_headers({"X-Env": "staging"}, {"x-env": "prod"})
        self.assertEqual(merged, {"x-env": "prod"})

    def test_non_matching_default_is_preserved_alongside_override(self) -> None:
        merged = merge_headers({"X-Common": "1"}, {"X-Env": "prod"})
        self.assertEqual(merged, {"X-Common": "1", "X-Env": "prod"})

    def test_inputs_are_not_mutated(self) -> None:
        defaults = {"X-Env": "staging"}
        overrides = {"X-Env": "prod"}
        merge_headers(defaults, overrides)
        self.assertEqual(defaults, {"X-Env": "staging"})
        self.assertEqual(overrides, {"X-Env": "prod"})


class WeightedPickerTests(unittest.TestCase):
    def specs(self) -> list[RequestSpec]:
        return [
            RequestSpec(url="http://localhost/a", weight=1.0),
            RequestSpec(url="http://localhost/b", weight=3.0),
        ]

    def test_requires_at_least_one_spec(self) -> None:
        with self.assertRaises(ValueError):
            WeightedPicker([])

    def test_rejects_non_positive_weight(self) -> None:
        with self.assertRaises(ValueError):
            WeightedPicker([RequestSpec(url="http://localhost/", weight=0.0)])

    def test_single_spec_always_picked(self) -> None:
        only = RequestSpec(url="http://localhost/only")
        picker = WeightedPicker([only], random.Random(1))
        for _ in range(10):
            self.assertIs(picker.pick(), only)

    def test_seeded_rng_is_deterministic(self) -> None:
        first = WeightedPicker(self.specs(), random.Random(42))
        second = WeightedPicker(self.specs(), random.Random(42))
        sequence_one = [first.pick().url for _ in range(100)]
        sequence_two = [second.pick().url for _ in range(100)]
        self.assertEqual(sequence_one, sequence_two)

    def test_different_seeds_diverge(self) -> None:
        first = WeightedPicker(self.specs(), random.Random(1))
        second = WeightedPicker(self.specs(), random.Random(2))
        sequence_one = [first.pick().url for _ in range(100)]
        sequence_two = [second.pick().url for _ in range(100)]
        self.assertNotEqual(sequence_one, sequence_two)

    def test_weights_shape_the_distribution(self) -> None:
        picker = WeightedPicker(self.specs(), random.Random(7))
        counts: Counter[str] = Counter(picker.pick().url for _ in range(4000))
        share_b = counts["http://localhost/b"] / 4000
        # Weight 3 of 4 total, so roughly 75 percent, with slack for randomness.
        self.assertGreater(share_b, 0.70)
        self.assertLess(share_b, 0.80)


if __name__ == "__main__":
    unittest.main()
