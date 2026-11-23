import hashlib
import io
import json
import zipfile
from copy import deepcopy

import pytest

from manifest_diff.publish import BOT_ID, WORKFLOW, artifact, publish, unpack
from manifest_diff.report import MARKER, markdown, validate

CTX = {"repo": "example/demo", "pr": 7, "base": "a" * 40, "head": "b" * 40, "run": 12, "attempt": 1}
REPORT = {
    "counts": {"Added": 0, "Modified": 0, "Removed": 0},
    "changes": [],
    "errors": [],
    "targets": [],
    "truncated": False,
}


def pack(data, path="report.json"):
    sink = io.BytesIO()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(path, json.dumps(data))
    raw = sink.getvalue()
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


class Service:
    def __init__(self):
        self.pr = {
            "number": 7,
            "state": "open",
            "head": {"sha": CTX["head"]},
            "base": {"sha": CTX["base"], "repo": {"full_name": CTX["repo"]}},
        }
        self.raw, digest = pack({"version": 1, "context": CTX, "report": REPORT})
        self.run = {
            "id": 12,
            "run_attempt": 1,
            "event": "pull_request_target",
            "path": WORKFLOW,
            "workflow_id": 3,
            "head_sha": CTX["base"],
            "repository": {"id": 4, "full_name": CTX["repo"]},
        }
        self.meta = {
            "id": 9,
            "name": "semantic-diff-12-1",
            "expired": False,
            "size_in_bytes": len(self.raw),
            "digest": digest,
            "workflow_run": {"id": 12, "repository_id": 4, "head_sha": CTX["base"]},
        }
        self.comments = [{"id": 123, "body": MARKER + "\n", "user": {"id": 1, "login": "outsider"}}]
        self.writes = []

    def __call__(self, path, method="GET", body=None, binary=False):
        if method != "GET":
            self.writes.append(method)
            if method == "POST":
                self.comments.append(
                    {
                        "id": 456,
                        "body": body["body"],
                        "user": {"id": BOT_ID, "login": "github-actions[bot]"},
                    }
                )
            elif method == "PATCH":
                assert path.endswith("/456")
                self.comments[1]["body"] = body["body"]
            else:
                self.comments = [c for c in self.comments if not path.endswith("/" + str(c["id"]))]
            return
        if "/runs/" in path:
            return self.run
        if "/workflows/" in path:
            assert path.endswith("/workflows/manifest-diff.yaml")
            return {"id": 3, "path": WORKFLOW}
        if path.endswith("/zip"):
            assert binary
            return self.raw
        if "/artifacts/" in path:
            return self.meta
        if "/pulls/" in path:
            return self.pr
        return self.comments


def test_idempotent_owned_comment_and_staleness():
    service = Service()
    publish(CTX, 9, service)
    publish(CTX, 9, service)
    assert service.writes == ["POST"]
    service.comments[1]["body"] = MARKER + "\nold"
    service.comments.append({**service.comments[1], "id": 999})
    publish(CTX, 9, service)
    assert service.writes == ["POST", "PATCH", "DELETE"] and len(service.comments) == 2
    service.pr["head"]["sha"] = "c" * 40
    assert "superseded" in publish(CTX, 9, service)
    assert service.comments[0]["body"] == MARKER + "\n"


@pytest.mark.parametrize(
    ("obj", "key", "value"),
    [
        ("meta", "name", "semantic-diff-12-2"),
        ("meta", "expired", True),
        ("meta", "id", 10),
        ("meta", "digest", "sha256:wrong"),
        ("meta", "workflow_run", {"id": 999}),
        ("run", "event", "pull_request"),
        ("run", "path", "another.yml"),
        ("run", "workflow_id", 999),
        ("run", "run_attempt", 2),
        ("run", "repository", {"id": 4, "full_name": "other/repo"}),
    ],
)
def test_reject_provenance(obj, key, value):
    service = Service()
    getattr(service, obj)[key] = value
    with pytest.raises(ValueError):
        artifact(CTX, 9, service)
    assert not service.writes


@pytest.mark.parametrize("path", ["../report.json", "dir/report.json", "/report.json"])
def test_reject_archive_names(path):
    raw, digest = pack({}, path)
    with pytest.raises(ValueError):
        unpack(raw, digest, CTX)


@pytest.mark.parametrize("key", list(CTX))
def test_envelope_binding(key):
    raw, digest = pack({"version": 1, "context": {**CTX, key: "wrong"}, "report": REPORT})
    with pytest.raises(ValueError):
        unpack(raw, digest, CTX)


def test_malformed_report_and_zip_bomb():
    with pytest.raises(ValueError):
        validate({**REPORT, "changes": ["shell command"]})
    raw, digest = pack("x" * 1_100_000)
    with pytest.raises(ValueError):
        unpack(raw, digest, CTX)


def test_markdown_and_size():
    report = deepcopy(REPORT)
    report["counts"]["Modified"] = 1
    report["changes"] = [
        {
            "target": "prod",
            "app": "api",
            "env": "prod",
            "resource": ["", "Secret", "prod", "sample"],
            "status": "Modified",
            "fields": [["data.x", "<img src=x>@everyone |", "[redacted]"]],
        }
    ]
    result = markdown(report)
    assert (
        "security-sensitive" in result
        and "<details open>" in result
        and "<img" not in result
        and "@everyone" not in result
    )
    report["changes"][0]["fields"][0][1] = "x" * 70_000
    result = markdown(report)
    assert len(result.encode()) < 60_000 and "exceed" in result


def test_context(monkeypatch, tmp_path):
    from manifest_diff.publish import context

    data = Service().pr
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": data, "number": 7}))
    for k, v in {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": CTX["repo"],
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_RUN_ID": "12",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(k, v)
    assert context() == CTX
