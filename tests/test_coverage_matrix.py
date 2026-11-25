"""Explicit tests for all 28 requirements of the Kubernetes Manifest Semantic Diff specification.

Covers:
1.  Helm-only repositories
2.  Kustomize-only repositories
3.  Mixed repositories
4.  Added resources
5.  Removed resources
6.  Modified resources
7.  Replica changes
8.  Image changes
9.  CPU and memory changes
10. Environment variable changes
11. YAML key reordering
12. Resource document reordering
13. Kubernetes list semantics
14. Nested object changes
15. Custom Resources
16. Duplicate resource identities
17. Invalid YAML
18. Failed Helm rendering
19. Failed Kustomize rendering
20. Missing values files
21. Added or removed rendering targets
22. Secret redaction
23. Oversized reports
24. Existing PR comment updates
25. Repeated workflow execution
26. Untrusted PR input
27. Artifact validation
28. No semantic changes
"""

import hashlib
import io
import json
import zipfile

import pytest
import yaml

from manifest_diff.config import inventory
from manifest_diff.input import Problem, documents, one
from manifest_diff.publish import BOT_ID, WORKFLOW, publish, unpack
from manifest_diff.render import Renderer
from manifest_diff.report import MARKER, markdown
from manifest_diff.semantic import fields, flatten, index


def make_k8s_obj(kind="Deployment", api="apps/v1", name="web", namespace=None, **extra):
    meta = {"name": name}
    if namespace is not None:
        meta["namespace"] = namespace
    return {"apiVersion": api, "kind": kind, "metadata": meta, **extra}


# 1. Helm-only repositories
def test_01_helm_only_repository(tmp_path):
    root = tmp_path / "helm_repo"
    chart_dir = root / "charts/my-app"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: my-app\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text("replicaCount: 2\nimage: nginx:1.25\n")
    templates = chart_dir / "templates"
    templates.mkdir()
    (templates / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\nspec:\n"
        "  replicas: {{ .Values.replicaCount }}\n  selector:\n    matchLabels:\n      app: {{ .Release.Name }}\n"
        "  template:\n    metadata:\n      labels:\n        app: {{ .Release.Name }}\n"
        "    spec:\n      containers:\n      - name: app\n        image: {{ .Values.image }}\n"
    )
    targets, _ = inventory(root)
    assert len(targets) == 1 and "charts-my-app" in targets
    target = targets["charts-my-app"]
    assert target.type == "helm"
    rendered = flatten(Renderer(isolated=False).render(root, target))
    assert len(rendered) == 1 and rendered[0]["kind"] == "Deployment"


# 2. Kustomize-only repositories
def test_02_kustomize_only_repository(tmp_path):
    root = tmp_path / "kustomize_repo"
    base = root / "k8s/base"
    base.mkdir(parents=True)
    (base / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n- service.yaml\n"
    )
    (base / "service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\nspec:\n  ports:\n  - port: 80\n    name: http\n"
    )
    overlay = root / "k8s/overlays/prod"
    overlay.mkdir(parents=True)
    (overlay / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n- ../../base\n"
        "namespace: production\n"
    )
    targets, _ = inventory(root)
    assert len(targets) == 1 and "k8s-overlays-prod" in targets
    target = targets["k8s-overlays-prod"]
    assert target.type == "kustomize"
    rendered = flatten(Renderer(isolated=False).render(root, target))
    assert len(rendered) == 1 and rendered[0]["metadata"]["namespace"] == "production"


# 3. Mixed repositories
def test_03_mixed_repository(pair, run):
    # Both Helm and Kustomize targets configured in .manifest-diff.yaml
    targets, _ = inventory(pair[0])
    types = {t.type for t in targets.values()}
    assert types == {"helm", "kustomize"}
    result = run(pair)
    assert not result["errors"]
    assert "No semantic changes" in markdown(result)


# 4. Added resources
def test_04_added_resources():
    base_docs = [make_k8s_obj("Service", "v1", "svc1")]
    head_docs = [make_k8s_obj("Service", "v1", "svc1"), make_k8s_obj("Service", "v1", "svc2")]
    idx_a = index(base_docs, "default", [], set())
    idx_b = index(head_docs, "default", [], set())
    added = [k for k in idx_b if k not in idx_a]
    assert len(added) == 1 and added[0] == ("", "Service", "default", "svc2")


# 5. Removed resources
def test_05_removed_resources():
    base_docs = [make_k8s_obj("Service", "v1", "svc1"), make_k8s_obj("Service", "v1", "svc2")]
    head_docs = [make_k8s_obj("Service", "v1", "svc1")]
    idx_a = index(base_docs, "default", [], set())
    idx_b = index(head_docs, "default", [], set())
    removed = [k for k in idx_a if k not in idx_b]
    assert len(removed) == 1 and removed[0] == ("", "Service", "default", "svc2")


# 6. Modified resources
def test_06_modified_resources():
    a = make_k8s_obj(spec={"replicas": 1})
    b = make_k8s_obj(spec={"replicas": 3})
    diffs = list(fields(a, b, "apps", "Deployment"))
    assert len(diffs) == 1 and diffs[0] == ("spec.replicas", "1", "3")


# 7. Replica changes
def test_07_replica_changes():
    a = make_k8s_obj(spec={"replicas": 2})
    b = make_k8s_obj(spec={"replicas": 4})
    diffs = list(fields(a, b, "apps", "Deployment"))
    assert diffs == [("spec.replicas", "2", "4")]


# 8. Image changes
def test_08_image_changes():
    a = make_k8s_obj(
        spec={"template": {"spec": {"containers": [{"name": "app", "image": "backend:v1"}]}}}
    )
    b = make_k8s_obj(
        spec={"template": {"spec": {"containers": [{"name": "app", "image": "backend:v2"}]}}}
    )
    diffs = list(fields(a, b, "apps", "Deployment"))
    assert diffs == [("spec.template.spec.containers[app].image", '"backend:v1"', '"backend:v2"')]


# 9. CPU and memory changes
def test_09_cpu_and_memory_changes():
    res_a = {"requests": {"cpu": "100m"}, "limits": {"memory": "256Mi"}}
    res_b = {"requests": {"cpu": "250m"}, "limits": {"memory": "512Mi"}}
    a = make_k8s_obj(
        spec={"template": {"spec": {"containers": [{"name": "c", "resources": res_a}]}}}
    )
    b = make_k8s_obj(
        spec={"template": {"spec": {"containers": [{"name": "c", "resources": res_b}]}}}
    )
    diffs = {r[0]: r[1:] for r in fields(a, b, "apps", "Deployment")}
    assert diffs["spec.template.spec.containers[c].resources.requests.cpu"] == ('"100m"', '"250m"')
    assert diffs["spec.template.spec.containers[c].resources.limits.memory"] == (
        '"256Mi"',
        '"512Mi"',
    )


# 10. Environment variable changes
def test_10_environment_variable_changes():
    env_a = [{"name": "PORT", "value": "8080"}]
    env_b = [{"name": "PORT", "value": "9090"}, {"name": "LOG_LEVEL", "value": "debug"}]
    a = make_k8s_obj(spec={"template": {"spec": {"containers": [{"name": "c", "env": env_a}]}}})
    b = make_k8s_obj(spec={"template": {"spec": {"containers": [{"name": "c", "env": env_b}]}}})
    diffs = {r[0]: r[1:] for r in fields(a, b, "apps", "Deployment")}
    assert diffs["spec.template.spec.containers[c].env[PORT].value"] == ('"8080"', '"9090"')
    assert diffs["spec.template.spec.containers[c].env[LOG_LEVEL].value"] == ("∅", '"debug"')


# 11. YAML key reordering
def test_11_yaml_key_reordering():
    yaml_a = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\ndata:\n  alpha: '1'\n  beta: '2'\n"
    yaml_b = "kind: ConfigMap\napiVersion: v1\ndata:\n  beta: '2'\n  alpha: '1'\nmetadata:\n  name: cfg\n"
    doc_a = documents(yaml_a.encode())[0]
    doc_b = documents(yaml_b.encode())[0]
    idx_a = index([doc_a], "default", [], set())
    idx_b = index([doc_b], "default", [], set())
    assert idx_a == idx_b
    assert not list(fields(doc_a, doc_b, "", "ConfigMap"))


# 12. Resource document reordering
def test_12_resource_document_reordering():
    doc1 = make_k8s_obj("ConfigMap", "v1", "cm-1")
    doc2 = make_k8s_obj("Service", "v1", "svc-1")
    doc3 = make_k8s_obj("Deployment", "apps/v1", "dep-1")
    idx_a = index([doc1, doc2, doc3], "default", [], set())
    idx_b = index([doc3, doc1, doc2], "default", [], set())
    assert idx_a == idx_b


# 13. Kubernetes list semantics
def test_13_kubernetes_list_semantics():
    # Regular containers: matched by container name, order is insensitive
    c1 = {"name": "app", "image": "img1"}
    c2 = {"name": "sidecar", "image": "img2"}
    a = make_k8s_obj(spec={"template": {"spec": {"containers": [c1, c2]}}})
    b = make_k8s_obj(spec={"template": {"spec": {"containers": [c2, c1]}}})
    assert not list(fields(a, b, "apps", "Deployment"))

    # InitContainers: matched by name, but reports relative order change
    a_init = make_k8s_obj(spec={"template": {"spec": {"initContainers": [c1, c2]}}})
    b_init = make_k8s_obj(spec={"template": {"spec": {"initContainers": [c2, c1]}}})
    diffs = list(fields(a_init, b_init, "apps", "Deployment"))
    assert len(diffs) == 1 and diffs[0][0] == "spec.template.spec.initContainers.order"

    # Command arguments: positional list where order is strictly preserved
    a_cmd = make_k8s_obj(
        spec={
            "template": {
                "spec": {"containers": [{"name": "app", "args": ["--start", "--verbose"]}]}
            }
        }
    )
    b_cmd = make_k8s_obj(
        spec={
            "template": {
                "spec": {"containers": [{"name": "app", "args": ["--verbose", "--start"]}]}
            }
        }
    )
    diffs_cmd = list(fields(a_cmd, b_cmd, "apps", "Deployment"))
    assert len(diffs_cmd) == 2


# 14. Nested object changes
def test_14_nested_object_changes():
    a = make_k8s_obj(
        spec={
            "template": {
                "spec": {
                    "securityContext": {
                        "runAsNonRoot": True,
                        "sysctls": [{"name": "net.ipv4.ip_forward", "value": "0"}],
                    }
                }
            }
        }
    )
    b = make_k8s_obj(
        spec={
            "template": {
                "spec": {
                    "securityContext": {
                        "runAsNonRoot": False,
                        "sysctls": [{"name": "net.ipv4.ip_forward", "value": "1"}],
                    }
                }
            }
        }
    )
    diffs = {r[0]: r[1:] for r in fields(a, b, "apps", "Deployment")}
    assert diffs["spec.template.spec.securityContext.runAsNonRoot"] == ("true", "false")
    assert diffs["spec.template.spec.securityContext.sysctls[0].value"] == ('"0"', '"1"')


# 15. Custom Resources
def test_15_custom_resources():
    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "databases.corp.internal"},
        "spec": {"group": "corp.internal", "names": {"kind": "Database"}, "scope": "Cluster"},
    }
    cr1 = {
        "apiVersion": "corp.internal/v1",
        "kind": "Database",
        "metadata": {"name": "main-db"},
        "spec": {"storage": "100Gi"},
    }
    cr2 = {
        "apiVersion": "corp.internal/v1",
        "kind": "Database",
        "metadata": {"name": "main-db"},
        "spec": {"storage": "200Gi"},
    }
    idx_a = index([crd, cr1], "ignored", [], set())
    idx_b = index([crd, cr2], "ignored", [], set())
    # Scope is Cluster, so namespace in key is empty string
    key = ("corp.internal", "Database", "", "main-db")
    assert key in idx_a and key in idx_b
    diffs = list(fields(idx_a[key], idx_b[key], "corp.internal", "Database"))
    assert diffs == [("spec.storage", '"100Gi"', '"200Gi"')]


# 16. Duplicate resource identities
def test_16_duplicate_resource_identities():
    doc1 = make_k8s_obj("ConfigMap", "v1", "dup-cm", namespace="test")
    doc2 = make_k8s_obj("ConfigMap", "v1", "dup-cm", namespace="test")
    with pytest.raises(Problem, match="Duplicate"):
        index([doc1, doc2], "test", [], set())


# 17. Invalid YAML
def test_17_invalid_yaml():
    with pytest.raises(Problem, match="Invalid YAML"):
        documents(b"invalid: yaml: [syntax error")
    with pytest.raises(Problem, match="YAML aliases are unsupported"):
        documents(b"base: &b {val: 1}\nextend: *b\n")


# 18. Failed Helm rendering
def test_18_failed_helm_rendering(pair, run):
    fail_template = pair[1] / "examples/payments/templates/broken.yaml"
    fail_template.write_text('{{ fail "deliberate chart error" }}')
    res = run(pair)
    assert any(
        e[1] == "payments-production" and "deliberate chart error" not in e[2]
        for e in res["errors"]
    )
    assert res["counts"] == {"Added": 0, "Modified": 0, "Removed": 0}


# 19. Failed Kustomize rendering
def test_19_failed_kustomize_rendering(pair, run):
    kust_file = pair[1] / "examples/backend/overlays/production/kustomization.yaml"
    kust_file.write_text("resources:\n- nonexistent-dir\n")
    res = run(pair)
    assert any(e[1] == "backend-production" for e in res["errors"])


# 20. Missing values files
def test_20_missing_values_files(pair):
    cfg_path = pair[0] / ".manifest-diff.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["targets"][1]["values"].append("examples/environments/nonexistent.yaml")
    cfg_path.write_text(yaml.safe_dump(data))
    with pytest.raises(Problem, match="values file is missing"):
        inventory(pair[0])


# 21. Added or removed rendering targets
def test_21_added_or_removed_rendering_targets(pair, run):
    cfg_b = pair[1] / ".manifest-diff.yaml"
    data = yaml.safe_load(cfg_b.read_text())
    # Remove last target
    removed = data["targets"].pop()
    cfg_b.write_text(yaml.safe_dump(data))
    res = run(pair)
    target_names = [t[0] for t in res["targets"]]
    assert removed["name"] in target_names
    assert [t[1] for t in res["targets"] if t[0] == removed["name"]][0] == "Removed"


# 22. Secret redaction
def test_22_secret_redaction():
    secret_raw = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "app-secret"},
        "data": {"API_KEY": "c2VjcmV0LXBhc3N3b3Jk"},
        "stringData": {"TOKEN": "plain-text-token-value"},
    }
    known = set()
    idx = index([secret_raw], "default", [], known)
    masked_sec = idx[("", "Secret", "default", "app-secret")]
    assert "plain-text-token-value" in known
    assert "secret-password" in known
    # Field diff must show [redacted] for secret fields
    diffs = {r[0]: r[1:] for r in fields({}, masked_sec, "", "Secret")}
    for _, new_val in diffs.values():
        assert "plain-text-token-value" not in str(new_val)
        assert "secret-password" not in str(new_val)
    assert diffs["data.API_KEY"] == ("∅", "[redacted]")
    assert diffs["stringData.TOKEN"] == ("∅", "[redacted]")


# 23. Oversized reports
def test_23_oversized_reports():
    big_report = {
        "counts": {"Added": 0, "Modified": 400, "Removed": 0},
        "changes": [
            {
                "target": f"target-{i}",
                "app": f"app-{i}",
                "env": "prod",
                "resource": ["apps", "Deployment", "prod", f"dep-{i}"],
                "status": "Modified",
                "fields": [["spec.replicas", "1", "2"]],
            }
            for i in range(400)
        ],
        "errors": [],
        "targets": [],
        "truncated": True,
    }
    comment = markdown(big_report, "1234567890abcdef1234567890abcdef12345678")
    assert len(comment.encode("utf-8")) <= 60_000
    assert "exceed the review limit" in comment and "400 modified" in comment


# 24. Existing PR comment updates
def test_24_existing_pr_comment_updates():
    ctx = {
        "repo": "test/repo",
        "pr": 42,
        "base": "a" * 40,
        "head": "b" * 40,
        "run": 100,
        "attempt": 1,
    }
    calls = []

    def mock_request(path, method="GET", body=None, binary=False):
        calls.append((method, path, body))
        if "/runs/" in path:
            return {
                "id": 100,
                "run_attempt": 1,
                "event": "pull_request_target",
                "path": WORKFLOW,
                "workflow_id": 1,
                "head_sha": "a" * 40,
                "repository": {"id": 2, "full_name": "test/repo"},
            }
        if "/workflows/" in path:
            return {"id": 1, "path": WORKFLOW}
        if "/artifacts/" in path and not path.endswith("/zip"):
            raw, digest = _pack_test_artifact(ctx)
            return {
                "id": 50,
                "name": "semantic-diff-100-1",
                "expired": False,
                "size_in_bytes": len(raw),
                "digest": digest,
                "workflow_run": {"id": 100, "repository_id": 2, "head_sha": "a" * 40},
            }
        if path.endswith("/zip"):
            raw, _ = _pack_test_artifact(ctx)
            return raw
        if "/pulls/" in path:
            return {
                "number": 42,
                "state": "open",
                "head": {"sha": "b" * 40},
                "base": {"sha": "a" * 40, "repo": {"full_name": "test/repo"}},
            }
        if "/issues/42/comments" in path:
            return [
                {
                    "id": 1001,
                    "body": MARKER + "\nold comment content",
                    "user": {"id": BOT_ID, "login": "github-actions[bot]"},
                }
            ]
        return {}

    publish(ctx, 50, mock_request)
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1 and patch_calls[0][1].endswith("/1001")


# 25. Repeated workflow execution
def test_25_repeated_workflow_execution():
    ctx = {
        "repo": "test/repo",
        "pr": 42,
        "base": "a" * 40,
        "head": "b" * 40,
        "run": 100,
        "attempt": 1,
    }
    calls = []
    # Build identical body as what publish produces
    test_report = {
        "counts": {"Added": 0, "Modified": 0, "Removed": 0},
        "changes": [],
        "errors": [],
        "targets": [],
        "truncated": False,
    }
    expected_body = markdown(test_report, ctx["head"])

    def mock_request(path, method="GET", body=None, binary=False):
        calls.append(method)
        if "/runs/" in path:
            return {
                "id": 100,
                "run_attempt": 1,
                "event": "pull_request_target",
                "path": WORKFLOW,
                "workflow_id": 1,
                "head_sha": "a" * 40,
                "repository": {"id": 2, "full_name": "test/repo"},
            }
        if "/workflows/" in path:
            return {"id": 1, "path": WORKFLOW}
        if "/artifacts/" in path and not path.endswith("/zip"):
            raw, digest = _pack_test_artifact(ctx, test_report)
            return {
                "id": 50,
                "name": "semantic-diff-100-1",
                "expired": False,
                "size_in_bytes": len(raw),
                "digest": digest,
                "workflow_run": {"id": 100, "repository_id": 2, "head_sha": "a" * 40},
            }
        if path.endswith("/zip"):
            raw, _ = _pack_test_artifact(ctx, test_report)
            return raw
        if "/pulls/" in path:
            return {
                "number": 42,
                "state": "open",
                "head": {"sha": "b" * 40},
                "base": {"sha": "a" * 40, "repo": {"full_name": "test/repo"}},
            }
        if "/issues/42/comments" in path:
            return [
                {
                    "id": 1001,
                    "body": expected_body,
                    "user": {"id": BOT_ID, "login": "github-actions[bot]"},
                }
            ]
        return {}

    publish(ctx, 50, mock_request)
    # Since body is already up to date, NO PATCH, NO POST, NO DELETE should occur
    mutating = [m for m in calls if m in ("POST", "PATCH", "DELETE")]
    assert mutating == []


# 26. Untrusted PR input
def test_26_untrusted_pr_input(tmp_path):
    # Absolute paths, path traversals, backslashes, control characters
    for evil in (
        "/etc/passwd",
        "../outside",
        "dir/../../escape",
        "path\\with\\backslash",
        "path\nnewline",
    ):
        with pytest.raises(Problem):
            from manifest_diff.input import local

            local(tmp_path, evil)


# 27. Artifact validation
def test_27_artifact_validation():
    ctx = {"repo": "test/repo", "pr": 1, "base": "a" * 40, "head": "b" * 40, "run": 1, "attempt": 1}
    # Tampered sha in context
    tampered_ctx = {**ctx, "head": "f" * 40}
    raw, digest = _pack_test_artifact(tampered_ctx)
    with pytest.raises(ValueError):
        unpack(raw, digest, ctx)

    # Bad hash digest
    raw_valid, _ = _pack_test_artifact(ctx)
    with pytest.raises(ValueError):
        unpack(
            raw_valid,
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            ctx,
        )


# 28. No semantic changes
def test_28_no_semantic_changes(pair, run):
    res = run(pair)
    assert res["counts"] == {"Added": 0, "Modified": 0, "Removed": 0}
    assert res["changes"] == []
    md = markdown(res)
    assert "No semantic changes found in rendered resources." in md


def test_edge_cases_config_and_input_limits(tmp_path):
    # Multiple kustomization files in same dir
    (tmp_path / "kustomization.yaml").write_text("resources: []")
    (tmp_path / "kustomization.yml").write_text("resources: []")
    with pytest.raises(Problem, match="share a directory"):
        inventory(tmp_path)
    (tmp_path / "kustomization.yml").unlink()

    # Invalid config version or keys
    cfg = tmp_path / ".manifest-diff.yaml"
    cfg.write_text("version: 2\ntargets: []\n")
    with pytest.raises(Problem, match="version 1"):
        inventory(tmp_path)

    # Invalid sensitivePaths format
    cfg.write_text("version: 1\nsensitivePaths: [not_absolute]\ntargets: []\n")
    with pytest.raises(Problem, match="Sensitive paths"):
        inventory(tmp_path)

    # Invalid target type
    cfg.write_text("version: 1\ntargets:\n  - name: bad\n    type: unknown\n")
    with pytest.raises(Problem, match="Target type must be helm or kustomize"):
        inventory(tmp_path)

    # Invalid helm api capability
    cfg.write_text(
        "version: 1\ntargets:\n  - name: bad-api\n    type: helm\n    chart: .\n    apiVersions: ['bad api with space']\n"
    )
    with pytest.raises(Problem, match="Invalid Helm API capability"):
        inventory(tmp_path)

    # Invalid clusterScoped format
    cfg.write_text(
        "version: 1\ntargets:\n  - name: bad-cs\n    type: helm\n    chart: .\n    clusterScoped: ['NoGroup']\n"
    )
    with pytest.raises(Problem, match="clusterScoped"):
        inventory(tmp_path)

    # Duplicate target names
    cfg.write_text(
        "version: 1\ntargets:\n  - name: dup\n    type: kustomize\n    path: .\n  - name: dup\n    type: kustomize\n    path: .\n"
    )
    with pytest.raises(Problem, match="Duplicate target names"):
        inventory(tmp_path)

    # Non-list targets or exceeding 128
    cfg.write_text("version: 1\ntargets: 'not-a-list'\n")
    with pytest.raises(Problem, match="Expected at most 128"):
        inventory(tmp_path)

    # Cyclic Kustomize references
    (tmp_path / ".manifest-diff.yaml").unlink()
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "kustomization.yaml").write_text("resources: [../d2]\n")
    (d2 / "kustomization.yaml").write_text("resources: [../d1]\n")
    with pytest.raises(Problem, match="Cyclic"):
        inventory(tmp_path)


def test_edge_cases_input_validation(tmp_path):
    # Oversized documents
    with pytest.raises(Problem, match="8 MiB"):
        documents(b"x: " + b"a" * (8 * 1024 * 1024 + 1))

    # Deeply nested YAML exceeding depth limit
    deep_yaml = "a: " * 85 + "1" + "}" * 0
    with pytest.raises(Problem):
        documents(deep_yaml.encode())

    # Config file exceeding 512 kB
    big_cfg = tmp_path / "big.yaml"
    big_cfg.write_text("a: " + "b" * 600_000)
    with pytest.raises(Problem, match="size limit"):
        one(big_cfg)


def test_edge_cases_engine_budget(pair):
    # Test large number of field changes triggering budget
    a = make_k8s_obj(
        spec={
            "template": {
                "spec": {"containers": [{"name": f"c{i}", "image": f"v1-{i}"} for i in range(300)]}
            }
        }
    )
    b = make_k8s_obj(
        spec={
            "template": {
                "spec": {"containers": [{"name": f"c{i}", "image": f"v2-{i}"} for i in range(300)]}
            }
        }
    )
    diffs = list(fields(a, b, "apps", "Deployment"))
    assert len(diffs) >= 256


def test_edge_cases_publish_main(monkeypatch):
    from manifest_diff import publish as pub

    monkeypatch.setattr(pub, "context", lambda: {"repo": "test/repo"})
    monkeypatch.setattr(pub, "publish", lambda ctx, art_id: "Success")
    monkeypatch.setenv("ARTIFACT_ID", "123")
    assert pub.main() == 0

    monkeypatch.setattr(
        pub, "publish", lambda ctx, art_id: (_ for _ in ()).throw(ValueError("failed"))
    )
    assert pub.main() == 1


def test_edge_cases_renderer_checks(monkeypatch, tmp_path):
    import shutil

    from manifest_diff.config import Target

    target = Target(
        name="t",
        type="helm",
        path=tmp_path,
        app="a",
        env="e",
        namespace="default",
        release="r",
        values=(),
        kube_version="1.32.0",
        apis=(),
        cluster_scoped=(),
    )
    r = Renderer(isolated=False)
    monkeypatch.setattr(shutil, "which", lambda tool: None)
    with pytest.raises(Problem, match="Pinned renderer is missing"):
        r.render(tmp_path, target)

    monkeypatch.setattr(shutil, "which", lambda tool: "/usr/bin/helm")
    monkeypatch.setattr("manifest_diff.render.capture", lambda cmd, env, cwd: b"v999.0.0\n")
    with pytest.raises(Problem, match="Renderer version differs"):
        r.render(tmp_path, target)


def test_edge_cases_cli_invalid_roots(monkeypatch, tmp_path, capsys):
    import sys

    from manifest_diff.cli import main

    file_a = tmp_path / "a.txt"
    file_a.write_text("not a dir")
    monkeypatch.setattr(sys, "argv", ["manifest-diff", str(file_a), str(tmp_path)])
    assert main() == 2
    assert "could not complete" in capsys.readouterr().err


def _pack_test_artifact(ctx, rep=None):
    if rep is None:
        rep = {
            "counts": {"Added": 0, "Modified": 0, "Removed": 0},
            "changes": [],
            "errors": [],
            "targets": [],
            "truncated": False,
        }
    sink = io.BytesIO()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.json", json.dumps({"version": 1, "context": ctx, "report": rep}))
    raw = sink.getvalue()
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()
