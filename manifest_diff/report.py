"""Strict report protocol and Markdown presentation. Uses only the standard library."""

import re
from collections import Counter
from difflib import unified_diff
from itertools import groupby
from urllib.parse import quote

MARKER = "<!-- kubernetes-manifest-semantic-diff:v1 -->"
STATUSES = ("Added", "Modified", "Removed")
HUNK_HEADER = re.compile(r"@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@")


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
            and set(change) - {"hunks"} == {"target", "app", "env", "resource", "status", "fields"}
        )
        words([change[k] for k in ("target", "app", "env")], 3)
        words(change["resource"], 4)
        require(change["status"] in STATUSES)
        actual[change["status"]] += 1
        for row in array(change["fields"], 256):
            words(row, 3)
        if "hunks" in change:
            hunks = array(change["hunks"], 1024)
            require(
                hunks
                and all(
                    type(line) is str and line and "\n" not in line and "\r" not in line
                    for line in hunks
                )
            )
            require(sum(map(len, hunks)) <= 60_000 and HUNK_HEADER.fullmatch(hunks[0]))
            require(all(line[0] in " +-" or HUNK_HEADER.fullmatch(line) for line in hunks))
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


def literal(value):
    # Keep untrusted values on one visible line, including in legacy field-only reports.
    out = []
    for c in value:
        if not c.isprintable():
            out.append(ascii(c)[1:-1])
        elif c in "<>@":
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return "".join(out)


def resource_patch(change):
    group, kind, ns, name = change["resource"]
    parts = [change["target"], ns or "_cluster", kind + ("." + group if group else ""), name]
    path = "rendered/" + "/".join(quote(p, safe="") for p in parts) + ".yaml"
    hunks = change.get("hunks", [])
    if not hunks:
        # Earlier artifacts and very large resources retain the exact semantic field changes.
        path += ".fields"
        before, after = (
            [
                f"{literal(row[0])}: {literal(row[side])}"
                for row in change["fields"]
                if row[side] != "∅"
            ]
            for side in (1, 2)
        )
        hunks = list(unified_diff(before, after, lineterm=""))[2:]
    blocks = []
    if hunks:
        old = "/dev/null" if change["status"] == "Added" else "a/" + path
        new = "/dev/null" if change["status"] == "Removed" else "b/" + path
        lines = [f"--- {old}", f"+++ {new}"]
        lines.extend(line if HUNK_HEADER.fullmatch(line) else literal(line) for line in hunks)
        body = "\n".join(lines)
        longest = max((len(s) for s in re.findall(r"`{3,}", body)), default=2)
        fence = "`" * (longest + 1)
        blocks.append(f"{fence}diff\n{body}\n{fence}")
    if change.get("hunks"):
        paths = [code(field) for field, old, new in change["fields"] if old != new]
        if paths:
            blocks.append("Field paths · " + ", ".join(paths))
    masked = [code(field) for field, old, new in change["fields"] if old == new == "[redacted]"]
    if masked:
        blocks.append("Changed protected fields · " + ", ".join(masked))
    return "\n\n".join(blocks)


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
            patch = resource_patch(change)
            if patch:
                details.append(f"<details open><summary>{title}</summary>\n\n{patch}\n\n</details>")
            else:
                details.append("- " + title)
    full = "\n\n".join([summary, *details])
    if len(full.encode()) > 60_000 or report["truncated"]:
        return (
            summary
            + "\n\nField details exceed the review limit. Narrow the target set for a detailed comparison."
        )
    return full
