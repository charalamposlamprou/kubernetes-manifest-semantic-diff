import json

import pytest

from manifest_diff.patch import capture_pair, yaml_hunks
from manifest_diff.report import markdown, validate


def test_context_does_not_leak_literal_learned_from_a_later_target(pair, run):
    token = "synthetic-" + "context-credential"
    for root in pair:
        # Same literal also occurs in an independent later Helm target's Secret.
        ingress = root / "examples/backend/base/ingress.yaml"
        ingress.write_text(ingress.read_text().replace("path: /", f"path: /{token}"))
        values = root / "examples/payments/values.yaml"
        values.write_text(values.read_text().replace("fixture-payments-only", token))
    ingress = pair[1] / "examples/backend/base/ingress.yaml"
    ingress.write_text(ingress.read_text().replace("pathType: Prefix", "pathType: Exact"))
    report = run(pair)
    assert not report["errors"] and report["counts"]["Modified"] == 3
    hidden = token not in json.dumps(report) and token not in markdown(report)
    assert hidden
    assert any("[redacted]" in line for c in report["changes"] for line in c["hunks"])


def test_patch_respects_list_ordering():
    a = {
        "spec": {
            "containers": [{"name": "b"}, {"name": "a"}],
            "initContainers": [{"name": "x"}, {"name": "y"}],
        }
    }
    b = {"spec": {key: list(reversed(items)) for key, items in a["spec"].items()}}
    h = yaml_hunks(capture_pair(a, b, 10000), lambda doc: doc, "", "Pod")
    edits = [s for s in h if s.startswith(("+", "-"))]
    assert edits and all("name: x" in s or "name: y" in s for s in edits)


def test_patch_fence_and_schema():
    change = {
        "app": "x",
        "env": "test",
        "target": "x",
        "resource": ["", "ConfigMap", "default", "x"],
        "status": "Modified",
        "fields": [],
        "hunks": ["@@ -1,2 +1,2 @@", " ```", "-old", "+new"],
    }
    report = {
        "counts": {"Modified": 1, "Added": 0, "Removed": 0},
        "changes": [change],
        "targets": [],
        "errors": [],
        "truncated": False,
    }
    body = markdown(report)
    assert "\n````diff\n" in body and "\n````\n" in body
    for invalid in (["@@ shell @@"], ["@@ -1 +1 @@", "+x\nfree text"]):
        change["hunks"] = invalid
        with pytest.raises(ValueError):
            validate(report)


@pytest.mark.parametrize(
    "field",
    [
        "spec.template.spec.containers[api].envFrom[0].configMapRef.name",
        "spec.template.spec.initContainers[init].env[MODE].valueFrom.configMapKeyRef.name",
        "spec.template.spec.volumes[settings].configMap.name",
    ],
)
def test_configmap_switch_needs_both_maps_and_a_matching_deployment_reference(field):
    from manifest_diff.report import review_highlights

    old = {"status": "Removed", "resource": ["", "ConfigMap", "prod", "old"], "fields": []}
    new = {"status": "Added", "resource": ["", "ConfigMap", "prod", "new"], "fields": []}
    dep = {
        "status": "Modified",
        "resource": ["apps", "Deployment", "prod", "api"],
        "fields": [[field, '"old"', '"new"']],
    }
    expected = "Deployment <code>api</code> switches to a new ConfigMap"
    assert review_highlights([new, dep, old]) == expected
    extra = {**new, "resource": ["", "ConfigMap", "prod", "unrelated"]}
    assert "<code>ConfigMap</code> added" in review_highlights([old, new, dep, extra])
    for mismatched in [
        [new, dep],
        [old, dep],
        [old, new, {**dep, "resource": ["apps", "Deployment", "dev", "api"]}],
        [old, new, {**dep, "fields": [[field, "[redacted]", '"new"']]}],
        [old, new, {**dep, "fields": [["metadata.annotations." + field, '"old"', '"new"']]}],
    ]:
        assert "switches to a new ConfigMap" not in review_highlights(mismatched)


def test_same_memory_value_stays_with_its_yaml_parent():
    def resources(limit_cpu, limit_memory, request_cpu, request_memory):
        return {
            "resources": {
                "limits": {"cpu": limit_cpu, "memory": limit_memory},
                "requests": {"cpu": request_cpu, "memory": request_memory},
            }
        }

    before = resources("100m", "32Mi", "10m", "16Mi")
    after = resources("200m", "64Mi", "25m", "32Mi")
    hunk = yaml_hunks(capture_pair(before, after, 10000), lambda doc: doc, "", "Pod")
    assert hunk == [
        "@@ -1,7 +1,7 @@",
        " resources:",
        "   limits:",
        "-    cpu: 100m",
        "-    memory: 32Mi",
        "+    cpu: 200m",
        "+    memory: 64Mi",
        "   requests:",
        "-    cpu: 10m",
        "-    memory: 16Mi",
        "+    cpu: 25m",
        "+    memory: 32Mi",
    ]


def test_inserting_named_item_keeps_existing_sibling_as_context():
    before = {"containers": [{"name": "api", "image": "shared"}]}
    after = {"containers": [{"name": "worker", "image": "shared"}, *before["containers"]]}
    hunks = yaml_hunks(capture_pair(before, after, 10000), lambda doc: doc, "", "Example")
    edits = [line for line in hunks if line.startswith(("+", "-"))]
    assert edits == ["+- image: shared", "+  name: worker"]
