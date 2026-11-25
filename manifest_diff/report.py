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
    masked = [code(field) for field, old, new in change["fields"] if old == new == "[redacted]"]
    if masked:
        blocks.append("Changed protected fields · " + ", ".join(masked))
    return "\n\n".join(blocks)


def sensitive(change):
    group, kind, _, _ = change["resource"]
    return (
        (
            group == "rbac.authorization.k8s.io"
            and kind in {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
        )
        or (group == "networking.k8s.io" and kind == "NetworkPolicy")
        or kind == "Secret"
        or any("securityContext" in row[0] for row in change["fields"])
    )


CONTAINER_FIELD = re.compile(
    r"spec\.(?:jobTemplate\.spec\.)?(?:template\.spec\.)?"
    r"(?:containers|initContainers)\[[^\]]+\]\.(.+)"
)


def field_highlight(path, before, after):
    if path == "spec.replicas":
        return f"Replicas {code(before)} → {code(after)}"
    if path == "spec.minReadySeconds":
        return "Minimum ready time"
    match = CONTAINER_FIELD.fullmatch(path)
    if match:
        field = match[1]
        if field.startswith(("resources.requests.", "resources.limits.")):
            return "Resource requests/limits"
        if field.startswith("readinessProbe."):
            return "Readiness probe"
        if re.fullmatch(r"envFrom\[\d+\]\.configMapRef\.name", field):
            return "ConfigMap reference"
        if field == "image":
            return "Container image"
    return code(path)


def review_highlights(changes):
    labels = {}
    for change in changes:
        kind = change["resource"][1]
        if change["status"] != "Modified":
            labels[f"{code(kind)} {change['status'].lower()}"] = None
        for path, before, after in change["fields"]:
            labels[field_highlight(path, before, after)] = None
    highlights = list(labels)
    shown = highlights[:6]
    if len(highlights) > len(shown):
        shown.append(f"+{len(highlights) - len(shown)} more")
    if any(sensitive(change) for change in changes):
        shown.insert(0, "**security-sensitive**")
    return " · ".join(shown) or "See resource details"


def resource_detail(change):
    group, kind, ns, name = change["resource"]
    ident = " / ".join(
        code(s) for s in (kind + ("." + group if group else ""), ns or "(cluster)", name)
    )
    title = f"{change['status']} · {ident}"
    if sensitive(change):
        title += " · security-sensitive"
    sections = []
    if change["fields"]:
        sections.append(
            "| Field | Before | After |\n|---|---|---|\n"
            + "\n".join(
                "| " + " | ".join(code(v) if v != "∅" else "Absent" for v in row) + " |"
                for row in change["fields"]
            )
        )
    patch = resource_patch(change)
    if patch:
        sections.append(patch)
    if not sections:
        return "- " + title
    return f"<details><summary>{title}</summary>\n\n" + "\n\n".join(sections) + "\n\n</details>"


def markdown(report, head=None):
    validate(report)
    counts = report["counts"]
    total = sum(counts.values())
    headline = [MARKER, "### Kubernetes Manifest Semantic Diff"]
    if report["errors"]:
        headline.append("**Comparison incomplete.** Failed targets are excluded from the counts.")
    if total or report["errors"]:
        statuses = " · ".join(f"{counts[s]} {s.lower()}" for s in STATUSES)
        headline.append(f"**{total} resource change{'s' if total != 1 else ''}** · {statuses}")
    else:
        headline.append("No semantic changes found in rendered resources.")
    if head:
        headline.append("Reviewed head " + code(head))
    summary = "\n\n".join(headline)
    notices = []
    if report["errors"]:
        notices.append(
            "\n".join(
                f"- {code(side)} / {code(name)} — {code(message)}"
                for side, name, message in report["errors"]
            )
        )
    if report["targets"]:
        notices.append(
            "**Target inventory**\n\n"
            + "\n".join(f"- {status} {code(name)}" for name, status in report["targets"])
        )
    with_notices = "\n\n".join([summary, *notices])
    if len(with_notices.encode()) <= 45_000:
        summary = with_notices
    else:
        summary += f"\n\n{len(report['errors'])} errors and {len(report['targets'])} inventory changes. See the redacted artifact."
    limit_note = "\n\nField details exceed the review limit. Narrow the target set for a detailed comparison."
    if report["truncated"]:
        return summary + limit_note
    if not report["changes"]:
        return summary

    def grouping(change):
        return tuple(change[k] for k in ("app", "env", "target"))

    table = [
        "| Application | Environment | Target | Resources | Changes to inspect |",
        "|---|---|---|---:|---|",
    ]
    details = []
    for (app, env, target), items in groupby(sorted(report["changes"], key=grouping), grouping):
        changes = list(items)
        table.append(
            f"| {code(app)} | {code(env)} | {code(target)} | {len(changes)} | {review_highlights(changes)} |"
        )
        details.append(f"#### {code(app)} / {code(env)} · {code(target)}")
        details.extend(resource_detail(change) for change in changes)
    overview = summary + "\n\n" + "\n".join(table)
    full = "\n\n".join([overview, *details])
    if len(full.encode()) > 60_000:
        return (
            overview if len((overview + limit_note).encode()) <= 60_000 else summary
        ) + limit_note
    return full
