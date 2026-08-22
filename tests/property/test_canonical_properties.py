"""Kanonik JSON icin ozellik ve fuzz testleri.

Harici bir property kutuphanesine bagimli olmadan, tohumlanmis rastgele belge
uretici ile determinizm ve carpisma ozellikleri sinanir.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from zekam.domain.canonical import canonical_json, digest

pytestmark = pytest.mark.property

SEED = 20260820
ITERATIONS = 300


def _random_value(rng: random.Random, depth: int = 0) -> Any:
    choices = ["null", "bool", "int", "float", "str"]
    if depth < 3:
        choices += ["list", "dict"]
    kind = rng.choice(choices)
    if kind == "null":
        return None
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "int":
        return rng.randint(-(10**12), 10**12)
    if kind == "float":
        return rng.uniform(-1e6, 1e6)
    if kind == "str":
        alphabet = 'abcXYZ019 -_.ölçüğşİ"\\\n\t{}[]:,'
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
    if kind == "list":
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = [f"k{rng.randint(0, 20)}" for _ in range(rng.randint(0, 5))]
    return {key: _random_value(rng, depth + 1) for key in keys}


def _shuffle_keys(value: Any, rng: random.Random) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffle_keys(item, rng) for key, item in items}
    if isinstance(value, list):
        return [_shuffle_keys(item, rng) for item in value]
    return value


def test_digest_is_deterministic_across_repeated_calls() -> None:
    rng = random.Random(SEED)
    for _ in range(ITERATIONS):
        document = _random_value(rng)
        assert digest(document) == digest(document)


def test_digest_is_stable_under_key_reordering() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(ITERATIONS):
        document = _random_value(rng)
        reordered = _shuffle_keys(document, rng)
        assert digest(document) == digest(reordered)


def test_canonical_json_roundtrips_through_json_loads() -> None:
    rng = random.Random(SEED + 2)
    for _ in range(ITERATIONS):
        document = _random_value(rng)
        rendered = canonical_json(document)
        assert canonical_json(json.loads(rendered)) == rendered


def test_distinct_documents_produce_distinct_digests() -> None:
    rng = random.Random(SEED + 3)
    seen: dict[str, str] = {}
    for _ in range(ITERATIONS):
        document = _random_value(rng)
        rendered = canonical_json(document)
        value = digest(document)
        if value in seen:
            assert seen[value] == rendered, "Farkli belgeler ayni digest uretti"
        seen[value] = rendered


def test_formatting_of_input_does_not_change_digest() -> None:
    """Girdi JSON'u bosluklu veya kacisli yazilsa da digest degismez."""
    rng = random.Random(SEED + 4)
    for _ in range(ITERATIONS):
        document = _random_value(rng)
        pretty = json.dumps(document, indent=4, ensure_ascii=True, sort_keys=False)
        assert digest(json.loads(pretty)) == digest(document)
