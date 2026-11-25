import json
import shutil

import pytest
import yaml

from manifest_diff.config import inventory
from manifest_diff.input import Problem
from manifest_diff.render import Renderer
from manifest_diff.report import markdown
from manifest_diff.semantic import flatten


def edit(path, old, new):
    original = path.read_text()
    assert old in original
    path.write_text(original.replace(old, new))


def configure(root, targets):
    (root / ".manifest-diff.yaml").write_text(yaml.safe_dump({"version": 1, "targets": targets}))


@pytest.mark.parametrize("tool", ["helm", "kustomize", "mixed"])
def test_real_discovery(tmp_path, run, tool, pair):
    for root in pair:
        (root / ".manifest-diff.yaml").unlink()
        if tool != "mixed":
            shutil.rmtree(root / "examples" / ("backend" if tool == "helm" else "payments"))
    targets, _ = inventory(pair[0])
    assert len(targets) == {"helm": 1, "kustomize": 3, "mixed": 4}[tool]
    result = run(pair)
    assert not result["errors"] and "No semantic changes" in markdown(result)


def test_mixed_patch_and_values(pair, run):
    head = pair[1] / "examples"
    edit(head / "backend/overlays/production/capacity.yaml", "replicas: 3", "replicas: 4")
    edit(
        head / "backend/overlays/production/kustomization.yaml",
        'newTag: "1.0.0"',
        'newTag: "2.0.0"',
    )
    edit(head / "environments/production/payments.yaml", "100m", "250m")
    edit(head / "environments/production/payments.yaml", "256Mi", "512Mi")
    out = run(pair)
    assert not out["errors"] and out["counts"] == {"Added": 0, "Modified": 2, "Removed": 0}
    rows = {row[0]: row[1:] for change in out["changes"] for row in change["fields"]}
    assert rows["spec.replicas"] == ["3", "4"]
    assert rows["spec.template.spec.containers[backend].image"][1].endswith(':2.0.0"')
    assert rows["spec.template.spec.containers[payments].resources.requests.cpu"] == [
        '"100m"',
        '"250m"',
    ]
    assert rows["spec.template.spec.containers[payments].resources.limits.memory"] == [
        '"256Mi"',
        '"512Mi"',
    ]
    body = markdown(out)
    overview, details = body.split("<details>", 1)
    assert "2 resource changes" in overview
    assert "Replicas <code>3</code> → <code>4</code>" in overview
    assert "Container image" in overview and "Resource requests/limits" in overview
    assert "| <code>spec.replicas</code> | <code>3</code> | <code>4</code> |" in details
    assert "<details open>" not in body
    assert "--- a/rendered/backend-production/production/Deployment.apps/backend.yaml" in body
    assert "+++ b/rendered/payments-production/production/Deployment.apps/payments.yaml" in body
    assert "\n-  replicas: 3\n+  replicas: 4\n" in body
    assert "image: ghcr.io/example/backend:2.0.0" in body
    assert "cpu: 250m" in body and body.count("```diff") == 2 and "\n@@ -" in body


@pytest.mark.parametrize("action", ["add", "remove"])
def test_target_inventory(pair, run, action):
    configure(pair[0 if action == "add" else 1], [])
    out = run(pair)
    status = "Added" if action == "add" else "Removed"
    assert len(out["targets"]) == 6 and out["counts"][status] > 30 and not out["errors"]
    assert all(x[1] == status for x in out["targets"])
    assert ("--- /dev/null" if action == "add" else "+++ /dev/null") in markdown(out)


@pytest.mark.parametrize("tool", ["helm", "kustomize"])
def test_render_failure_is_not_deletion(pair, run, tool):
    for root in pair:
        configure(
            root,
            [
                {
                    "name": "sample",
                    "type": tool,
                    ("chart" if tool == "helm" else "path"): (
                        "examples/payments" if tool == "helm" else "examples/backend/base"
                    ),
                }
            ],
        )
    sentinel = "private" + "-fixture-error"
    if tool == "helm":
        (pair[1] / "examples/payments/templates/fail.yaml").write_text(
            '{{ fail "' + sentinel + '" }}'
        )
    else:
        (pair[1] / "examples/backend/base/workload.yaml").write_text(sentinel + ": [")
    out = run(pair)
    assert out["errors"][0][:2] == ["head", "sample"] and sum(out["counts"].values()) == 0
    safe = sentinel not in json.dumps(out) and "incomplete" in markdown(out)
    assert safe


def test_missing_values(pair, run):
    (pair[1] / "examples/environments/production/payments.yaml").unlink()
    out = run(pair)
    assert out["errors"] and not out["changes"]


def test_secret_and_policy_union(pair, run):
    old, new = "CANARY" + "-BEFORE", "CANARY" + "-AFTER"
    for root, token in zip(pair, [old, new], strict=True):
        cfg = root / ".manifest-diff.yaml"
        data = yaml.safe_load(cfg.read_text())
        data["targets"] = [t for t in data["targets"] if t["name"] == "payments-production"]
        data["sensitivePaths"] = ["/data/private"] if root == pair[0] else []
        cfg.write_text(yaml.safe_dump(data))
        with (root / "examples/environments/production/payments.yaml").open("a") as f:
            f.write(f"dummyToken: {token}\n")
        (root / "examples/payments/templates/config.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: test}\ndata:\n  private: extra-"
            + token
            + "\n"
        )
    out = run(pair)
    assert out["counts"]["Modified"] == 2
    safe = all(v not in json.dumps(out) + markdown(out) for v in (old, new))
    assert safe
    assert all(r[1:] == ["[redacted]", "[redacted]"] for c in out["changes"] for r in c["fields"])
    assert "Changed protected fields" in markdown(out)


def test_chart_capabilities_crds_hook_and_dependency(pair):
    root = pair[0]
    chart = root / "examples/payments"
    (chart / "crds").mkdir()
    (chart / "crds/widget.yaml").write_text(
        "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata: {name: widgets.sample.test}"
    )
    (chart / "templates/cap.yaml").write_text(
        '{{ if .Capabilities.APIVersions.Has "sample.test/v1" }}\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: capability}\ndata: {version: "{{ .Capabilities.KubeVersion.Version }}"}\n{{ end }}'
    )
    configure(
        root,
        [
            {
                "name": "sample",
                "type": "helm",
                "chart": "examples/payments",
                "apiVersions": ["sample.test/v1"],
                "kubeVersion": "1.31.2",
            }
        ],
    )
    docs = flatten(Renderer(False).render(root, inventory(root)[0]["sample"]))
    by_name = {x["metadata"]["name"]: x for x in docs}
    assert {"widgets.sample.test", "sample-audit", "sample-check", "capability"} <= by_name.keys()
    assert by_name["capability"]["data"]["version"] == "v1.31.2"


@pytest.mark.parametrize("name", ["kustomization.yaml", "kustomization.yml", "Kustomization"])
def test_filenames(tmp_path, name):
    (tmp_path / name).write_text("resources: []")
    assert list(inventory(tmp_path)[0]) == ["root"]


def test_overlapping_discovery_and_explicit_precedence(tmp_path):
    (tmp_path / "Chart.yaml").write_text("name: sample")
    (tmp_path / "Kustomization").write_text("resources: []")
    with pytest.raises(Problem, match="Overlapping"):
        inventory(tmp_path)
    configure(tmp_path, [])
    assert not inventory(tmp_path)[0]


@pytest.mark.parametrize(
    "value", ["../escape", "/tmp", "./missing", "https://example.test/x", "--post-renderer"]
)
def test_invalid_values_paths(pair, value):
    configure(
        pair[0], [{"name": "test", "type": "helm", "chart": "examples/payments", "values": [value]}]
    )
    with pytest.raises(Problem):
        inventory(pair[0])


def test_symlink_and_unknown_options(pair):
    root = pair[0]
    configure(
        root, [{"name": "x", "type": "helm", "chart": "examples/payments", "postRenderer": "bad"}]
    )
    with pytest.raises(Problem, match="Unsupported"):
        inventory(root)
    (root / "link").symlink_to(root.parent)
    with pytest.raises(Problem, match="Symbolic"):
        inventory(root)
