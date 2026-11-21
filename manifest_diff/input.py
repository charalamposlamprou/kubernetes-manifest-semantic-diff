"""Bounded JSON-compatible YAML input. Diagnostics never include source excerpts."""

import math
import re
from pathlib import Path

import yaml


class Problem(Exception):
    """A fixed, publication-safe diagnostic."""


class Loader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise Problem("YAML aliases are unsupported. Expand the alias.")
        self.nodes = getattr(self, "nodes", 0) + 1
        self.depth = getattr(self, "depth", 0) + 1
        if self.nodes > 250_000 or self.depth > 80:
            raise Problem("YAML complexity limit exceeded.")
        node = super().compose_node(parent, index)
        self.depth -= 1
        return node

    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep)
        if len(result) != len(node.value) or any(type(k) is not str for k in result):
            raise Problem("YAML maps require unique string keys.")
        return result


def check_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            check_json(child)
    elif isinstance(value, list):
        for child in value:
            check_json(child)
    elif type(value) is str:
        value.encode("utf-8")
    elif type(value) not in (int, float, bool, type(None)) or (
        type(value) is float and not math.isfinite(value)
    ):
        raise Problem("Use JSON-compatible YAML values. Quote dates and binary strings.")


def documents(raw):
    if len(raw) > 8 * 1024 * 1024:
        raise Problem("Rendered input exceeds 8 MiB.")
    try:
        docs = list(yaml.load_all(raw, Loader=Loader))
        check_json(docs)
        return [doc for doc in docs if doc is not None]
    except (yaml.YAMLError, UnicodeError, ValueError, OverflowError, RecursionError):
        raise Problem("Invalid YAML. Check mapping keys, indentation and scalar types.") from None


def one(path):
    if path.stat().st_size > 512_000:
        raise Problem("Configuration exceeds size limit.")
    docs = documents(path.read_bytes())
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise Problem("Expected one YAML mapping in configuration.")
    return docs[0]


def label(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise Problem(
            "Invalid identifier. Use a short alphanumeric name with dots, dashes or underscores."
        )
    return value


def local(root, value, *, directory=False):
    if (
        type(value) is not str
        or not value
        or Path(value).is_absolute()
        or any(part.startswith(".") for part in Path(value).parts)
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise Problem("Expected a visible repository-relative path without traversal.")
    path = root / value
    if not path.resolve().is_relative_to(root) or not (
        path.is_dir() if directory else path.is_file()
    ):
        raise Problem("Target directory or values file is missing or outside the revision.")
    return path


def strings(value):
    if type(value) is not list or len(value) > 128 or any(type(x) is not str for x in value):
        raise Problem("Expected a bounded list of strings in configuration.")
    return tuple(value)
