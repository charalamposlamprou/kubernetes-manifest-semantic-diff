"""Independent target inventories for each revision."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .input import Problem, label, local, one, strings

MARKERS = {"kustomization.yaml", "kustomization.yml", "Kustomization"}


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    path: Path
    app: str
    env: str
    namespace: str
    release: str
    values: tuple
    kube_version: str
    apis: tuple
    cluster_scoped: tuple


def inventory_files(root):
    found = []
    count = 0
    for base, dirs, files in os.walk(root):
        # Hidden files may still be read by a renderer. Reject links even there.
        if Path(base) == root:
            dirs[:] = [
                d
                for d in dirs
                if d not in {".git", ".venv", ".tools", ".pytest_cache", ".ruff_cache"}
            ]
        count += len(dirs) + len(files)
        if count > 60_000:
            raise Problem("Revision exceeds file inventory limit.")
        if any((Path(base) / item).is_symlink() for item in dirs + files):
            raise Problem("Symbolic links in manifest inputs are unsupported.")
        if any(p.startswith(".") for p in Path(base).relative_to(root).parts):
            continue
        markers = MARKERS.intersection(files)
        if len(markers) > 1 and not (root / ".manifest-diff.yaml").exists():
            raise Problem("Several Kustomization filenames share a directory. Keep one.")
        found.extend(Path(base) / name for name in markers | ({"Chart.yaml"} & set(files)))
    return found


def discover(root, files):
    charts = {p.parent for p in files if p.name == "Chart.yaml"}

    def vendored(path):
        return any((parent / "charts") in (path, *path.parents) for parent in charts)

    charts = {p for p in charts if not vendored(p)}
    ks = {p.parent: p for p in files if p.name in MARKERS and not vendored(p.parent)}
    referenced = set()
    edges = {}
    for path, marker in ks.items():
        data = one(marker)
        deps = set()
        for field in ("resources", "bases", "components"):
            for ref in strings(data.get(field, [])):
                dest = (path / ref).resolve()
                if not dest.is_relative_to(root) or "://" in ref:
                    raise Problem("Discovery requires local resources. Vendor remote dependencies.")
                if dest in ks:
                    deps.add(dest)
        edges[path] = deps
        referenced.update(deps)
        if data.get("kind") == "Component":
            referenced.add(path)
    done = set()

    def visit(path, active):
        if path in active or len(active) > 80:
            raise Problem("Cyclic or excessively deep Kustomize references.")
        if path not in done:
            for child in edges[path]:
                visit(child, active | {path})
            done.add(path)

    for path in ks:
        visit(path, set())
    roots = [(p, "helm") for p in charts if one(p / "Chart.yaml").get("type") != "library"]
    roots += [(p, "kustomize") for p in ks.keys() - referenced]
    for path, _ in roots:
        if (
            any(other in (path, *path.parents) for other, _ in roots if other != path)
            or sum(p == path for p, _ in roots) > 1
        ):
            raise Problem(
                "Overlapping discovery roots. Declare deployable targets in .manifest-diff.yaml."
            )
    return [
        {
            "name": "-".join(p.relative_to(root).parts) or "root",
            "type": kind,
            "chart" if kind == "helm" else "path": str(p.relative_to(root)),
        }
        for p, kind in sorted(roots)
    ]


def inventory(root):
    markers = inventory_files(root)
    cfg = root / ".manifest-diff.yaml"
    policy = ()
    if cfg.exists():
        data = one(cfg)
        if (
            type(data.get("version")) is not int
            or data["version"] != 1
            or set(data) - {"version", "targets", "sensitivePaths"}
        ):
            raise Problem("Expected manifest-diff configuration version 1 and supported keys.")
        raw = data.get("targets")
        policy = strings(data.get("sensitivePaths", []))
        if any(not re.fullmatch(r"(/([^~/]|~[01])+)+", p) or len(p) > 512 for p in policy):
            raise Problem(
                "Sensitive paths must be absolute JSON pointers with optional wildcard segments."
            )
    else:
        raw = discover(root, markers)
    if type(raw) is not list or len(raw) > 128:
        raise Problem("Expected at most 128 rendering targets.")
    targets = {}
    for item in raw:
        if not isinstance(item, dict):
            raise Problem("Expected target mapping.")
        kind = item.get("type")
        keys = {"name", "type", "application", "environment", "namespace", "clusterScoped"}
        if kind == "helm":
            keys.update({"chart", "release", "values", "kubeVersion", "apiVersions"})
        elif kind == "kustomize":
            keys.add("path")
        else:
            raise Problem("Target type must be helm or kustomize.")
        if set(item) - keys:
            raise Problem("Unsupported target option. Check configuration keys.")
        name = label(item.get("name"))
        path = local(root, item.get("chart" if kind == "helm" else "path"), directory=True)
        values = tuple(local(root, v) for v in strings(item.get("values", [])))
        apis = strings(item.get("apiVersions", []))
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./-]{0,160}", api) for api in apis):
            raise Problem("Invalid Helm API capability.")
        cluster_scoped = strings(item.get("clusterScoped", []))
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]*/[A-Za-z][A-Za-z0-9]*", key)
            for key in cluster_scoped
        ):
            raise Problem("clusterScoped entries require group/Kind.")
        if name in targets:
            raise Problem("Duplicate target names. Set unique explicit names.")
        targets[name] = Target(
            name,
            kind,
            path,
            label(item.get("application", name)),
            label(item.get("environment", "default")),
            label(item.get("namespace", "default")),
            label(item.get("release", name)),
            values,
            label(item.get("kubeVersion", "1.32.0")),
            apis,
            tuple(tuple(key.split("/")) for key in cluster_scoped),
        )
    return targets, policy
