"""Tests for the class-independent user id / client IP sampler."""

from __future__ import annotations

import inspect
import random
import re

from playground.traffic.sampler import DOC_RANGE_PREFIXES, Sampler

USER_ID_RE = re.compile(r"^pg-u-[0-9a-f]{12}$")


def test_signature_takes_no_class_argument():
    params = list(inspect.signature(Sampler.__init__).parameters)
    assert params == ["self", "rng"]
    for name in params:
        assert "cls" not in name.lower()
        assert "class" not in name.lower()


def test_user_ids_match_the_expected_shape():
    sampler = Sampler(random.Random(0))
    for _ in range(500):
        assert USER_ID_RE.match(sampler.user_id())


def test_client_ips_stay_within_the_documentation_ranges():
    sampler = Sampler(random.Random(0))
    for _ in range(500):
        ip = sampler.client_ip()
        prefix, _, host = ip.rpartition(".")
        assert prefix in DOC_RANGE_PREFIXES
        assert 1 <= int(host) <= 254


def test_output_distribution_does_not_depend_on_any_class_label():
    # The sampler never sees a class at all, so the same seed produces the
    # same stream of ids/IPs no matter what a caller does with the result.
    one = Sampler(random.Random(42))
    other = Sampler(random.Random(42))
    real_like = [one.sample() for _ in range(200)]
    attack_like = [other.sample() for _ in range(200)]
    assert real_like == attack_like
