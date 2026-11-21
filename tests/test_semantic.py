import copy
import json

import pytest

from manifest_diff.input import Problem, documents
from manifest_diff.semantic import fields, flatten, index


def obj(kind="Deployment", api="apps/v1", name="web", **extra):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name}, **extra}


def test_reordering_and_type_sensitivity():
    a, b = obj(), obj("Service", "v1")
    assert index([a, b], "prod", [], set()) == index([b, a], "prod", [], set())
    assert not list(fields({"a": 1, "b": 2}, {"b": 2, "a": 1}, "", "X"))
    assert list(fields({"a": True}, {"a": 1}, "", "X")) == [("a", "true", "1")]


def test_container_order_env_and_commands():
    first = {
        "name": "a",
        "image": "old",
        "env": [{"name": "A", "value": "1"}, {"name": "B", "value": "$(A)"}],
        "command": ["one", "two"],
    }
    second = {"name": "b", "image": "old"}
    a = {"spec": {"containers": [first, second]}}
    b = copy.deepcopy(a)
    b["spec"]["containers"].reverse()
    assert not list(fields(a, b, "", "Pod"))
    changed = b["spec"]["containers"][1]
    changed["command"].reverse()
    changed["env"].reverse()
    changed["env"].append({"name": "C", "value": "3"})
    paths = [r[0] for r in fields(a, b, "", "Pod")]
    assert {
        "spec.containers[a].command[0]",
        "spec.containers[a].env.order",
        "spec.containers[a].env[C].value",
    } <= set(paths)
    assert list(fields(a, b, "sample.test", "Custom"))


def test_init_order_service_port_and_nested_security():
    seq = [{"name": "one"}, {"name": "two"}]
    assert (
        list(
            fields(
                {"spec": {"initContainers": seq}},
                {"spec": {"initContainers": seq[::-1]}},
                "",
                "Pod",
            )
        )[0][0]
        == "spec.initContainers.order"
    )
    changes = list(fields({"spec": {"ports": seq}}, {"spec": {"ports": seq[1:]}}, "", "Service"))
    assert changes == [("spec.ports[one].name", '"one"', "∅")]
    assert list(
        fields(
            {"securityContext": {"allowPrivilegeEscalation": False}},
            {"securityContext": {"allowPrivilegeEscalation": True}},
            "",
            "Pod",
        )
    )


def test_scope_api_versions_and_duplicate():
    crd = obj(
        "CustomResourceDefinition",
        "apiextensions.k8s.io/v1",
        "widgets.test.io",
        spec={"group": "test.io", "names": {"kind": "Widget"}, "scope": "Cluster"},
    )
    old = obj("Widget", "test.io/v1alpha1")
    new = obj("Widget", "test.io/v1")
    key = ("test.io", "Widget", "", "web")
    a, b = (index([x, crd], "production", [], set()) for x in [old, new])
    assert list(fields(a[key], b[key], "test.io", "Widget")) == [
        ("apiVersion", '"test.io/v1alpha1"', '"test.io/v1"')
    ]
    assert ("", "Namespace", "", "web") in index([obj("Namespace", "v1")], "prod", [], set())
    assert ("", "Service", "prod", "web") in index([obj("Service", "v1")], "prod", [], set())
    with pytest.raises(Problem, match="Duplicate"):
        index([old, new], "prod", [], set())


@pytest.mark.parametrize(
    "raw",
    [
        "a: [",
        "a: 1\na: 2",
        "a: &x 1\nb: *x",
        "true: 1",
        "a: .nan",
        "a: 2020-01-01",
        "x: !!python/name:os.system ''",
    ],
)
def test_invalid_yaml(raw):
    with pytest.raises(Problem):
        documents(raw)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"kind": "Service", "apiVersion": "v1", "metadata": {}},
        {"kind": "Secret", "apiVersion": "v1", "metadata": {"name": "x"}, "data": "wrong"},
    ],
)
def test_malformed_resource(bad):
    with pytest.raises(Problem):
        index([bad], "default", [], set())


def test_list_and_base64_redaction():
    import base64

    token = "sensitive" + "-canary"
    secret = obj("Secret", "v1", data={"key": base64.b64encode(token.encode()).decode()})
    docs = flatten(json.dumps({"kind": "List", "apiVersion": "v1", "items": [secret]}))
    known = set()
    masked = next(iter(index(docs, "default", [], known).values()))
    rows = list(fields({}, masked, "", "Secret"))
    safe = token not in str(rows) and secret["data"]["key"] not in str(rows)
    assert safe and token in known
    assert ("data.key", "∅", "[redacted]") in rows
