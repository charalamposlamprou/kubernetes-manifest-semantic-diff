"""Build unified hunks from bounded, redacted views of rendered resources."""

import json
from difflib import unified_diff
from itertools import islice

import yaml

from .semantic import Mask, unordered_named_paths


def mask_value(value):
    if isinstance(value, Mask):
        return "[redacted]"
    raise TypeError("Unexpected value in normalized resource.")


def capture_pair(before, after, remaining):
    # Serialize in memory so the retained context budget bounds large object graphs too.
    pair = tuple(json.dumps(v, default=mask_value) for v in (before, after))
    return pair if sum(map(len, pair)) <= min(remaining, 128_000) else None


def yaml_hunks(pair, scrub, group, kind):
    docs = [json.loads(source) for source in pair]
    for path in unordered_named_paths(group, kind):
        sequences = []
        for doc in docs:
            node = doc
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, list):
                sequences.append(node)
        # With duplicate or absent names, both revisions keep their positional meaning.
        if all(unique_names(seq) for seq in sequences):
            for seq in sequences:
                seq.sort(key=lambda item: item["name"])

    def lines(doc):
        if doc is None:
            return []
        # Scrub individual scalars before YAML quoting or wrapping changes them.
        return (
            yaml.safe_dump(scrub(doc), sort_keys=True, allow_unicode=True, width=120)
            .rstrip("\n")
            .split("\n")
        )

    before, after = map(lines, docs)
    if max(len(before), len(after)) > 2_000:
        return []
    diff = unified_diff(before, after, n=3, lineterm="")
    hunks = list(islice(diff, 2, 1027))  # The publisher constructs the two file headers.
    return hunks if len(hunks) <= 1024 and sum(map(len, hunks)) <= 60_000 else []


def unique_names(seq):
    names = [item.get("name") for item in seq if isinstance(item, dict)]
    return (
        len(names) == len(seq)
        and all(isinstance(n, str) and n for n in names)
        and len(set(names)) == len(names)
    )
