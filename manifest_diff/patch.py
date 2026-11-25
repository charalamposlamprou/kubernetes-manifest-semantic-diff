"""Build unified hunks from bounded, redacted views of rendered resources."""

import json
from difflib import SequenceMatcher
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

    # Scrub scalars before either path matching or YAML wrapping can expose them.
    docs = [scrub(doc) for doc in docs]
    rendered = [
        yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, width=120).rstrip("\n").split("\n")
        if doc is not None
        else []
        for doc in docs
    ]
    before, after = rendered
    if max(len(before), len(after)) > 2_000 or before == after:
        return []
    keys = rendered
    if before and after:
        keys = [line_keys(lines, doc) for lines, doc in zip(rendered, docs, strict=True)]
    matcher = SequenceMatcher(None, *keys)
    diff = path_aware_hunks(matcher, before, after)
    hunks = list(islice(diff, 1025))
    return hunks if len(hunks) <= 1024 and sum(map(len, hunks)) <= 60_000 else []


def line_keys(lines, document):
    """Match rendered lines by both YAML location and text, including scalar continuations."""
    starts = {}
    root = yaml.compose(
        "\n".join(lines) + "\n", Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    )

    def visit(node, data, path):
        starts[node.start_mark.line] = path
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                child = (*path, key.value)
                starts[key.start_mark.line] = child
                visit(value, data[key.value], child)
        elif isinstance(node, yaml.SequenceNode):
            named = unique_names(data)
            for index, item in enumerate(node.value):
                # A named item keeps its identity when a sibling is inserted before it.
                segment = ("name", data[index]["name"]) if named else index
                visit(item, data[index], (*path, segment))

    visit(root, document, ())
    keys = []
    path = ()
    for index, line in enumerate(lines):
        path = starts.get(index, path)
        keys.append((path, line))
    return keys


def hunk_range(start, end):
    count = end - start
    if not count:
        return f"{start},0"
    if count == 1:
        return str(start + 1)
    return f"{start + 1},{count}"


def path_aware_hunks(matcher, before, after):
    for group in matcher.get_grouped_opcodes(n=3):
        old_range = hunk_range(group[0][1], group[-1][2])
        new_range = hunk_range(group[0][3], group[-1][4])
        yield f"@@ -{old_range} +{new_range} @@"
        for op, i, j, k, end in group:
            if op == "equal":
                yield from (" " + line for line in before[i:j])
            if op in ("delete", "replace"):
                yield from ("-" + line for line in before[i:j])
            if op in ("insert", "replace"):
                yield from ("+" + line for line in after[k:end])


def unique_names(seq):
    names = [item.get("name") for item in seq if isinstance(item, dict)]
    return (
        len(names) == len(seq)
        and all(isinstance(n, str) and n for n in names)
        and len(set(names)) == len(names)
    )
