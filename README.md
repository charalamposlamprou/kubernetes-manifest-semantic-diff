# Kubernetes Manifest Semantic Diff

Review the Kubernetes objects a pull request would produce. A change to a Helm values file or a Kustomize patch appears as a field change on the resulting workload. Formatting-only edits produce a clean review. One bot-owned comment follows the PR across pushes.

The public validation PR exercised both renderers. Its first review found two modified Deployments. A subsequent push added a Secret rotation and updated the same comment. [Validation PR](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/pull/1) · [Initial review run](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/actions/runs/44908013477) · [Secret rotation run](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/actions/runs/44908163884)

## How a review works

```mermaid
flowchart LR
    E[Trusted PR target event] --> B[Exact base SHA]
    E --> H[Exact head SHA]
    B --> I[Independent target inventories]
    H --> I
    I --> K[Offline Kustomize build]
    I --> T[Offline Helm template]
    K --> D[Resource identity and field comparison]
    T --> D
    D --> A[Redacted structured artifact]
    A --> V[Separate publisher verifies run, artifact and current PR]
    V --> C[Create or update one comment]
```

The review workflow runs on opened, reopened and synchronize events. Each target renders independently on both sides. A missing target counts as an inventory change, even if it renders zero objects. A failed target contributes a diagnostic and no resource counts. Other successfully rendered targets can still report changes. A configuration error invalidates the inventory comparison. The publishing job marks incomplete comparisons as failed after posting the diagnostic.

## Try the sample

Use Python 3.12 or newer and install Helm **4.1.1** and Kustomize **5.8.1** on your path. The native mode in this example is for trusted local trees. Outputs go to stdout. Raw renderer diagnostics stay private.

```sh
git clone https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff.git
cd kubernetes-manifest-semantic-diff
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
base=$(mktemp -d)
git archive HEAD | tar -x -C "$base"
git apply examples/review.patch
manifest-diff --native "$base" .
git apply -R examples/review.patch
```

The patch changes the production backend's replica count and image tag, and the production payments container's CPU request. Development and staging use the same bases and retain their original settings. All Secret inputs in the samples are deliberately fictitious.

For isolated rendering, run `bash ci/build-renderers.sh` on a Linux amd64 Docker host. Pass two exported revision directories without Git metadata to `manifest-diff BASE_DIR HEAD_DIR`. The default mode uses the locally built scratch image. Each invocation receives one read-only revision mount, a bounded memory-backed temporary directory, and no network. Use clean exports for local comparisons, especially when working directories might contain credentials.

## Configure the deployment inventory

Place `.manifest-diff.yaml` at the root. Target names are stable comparison keys. Set `application` and `environment` to group a review. The default application label is the target name. The default environment is `default`.

```yaml
version: 1
sensitivePaths:
  - /metadata/annotations/example.com~1credential
  - /spec/template/spec/containers/*/env/*/value
targets:
  - name: backend-production
    type: kustomize
    application: backend
    environment: production
    path: examples/backend/overlays/production

  - name: payments-production
    type: helm
    application: payments
    environment: production
    chart: examples/payments
    release: payments
    namespace: production
    values:
      - examples/payments/values.yaml
      - examples/environments/production/payments.yaml
    kubeVersion: "1.32.0"
    apiVersions: [networking.k8s.io/v1]
    clusterScoped: [platform.example.com/Region]
```

| Option | Behavior |
|---|---|
| `type`, `name` | Required. Choose `helm` or `kustomize` and a unique stable name. |
| `chart`, `path` | Repository-relative chart or kustomization directory respectively. |
| `values` | Repository-relative files, in override order. Later files win. |
| `release`, `namespace` | Helm defaults to the target name and namespace `default`. For both tools, namespace supplies the comparison namespace when a namespaced object omits one. Kustomize namespace transformations belong in the kustomization. |
| `kubeVersion`, `apiVersions` | Helm capability inputs. Default Kubernetes version is `1.32.0`. API entries extend Helm's built-in capabilities. |
| `clusterScoped` | Target-local `group/Kind` entries for external cluster-scoped CRDs. CRDs included in the render supply their own scope. |
| `sensitivePaths` | Absolute JSON pointers into individual objects. A segment of `*` matches one key or list index. Use `~1` for a literal slash and `~0` for a tilde. The policy is the union of both revisions. |
| `targets: []` | An explicitly empty inventory. Automatic discovery stays disabled. |

Without a config, discovery looks for `kustomization.yaml`, `kustomization.yml`, `Kustomization`, and `Chart.yaml`. It follows local Kustomize resource/base/component references to exclude referenced bases, excludes components as standalone targets, and skips library and vendored Helm subcharts. Disjoint deployable roots become separate targets. Overlapping roots, name collisions, cycles, and competing kustomization filenames require an explicit selection or a repository fix. Declare an inventory when a repository also contains independent examples, fixtures or charts that should stay out of deployment reviews. Renaming a target appears as a removal and an addition.

## Renderer and comparison behavior

- Kustomize uses its built-in generators, components, patch and transformation pipeline with root-only load restrictions. Local directory references between overlays and bases work. Generator suffixes and updated consumer references are visible in the diff.
- Helm renders local charts, vendored dependencies in `charts/`, CRDs and hook definitions. No hook runs. Vendor dependencies before committing and supply fixed capability inputs. Two consecutive Helm renders must agree after mapping and document ordering are normalized. Templates depending on the clock, random values or live lookups are unsuitable for reproducible reviews.
- Resources match within each target by API group, kind, effective namespace and name. The complete API version participates in comparison. A same-group served-version migration appears as an `apiVersion` field change. A group or kind migration appears as removal and addition. Duplicate logical identities fail that target. Core `v1/List` objects expand into their members.
- Built-in cluster kinds and rendered CRD scope control namespace handling. For an external custom kind with no scope declaration, the engine assumes namespaced scope. Explicit namespaces on known cluster-scoped objects fail validation.
- Mappings and resource document order are insignificant. Named regular containers, volumes and named Service ports match by name. Init containers and environment entries match by name and report relative order changes. Their execution or expansion can depend on order. Duplicate or missing list keys fall back to positional comparison. Commands, arguments, CR lists and other unclassified lists compare positionally. Kubernetes quantity spellings, equivalent RBAC rule formulations and schema defaults can therefore produce review differences.
- Secret `data` and `stringData` values and a Secret's last-applied annotation are protected in memory before the report is built. Changed fields show `[redacted]` on both sides. Secret key names, object identity and resource counts remain reviewable. A repeated known sensitive literal in another displayed field is also suppressed. Apply explicit paths to application-specific credentials. A transformed credential intentionally placed into an unrelated public field requires its own redaction policy.

## GitHub Actions setup and adoption

In this repository the workflow is ready on the default branch. Enable Actions and permit the workflow's declared token permissions. Use GitHub-hosted Ubuntu runners. A fork PR can be reviewed with the base repository's trusted workflow. The publication path targets GitHub.com and its GitHub Actions bot identity.

To use the implementation in another repository:

1. Copy **only** `.github/workflows/kustomize-diff.yaml` to the same path in the consumer. Keep the filename because the publisher verifies its identity.
2. In the steps **Check out trusted engine** and **Check out trusted publisher**, add `repository: charalamposlamprou/kubernetes-manifest-semantic-diff` under `with` and set `ref` in both to the same reviewed, immutable release commit SHA. Leave the two input checkouts pointed at the consumer's event SHAs. A public engine repository is readable using the standard token.
3. Add the consumer's deployment inventory where needed. Merge the workflow to its default branch and open a PR. Update the two engine pins together when upgrading.

The versioned Python package also exposes `manifest-diff` as a console entry point. It can be installed from an immutable Git revision for local tools. The GitHub publisher has no third-party Python dependency. Renderer argument adapters live in `manifest_diff/render.py`. Resource comparison consumes ordinary Kubernetes objects regardless of their renderer.

### Trust and permissions

| Job | Token permissions | Inputs and responsibilities |
|---|---|---|
| Compare | `contents: read` | Check out trusted engine code and exact PR snapshots separately. Verify both checkout SHAs, detach Git metadata, and run the engine from the trusted directory. |
| Publish | `contents: read`, `actions: read`, `pull-requests: write` | Fetch the trusted publisher and one exact same-run artifact. Verify run, attempt, workflow path/ID, repository, artifact ID/name, SHA-256 digest, PR number, base/head SHAs and current open PR. |
| Engine tests | `contents: read` | Test trusted main pushes or manual dispatch. |

There are no PR-controlled shell fragments, Helm plugins, post-renderers, dependency downloads, external Kustomize plugins, repository scripts or cluster credentials in the rendering path. The scratch image contains two checksum-pinned binaries. Docker drops capabilities, disables its log driver, caps CPU, memory, processes and runtime, and gives the non-root user a read-only root filesystem. Rendered stdout is bounded and processed in memory. Stderr is discarded because renderer errors can quote Secrets.

Only the versioned redacted JSON envelope is uploaded, with one-day retention. The publisher reads a single bounded `report.json` ZIP member in memory, validates the schema, builds bounded Markdown itself and encodes untrusted text. It never extracts to a filesystem. The workflow serializes runs per PR, skips superseded heads, and updates only comments owned by the GitHub Actions bot with the exact marker. User-authored lookalikes are left alone. Repeated execution with an identical body needs no write.

## Example review

Applying `examples/review.patch` produces the following review. The file headers identify normalized render outputs for each target and resource. GitHub highlights additions and removals inside each expanded section. Hunk line numbers refer to the normalized YAML. Large resources can fall back to a field-only diff, and protected rotations identify the changed field.

### Kubernetes Manifest Semantic Diff

**Summary**

| Change | Count |
|---|---:|
| Added | 0 |
| Modified | 2 |
| Removed | 0 |

**Result** · 2 resource changes.

#### <code>backend</code> / <code>production</code> · <code>backend-production</code>

<details open><summary>Modified · <code>Deployment.apps</code> / <code>production</code> / <code>backend</code></summary>

```diff
--- a/rendered/backend-production/production/Deployment.apps/backend.yaml
+++ b/rendered/backend-production/production/Deployment.apps/backend.yaml
@@ -4,7 +4,7 @@
   name: backend
   namespace: production
 spec:
-  replicas: 3
+  replicas: 4
   selector:
     matchLabels:
       app: backend
@@ -23,7 +23,7 @@
         envFrom:
         - configMapRef:
             name: backend-settings-hf678c7m2b
-        image: ghcr.io/example/backend:1.0.0
+        image: ghcr.io/example/backend:1.1.0
         name: backend
         ports:
         - containerPort: 8080
```

Field paths · <code>spec.replicas</code>, <code>spec.template.spec.containers&#91;backend&#93;.image</code>

</details>

#### <code>payments</code> / <code>production</code> · <code>payments-production</code>

<details open><summary>Modified · <code>Deployment.apps</code> / <code>production</code> / <code>payments</code></summary>

```diff
--- a/rendered/payments-production/production/Deployment.apps/payments.yaml
+++ b/rendered/payments-production/production/Deployment.apps/payments.yaml
@@ -30,5 +30,5 @@
             cpu: 500m
             memory: 256Mi
           requests:
-            cpu: 100m
+            cpu: 250m
             memory: 128Mi
```

Field paths · <code>spec.template.spec.containers&#91;payments&#93;.resources.requests.cpu</code>

</details>

An unchanged render says **No semantic changes found in rendered resources.** Oversized output falls back to counts and a notice. RBAC, Secret, NetworkPolicy and security-context changes carry a security-sensitive label. Review the field changes to determine whether access expanded or contracted.

## Development and validation

```sh
python -m pip install --require-hashes -r ci/dev.lock
ruff check .
ruff format --check .
pytest --cov=manifest_diff --cov-report=term-missing
actionlint
python -m build
```

Install `build` separately for the optional package build. Tests invoke the pinned native Helm and Kustomize executables and exercise embedded chart dependencies, hooks, CRDs, overlays, ordered values, discovery, normalization, redaction, artifact validation, comment reconciliation, and an exhaustive 28-point requirements matrix. Main CI additionally runs the Docker production path and checksum-verified actionlint 1.7.12. Regenerate dependency locks using `uv pip compile --universal --python-version 3.12 --generate-hashes` on the respective `.in` files.

### Test coverage metrics

The test suite contains **104 tests** executing in ~9 seconds with **94% statement coverage**:

| Module | Statements | Miss | Coverage | Notes |
|---|---:|---:|---:|---|
| `manifest_diff/__init__.py` | 0 | 0 | 100% | Package root |
| `manifest_diff/report.py` | 123 | 2 | 98% | Markdown reports, summary counts, code fences |
| `manifest_diff/cli.py` | 29 | 1 | 97% | CLI commands, entry points, argument handling |
| `manifest_diff/config.py` | 116 | 4 | 97% | Discovery, target inventories, policy parsing |
| `manifest_diff/patch.py` | 38 | 2 | 95% | Unified YAML diff hunks, ordered lists |
| `manifest_diff/input.py` | 64 | 4 | 94% | Hardened YAML loader, traversal limits |
| `manifest_diff/semantic.py` | 166 | 12 | 93% | Canonical identities, CRDs, list strategies |
| `manifest_diff/engine.py` | 106 | 9 | 92% | Diff orchestration, budget management |
| `manifest_diff/publish.py` | 85 | 7 | 92% | Artifact provenance, idempotent comment sync |
| `manifest_diff/render.py` | 76 | 7 | 91% | Helm & Kustomize adapters, Docker sandbox |
| **TOTAL** | **803** | **48** | **94%** | **104 passed** |

### Comprehensive 28-requirement verification matrix

The dedicated test module `tests/test_coverage_matrix.py` directly validates each requirement from the specification:

| # | Requirement | Test Function | Status | Verification Detail |
|---|---|---|:---:|---|
| 1 | Helm-only repositories | `test_01_helm_only_repository` | PASSED | Auto-discovery and rendering of standalone Helm chart |
| 2 | Kustomize-only repositories | `test_02_kustomize_only_repository` | PASSED | Auto-discovery and rendering of standalone Kustomize overlay |
| 3 | Mixed repositories | `test_03_mixed_repository` | PASSED | Simultaneous rendering of Helm and Kustomize targets |
| 4 | Added resources | `test_04_added_resources` | PASSED | Added Service resource detected in diff and counts |
| 5 | Removed resources | `test_05_removed_resources` | PASSED | Removed Service resource detected in diff and counts |
| 6 | Modified resources | `test_06_modified_resources` | PASSED | In-place workload mutation detected |
| 7 | Replica changes | `test_07_replica_changes` | PASSED | `spec.replicas`: 2 → 4 detected |
| 8 | Image changes | `test_08_image_changes` | PASSED | `containers[app].image`: `backend:v1` → `backend:v2` |
| 9 | CPU and memory changes | `test_09_cpu_and_memory_changes` | PASSED | CPU request `100m` → `250m`, memory limit `256Mi` → `512Mi` |
| 10 | Environment variable changes | `test_10_environment_variable_changes` | PASSED | Named env var addition and value changes detected |
| 11 | YAML key reordering | `test_11_yaml_key_reordering` | PASSED | Mapping keys reordered alphabetically produce zero diff |
| 12 | Resource document reordering | `test_12_resource_document_reordering` | PASSED | Multi-document stream reordering produces zero diff |
| 13 | Kubernetes list semantics | `test_13_kubernetes_list_semantics` | PASSED | Container name matching, initContainer order sensitivity |
| 14 | Nested object changes | `test_14_nested_object_changes` | PASSED | Deep mutations inside `securityContext.sysctls` detected |
| 15 | Custom Resources | `test_15_custom_resources` | PASSED | Custom CRD scope resolution and CR field diffing |
| 16 | Duplicate resource identities | `test_16_duplicate_resource_identities` | PASSED | Duplicate `(group, kind, ns, name)` raises actionable error |
| 17 | Invalid YAML | `test_17_invalid_yaml` | PASSED | Syntax errors, unquoted dates, and YAML aliases rejected |
| 18 | Failed Helm rendering | `test_18_failed_helm_rendering` | PASSED | Chart failure reports diagnostic without silent deletion |
| 19 | Failed Kustomize rendering | `test_19_failed_kustomize_rendering` | PASSED | Overlay failure reports diagnostic without silent deletion |
| 20 | Missing values files | `test_20_missing_values_files` | PASSED | Unresolvable values file path raises validation error |
| 21 | Added or removed targets | `test_21_added_or_removed_rendering_targets` | PASSED | Target addition and deletion tracked in inventory diff |
| 22 | Secret redaction | `test_22_secret_redaction` | PASSED | `data`, `stringData`, and custom pointers show `[redacted]` |
| 23 | Oversized reports | `test_23_oversized_reports` | PASSED | Reports exceeding 60k bytes fall back to concise summary |
| 24 | Existing PR comment updates | `test_24_existing_pr_comment_updates` | PASSED | Existing bot comment updated via `PATCH` (no duplicate) |
| 25 | Repeated workflow execution | `test_25_repeated_workflow_execution` | PASSED | Idempotent execution performs 0 writes when comment matches |
| 26 | Untrusted PR input | `test_26_untrusted_pr_input` | PASSED | Path traversal (`../`, `/etc/passwd`, symlinks) blocked |
| 27 | Artifact validation | `test_27_artifact_validation` | PASSED | Tampered context, wrong digest, and zip slip blocked |
| 28 | No semantic changes | `test_28_no_semantic_changes` | PASSED | Identical base and head produce clean no-change message |

### Live GitHub Actions validation results

End-to-end continuous integration and pull request reconciliation have been validated against live GitHub runners:

- **Validation Pull Request**: [charalamposlamprou/kubernetes-manifest-semantic-diff#1](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/pull/1)
- **Consolidated PR Comment**: [Single Bot-Owned Comment #7063159011](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/pull/1#issuecomment-7063159011) (Updated in place across multiple pushes)
- **Engine CI Workflow Run**: [Engine tests #45253335450](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/actions/runs/45253335450) (`main` branch)
- **Review Workflow Run**: [Kubernetes manifest review #45253685510](https://github.com/charalamposlamprou/kubernetes-manifest-semantic-diff/actions/runs/45253685510) (`pull_request_target`)

## Troubleshooting and limits

| Symptom | Check |
|---|---|
| Incomplete comparison | The report identifies the revision and target or inventory. Validate local paths and syntax. For a trusted checkout, run the corresponding pinned renderer privately to inspect its detailed error. |
| Helm dependency missing | Commit the dependency archive or unpacked dependency chart under the parent chart's `charts/` directory. |
| Unexpected namespace or CR identity | Set the target namespace, include its CRD, or declare the external cluster-scoped kind. Manifests using only `generateName` lack a stable identity and fail comparison. |
| Publisher fails | Verify Actions and PR permissions, artifact availability, the workflow filename, trusted checkouts and current PR revisions. Rerun the full workflow to produce a fresh attempt-bound artifact. |
| Discovery includes an example | Declare deployment targets explicitly. Deployment intent cannot be inferred from arbitrary repository layout. |
| Review is abbreviated | A comment is capped below 60,000 UTF-8 bytes. Details are bounded to 512 resource records, 256 field records per resource and a shared text budget. Counts still cover successful targets. Split a large review by application. |
| Input rejected | Keep revisions within 60,000 inventory entries, 128 targets, 8 MiB of rendered output per invocation, 60 seconds per render and bounded YAML depth/node count. Use JSON-compatible YAML with unique string keys and expanded aliases. Visible repository-relative config paths keep untrusted input inside its snapshot. |

These are offline rendered comparisons. Live admission mutation, server-side defaulting and conversion, cluster-specific API schemas, controller reconciliation, runtime drift, and external secret resolution can change the applied result. A periodic Helm value can also agree across two quick renders and vary later. Independent targets can intentionally contain the same resource. Counts are per target. Offline diffing does not establish whether a deployment will succeed.

Future improvements include OpenAPI-driven scope and list strategies, Kubernetes quantity equivalence and optional target-specific review budgets.

## Repository layout

```text
.github/workflows/
  ├── kustomize-diff.yaml  PR semantic diff review workflow (compare + publish)
  └── ci.yaml              Engine tests, linter, coverage, and actionlint CI
ci/                        Renderer container Dockerfile and hashed Python lockfiles
manifest_diff/
  ├── cli.py               Console entry point (supports --native and --artifact)
  ├── config.py            Inventory loader and automatic chart/kustomize discovery
  ├── engine.py            Diff orchestration, inventory comparison, and change budgeting
  ├── input.py             Hardened YAML parsing, depth/size/node complexity bounds
  ├── patch.py             Unified diff hunk generator with schema-aware ordering
  ├── publish.py           Privileged artifact validator and idempotent PR commenter
  ├── render.py            Helm & Kustomize adapters and containerized isolation
  ├── report.py            Markdown presentation, summary tables, and sanitization
  └── semantic.py          Canonical resource identity, CRD scopes, and secret masking
examples/
  ├── backend/             Kustomize base, RBAC components, and dev/staging/prod overlays
  ├── payments/            Helm chart with hook, CRDs, and vendored subchart
  ├── environments/        Environment-specific Helm values overrides
  ├── podinfo/             Standalone microservice used for live PR validation
  └── review.patch         Replayable demonstration patch
tests/
  ├── test_boundaries.py   Security boundaries, CLI, container sandbox, and CR scopes
  ├── test_coverage_matrix.py Dedicated 28-requirement verification suite
  ├── test_integration.py  Integration tests using real pinned Helm and Kustomize binaries
  ├── test_patch.py        Unified diff hunks, credential leakage prevention, and code fences
  ├── test_publish.py      GitHub API provenance checks and comment update idempotency
  └── test_semantic.py     Canonical identity, YAML key/document reordering, and list semantics
.manifest-diff.yaml        Root deployment inventory and custom sensitivePaths policy
pyproject.toml             Package metadata, Ruff linter/formatter config, and pytest settings
```
