"""Exercise public entry points, protocol limits, and sensitive mixed-resource cases."""

import copy
import json
import sys

import pytest

from manifest_diff.cli import main
from manifest_diff.engine import compare
from manifest_diff.input import Problem, documents
from manifest_diff.render import Renderer
from manifest_diff.report import markdown
from manifest_diff.semantic import fields, flatten, index


def test_cli_native_and_artifact(pair, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["manifest-diff", "--native", *map(str, pair)])
    assert main() == 0 and "No semantic changes" in capsys.readouterr().out
    dest = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--artifact", str(dest)])
    monkeypatch.setattr("manifest_diff.publish.context", lambda: {"run": 1})
    assert main() == 0 and json.loads(dest.read_text())["version"] == 1
    monkeypatch.setattr(sys, "argv", ["manifest-diff", str(tmp_path / "missing"), str(tmp_path)])
    assert main() == 2 and "could not complete" in capsys.readouterr().err


def test_isolated_renderer_contract(tmp_path, pair, monkeypatch):
    from manifest_diff import render
    from manifest_diff.config import inventory

    root = pair[0]
    target = inventory(root)[0]["payments-production"]
    calls = []

    def inspect(command, env, cwd):
        calls.append(command)
        return b""

    monkeypatch.setattr(render, "capture", inspect)
    Renderer().render(root, target)
    command = calls[0]
    assert {
        "--network=none",
        "--log-driver=none",
        "--read-only",
        "--cap-drop=ALL",
        "--pull=never",
        f"{root}:/src:ro",
    } <= set(command)
    assert (
        command[command.index("/helm") + 1] == "template" and "--dependency-update" not in command
    )
    (root / ".git").mkdir()
    with pytest.raises(Problem, match="exported"):
        Renderer().render(root, target)


def test_embedded_and_typed_secrets():
    plain = "dummy" + "-nested-value"
    source = {
        "apiVersion": "v1",
        "kind": "SecretList",
        "items": [
            {"metadata": {"name": "x"}, "stringData": {"kind": "Secret", "credential": plain}}
        ],
    }
    secret = flatten(json.dumps(source))[0]
    wrapped = {
        "apiVersion": "example.io/v1",
        "kind": "Template",
        "metadata": {"name": "y"},
        "spec": {"object": secret},
    }
    for item in [secret, wrapped]:
        known = set()
        indexed = next(iter(index([item], "default", [], known).values()))
        changed = list(fields({}, indexed, "", "Demo"))
        safe = plain not in json.dumps(changed) and plain in known
        assert safe
    with pytest.raises(Problem, match="SecretList"):
        flatten(json.dumps({**source, "items": [{"kind": "Pod"}]}))


def test_cr_scope_and_invalid_inputs():
    custom = {"kind": "Region", "apiVersion": "platform.test/v1", "metadata": {"name": "west"}}
    assert ("platform.test", "Region", "", "west") in index(
        [custom], "ns", [], set(), [("platform.test", "Region")]
    )
    with pytest.raises(Problem, match="namespace"):
        index(
            [{**custom, "metadata": {"name": "west", "namespace": "bad"}}],
            "ns",
            [],
            set(),
            [("platform.test", "Region")],
        )
    with pytest.raises(Problem, match="Invalid YAML"):
        documents(r'{"x": "\ud800"}')
    for data in ("- foo", "apiVersion: v1\nkind: List\nitems: string"):
        with pytest.raises(Problem):
            flatten(data)


def test_non_deterministic_chart(pair):
    for root in pair:
        (root / "examples/payments/templates/random.yaml").write_text(
            'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: unstable}\ndata: {key: "{{ randAlphaNum 64 }}"}'
        )
    result = compare(*pair, Renderer(False))
    assert len(result["errors"]) == 6 and all("varied" in e[2] for e in result["errors"])
    assert not sum(result["counts"].values())


def test_add_remove_modify_resources_within_same_target(pair):
    head = pair[1] / "examples/backend/base"
    (head / "ingress.yaml").write_text(
        "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: backend}\nspec: {podSelector: {}}"
    )
    # The overlay's Ingress patch now has no selected resource.
    role = pair[1] / "examples/backend/components/access/rbac.yaml"
    role.write_text(role.read_text().replace("verbs: [get]", "verbs: [get, list]"))
    result = compare(*pair, Renderer(False))
    assert not result["errors"] and result["counts"] == {"Added": 3, "Modified": 3, "Removed": 3}
    assert all(
        c["fields"] == [["rules[0].verbs[1]", "∅", '"list"']]
        for c in result["changes"]
        if c["status"] == "Modified"
    )
    assert "security-sensitive" in markdown(result)


def test_large_summary_retains_counts():
    result = {
        "counts": {"Added": 5, "Modified": 0, "Removed": 0},
        "changes": [],
        "errors": [["base", "t", "é" * 500] for _ in range(100)],
        "targets": [],
        "truncated": True,
    }
    comment = markdown(result, "a" * 40)
    assert "| Added | 5 |" in comment and len(comment.encode()) < 60_000


def test_shape_changes_are_safe():
    source = {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": {"name": "a"},
        "data": {"key": "U0VDUkVU"},
    }
    a = next(iter(index([source], "default", [], set()).values()))
    b = copy.deepcopy(a)
    b["data"] = []
    assert list(fields(a, b, "", "Secret")) == [("data", "{…}", "[]")]
