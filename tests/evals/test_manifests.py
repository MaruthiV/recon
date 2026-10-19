from collections import Counter

import pytest

from recon_fixtures.families import FAMILIES
from recon_fixtures.manifests import SPLITS, digest, frozen, specs


@pytest.mark.parametrize("split", sorted(SPLITS))
def test_the_generator_still_makes_the_frozen_splits(split):
    # a mismatch means the generator, corpus or label rules changed: re-freeze only with a DECISIONS entry,
    # and never re-freeze held_out after looking at results on it
    assert digest(split) == frozen()[split], f"{split} drifted from evals/manifests/frozen.json"


def test_split_sizes_and_family_balance():
    for split, (per, _) in SPLITS.items():
        counts = Counter(s.family for s in specs(split))
        assert counts == dict.fromkeys(FAMILIES, per), split


def test_splits_share_no_cases():
    seen = {split: {(s.seed, s.tenant, s.amount, s.placed_days_ago) for s in specs(split)} for split in SPLITS}
    assert not seen["dev"] & seen["held_out"] and not seen["val"] & seen["held_out"] and not seen["dev"] & seen["val"]
