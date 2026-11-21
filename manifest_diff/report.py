"""Strict report protocol and Markdown presentation. Uses only the standard library."""

from collections import Counter
from itertools import groupby

MARKER = "<!-- kubernetes-manifest-semantic-diff:v1 -->"
STATUSES = ("Added", "Modified", "Removed")


def require(condition):
    if not condition:
        raise ValueError("Invalid report or workflow provenance.")


def array(value, n):
    require(type(value) is list and len(value) <= n)
    return value


def words(value, n):
    require(len(array(value, n)) == n and all(type(x) is str and len(x) <= 150_000 for x in value))


def validate(report):
    require(
        type(report) is dict
        and set(report) == {"counts", "targets", "changes", "errors", "truncated"}
    )
    counts = report["counts"]
    require(
        type(counts) is dict
        and set(counts) == set(STATUSES)
        and all(type(n) is int and 0 <= n <= 10_000_000 for n in counts.values())
    )
    require(type(report["truncated"]) is bool)
    actual = Counter()
    for change in array(report["changes"], 512):
        require(
            type(change) is dict
            and set(change) == {"target", "app", "env", "resource", "status", "fields"}
        )
        words([change[k] for k in ("target", "app", "env")], 3)
        words(change["resource"], 4)
        require(change["status"] in STATUSES)
        actual[change["status"]] += 1
        for row in array(change["fields"], 256):
            words(row, 3)
    require(
        all(
            actual[k] <= counts[k] if report["truncated"] else actual[k] == counts[k]
            for k in STATUSES
        )
    )
    for entry in array(report["targets"], 256):
        words(entry, 2)
        require(entry[1] in ("Added", "Removed"))
    for entry in array(report["errors"], 256):
        words(entry, 3)
        require(entry[0] in ("base", "head"))


def code(value):
    # Encode Markdown, HTML, pipes, mentions, bidi controls and line breaks.
    content = "".join(
        c if c.isascii() and (c.isalnum() or c in " ._/-") else f"&#{ord(c)};" for c in value
    )
    return "<code>" + content + "</code>"


def markdown(report, head=None):
    validate(report)
    parts = [MARKER, "### Kubernetes Manifest Semantic Diff"]
    if head:
        parts.append("Reviewed head " + code(head))
    parts.extend(
        [
            "**Summary**",
            "| Change | Count |\n|---|---:|\n"
            + "\n".join(f"| {s} | {report['counts'][s]} |" for s in STATUSES),
        ]
    )
    headline = "\n\n".join(parts)
    if report["errors"]:
        parts.append("**Comparison incomplete.** Failed targets are excluded from the counts.")
        parts.extend(
            f"- {code(side)} / {code(name)} — {code(message)}"
            for side, name, message in report["errors"]
        )
    if report["targets"]:
        parts.append(
            "**Target inventory**\n\n"
            + "\n".join(f"- {status} {code(name)}" for name, status in report["targets"])
        )
    total = sum(report["counts"].values())
    if total:
        parts.append(f"**Result** · {total} resource change{'s' if total != 1 else ''}.")
    elif not report["errors"]:
        parts.append("No semantic changes found in rendered resources.")
    summary = "\n\n".join(parts)
    if len(summary.encode()) > 45_000:
        summary = (
            headline
            + f"\n\n{len(report['errors'])} errors and {len(report['targets'])} inventory changes. See the redacted artifact."
        )
    details = []

    def grouping(change):
        return tuple(change[k] for k in ("app", "env", "target"))

    for (app, env, target), items in groupby(sorted(report["changes"], key=grouping), grouping):
        details.append(f"#### {code(app)} / {code(env)} · {code(target)}")
        for change in items:
            group, kind, ns, name = change["resource"]
            ident = " / ".join(
                code(s) for s in (kind + ("." + group if group else ""), ns or "(cluster)", name)
            )
            title = f"{change['status']} · {ident}"
            rows = change["fields"]
            security = group in ("rbac.authorization.k8s.io", "networking.k8s.io") and kind in (
                "Role",
                "ClusterRole",
                "RoleBinding",
                "ClusterRoleBinding",
                "NetworkPolicy",
            )
            security = (
                security or kind == "Secret" or any("securityContext" in row[0] for row in rows)
            )
            if security:
                title += " · security-sensitive"
            if rows:
                table = "| Field | Before | After |\n|---|---|---|\n" + "\n".join(
                    "| " + " | ".join(code(s) for s in row) + " |" for row in rows
                )
                details.append(f"<details><summary>{title}</summary>\n\n{table}\n\n</details>")
            else:
                details.append("- " + title)
    full = "\n\n".join([summary, *details])
    if len(full.encode()) > 60_000 or report["truncated"]:
        return (
            summary
            + "\n\nField details exceed the review limit. Narrow the target set for a detailed comparison."
        )
    return full
