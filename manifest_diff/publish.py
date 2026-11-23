"""Privileged entry point. Standard library only. All GitHub addresses come from trusted context."""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

from .report import MARKER, markdown, require, validate

WORKFLOW = ".github/workflows/manifest-diff.yaml"
BOT_ID = 41898282
MAX_ARCHIVE = 1024 * 1024


def context():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repo = os.environ["GITHUB_REPOSITORY"]
    pr = event["pull_request"]
    require(os.environ["GITHUB_EVENT_NAME"] == "pull_request_target")
    require(re.fullmatch(r"[\w.-]+/[\w.-]+", repo) and pr["base"]["repo"]["full_name"] == repo)
    require(pr["number"] == event["number"] and type(pr["number"]) is int and pr["number"] > 0)
    require(all(re.fullmatch(r"[0-9a-f]{40}", pr[k]["sha"]) for k in ("head", "base")))
    return {
        "repo": repo,
        "pr": pr["number"],
        "base": pr["base"]["sha"],
        "head": pr["head"]["sha"],
        "run": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    }


def api(path, method="GET", body=None, binary=False):
    cmd = ["gh", "api", "--method", method, path]
    if body is not None:
        cmd.extend(["--input", "-"])
    proc = subprocess.run(
        cmd,
        input=json.dumps(body).encode() if body is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=90,
        check=True,
    )
    return proc.stdout if binary else json.loads(proc.stdout) if proc.stdout else None


def unpack(raw, digest, ctx):
    require(len(raw) <= MAX_ARCHIVE and digest == "sha256:" + hashlib.sha256(raw).hexdigest())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        require(len(infos) == 1)
        entry = infos[0]
        require(
            entry.orig_filename == "report.json"
            and entry.file_size <= MAX_ARCHIVE
            and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG)
            and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            and not entry.flag_bits & 1
        )
        data = json.loads(archive.read(entry))
    require(
        type(data) is dict
        and set(data) == {"version", "context", "report"}
        and type(data["version"]) is int
        and data["version"] == 1
    )
    require(json.dumps(data["context"], sort_keys=True) == json.dumps(ctx, sort_keys=True))
    validate(data["report"])
    return data["report"]


def artifact(ctx, artifact_id, request):
    require(type(artifact_id) is int and artifact_id > 0)
    prefix = "repos/" + ctx["repo"]
    run = request(f"{prefix}/actions/runs/{ctx['run']}")
    workflow = request(f"{prefix}/actions/workflows/{Path(WORKFLOW).name}")
    require(
        run["id"] == ctx["run"]
        and run["run_attempt"] == ctx["attempt"]
        and run["event"] == "pull_request_target"
        and run["path"] == workflow["path"] == WORKFLOW
        and run["workflow_id"] == workflow["id"]
        and run["repository"]["full_name"] == ctx["repo"]
    )
    meta = request(f"{prefix}/actions/artifacts/{artifact_id}")
    require(
        meta["id"] == artifact_id
        and meta["workflow_run"]["id"] == ctx["run"]
        and meta["workflow_run"]["repository_id"] == run["repository"]["id"]
        and meta["workflow_run"]["head_sha"] == run["head_sha"]
        and not meta["expired"]
        and meta["name"] == f"semantic-diff-{ctx['run']}-{ctx['attempt']}"
        and 0 < meta["size_in_bytes"] <= MAX_ARCHIVE
    )
    return unpack(
        request(f"{prefix}/actions/artifacts/{artifact_id}/zip", binary=True), meta["digest"], ctx
    )


def publish(ctx, artifact_id, request=api):
    report = artifact(ctx, artifact_id, request)
    prefix = "repos/" + ctx["repo"]
    pr = ctx["pr"]
    endpoint = f"{prefix}/issues/{pr}/comments"
    owned = []
    page = 1
    while True:
        comments = request(f"{endpoint}?per_page=100&page={page}")
        owned.extend(
            c
            for c in comments
            if c["user"]["id"] == BOT_ID
            and c["user"]["login"] == "github-actions[bot]"
            and c["body"].startswith(MARKER + "\n")
        )
        if len(comments) < 100:
            break
        page += 1
    current = request(f"{prefix}/pulls/{pr}")
    if current["state"] != "open" or any(current[k]["sha"] != ctx[k] for k in ("base", "head")):
        return "Skipped superseded or closed pull request."
    require(current["number"] == pr and current["base"]["repo"]["full_name"] == ctx["repo"])
    body = markdown(report, ctx["head"])
    if not owned:
        request(endpoint, "POST", {"body": body})
    else:
        first, *duplicates = sorted(owned, key=lambda c: c["id"])
        if first["body"] != body:
            request(f"{prefix}/issues/comments/{first['id']}", "PATCH", {"body": body})
        for duplicate in duplicates:
            request(f"{prefix}/issues/comments/{duplicate['id']}", "DELETE")
    return "Consolidated comment verified or written."


def main():
    try:
        print(publish(context(), int(os.environ["ARTIFACT_ID"])))
    except Exception:
        print(
            "Publication failed. Check artifact provenance, format, API access and workflow permissions.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
