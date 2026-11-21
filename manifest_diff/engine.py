"""Join inventories by target name and compare successful renders in memory."""

from itertools import chain

from .config import inventory
from .input import Problem
from .semantic import encoded, fields, flatten, index

STATUSES = ("Added", "Modified", "Removed")


def compare(base, head, renderer):
    errors = []
    inventories = []
    policies = set()
    for side, root in [("base", base), ("head", head)]:
        try:
            targets, policy = inventory(root)
            policies.update(policy)
        except Problem as exc:
            errors.append([side, "inventory", str(exc)])
            targets = {}
        except OSError:
            errors.append(
                [
                    side,
                    "inventory",
                    "Unable to read revision files. Check paths and file permissions.",
                ]
            )
            targets = {}
        inventories.append(targets)
    report = {
        "counts": dict.fromkeys(STATUSES, 0),
        "targets": [],
        "errors": errors,
        "changes": [],
        "truncated": False,
    }
    if errors:
        return report
    patterns = [
        tuple(s.replace("~1", "/").replace("~0", "~") for s in p[1:].split("/")) for p in policies
    ]
    known = set()
    left, right = inventories
    budget = 0
    for name in sorted(left.keys() | right.keys()):
        if name not in left or name not in right:
            report["targets"].append([name, "Added" if name in right else "Removed"])
        indexes = []
        for side, root, targets in [("base", base, left), ("head", head, right)]:
            try:
                if name not in targets:
                    indexes.append({})
                else:
                    target = targets[name]
                    docs = flatten(renderer.render(root, target))
                    if target.type == "helm":
                        repeat = flatten(renderer.render(root, target))
                        if sorted(map(encoded, docs)) != sorted(map(encoded, repeat)):
                            raise Problem(
                                "Helm output varied between renders. Remove random or time-dependent templates."
                            )
                    indexes.append(
                        index(docs, target.namespace, patterns, known, target.cluster_scoped)
                    )
            except Problem as exc:
                errors.append([side, name, f"{targets[name].type}: {exc}"])
        if len(indexes) != 2:
            continue
        a, b = indexes
        target = right.get(name) or left[name]
        for key in sorted(a.keys() | b.keys()):
            group, kind, ns, resource = key
            rows = []
            if key not in a:
                status = "Added"
            elif key not in b:
                status = "Removed"
            else:
                status = "Modified"
                differences = iter(fields(a[key], b[key], group, kind))
                first = next(differences, None)
                if first is None:
                    continue
                for field in chain((first,), differences):
                    if len(rows) >= 256 or budget > 100_000:
                        report["truncated"] = True
                        break
                    rows.append(list(field))
                    budget += sum(len(v) for v in field)
                # Counts stay complete when the field-detail budget is exhausted.
            report["counts"][status] += 1
            if len(report["changes"]) >= 512 or budget > 100_000:
                report["truncated"] = True
                continue
            report["changes"].append(
                {
                    "app": target.app,
                    "env": target.env,
                    "target": name,
                    "resource": [group, kind, ns, resource],
                    "status": status,
                    "fields": rows,
                }
            )
    # Also suppress repeated literals outside designated paths, including JSON-escaped forms.
    literals = sorted(known | {encoded(v)[1:-1] for v in known}, key=len, reverse=True)

    def scrub(item):
        if isinstance(item, str):
            for literal in literals:
                if literal and literal in item:
                    return "[redacted]"
            return item
        if isinstance(item, list):
            return [scrub(x) for x in item]
        return item

    for change in report["changes"]:
        for key in ("app", "env", "target", "resource", "fields"):
            change[key] = scrub(change[key])
    for entry in report["targets"]:
        entry[0] = scrub(entry[0])
    for error in errors:
        error[1] = scrub(error[1])
    if len(encoded(report).encode()) > 200_000:
        report["changes"] = []
        report["truncated"] = True
    return report
