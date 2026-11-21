"""Fixed renderer commands. Native rendering is for trusted local worktrees only."""

import os
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .input import Problem

VERSIONS = {"helm": "v4.1.1", "kustomize": "v5.8.1"}
IMAGE = "manifest-diff-renderers:1"
LIMIT = 8 * 1024 * 1024


def capture(command, env, cwd):
    deadline = time.monotonic() + 60
    output = bytearray()
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(proc.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not ready.select(max(0, deadline - time.monotonic())):
                        break
                    block = os.read(proc.stdout.fileno(), 65536)
                    if not block:
                        if proc.wait(timeout=max(0, deadline - time.monotonic())):
                            raise Problem(
                                "Renderer failed. Check templates, patches and vendored dependencies."
                            )
                        return bytes(output)
                    output.extend(block)
                    if len(output) > LIMIT:
                        raise Problem("Renderer output exceeds 8 MiB.")
            raise Problem("Renderer exceeded the 60-second time limit.")
        finally:
            proc.kill()


def helm_args(target, loc):
    args = [
        "template",
        target.release,
        loc(target.path),
        "--namespace",
        target.namespace,
        "--include-crds",
        "--kube-version",
        target.kube_version,
    ]
    for api in target.apis:
        args.extend(["--api-versions", api])
    for path in target.values:
        args.extend(["--values", loc(path)])
    return args


def kustomize_args(target, loc):
    return ["build", loc(target.path), "--load-restrictor", "LoadRestrictionsRootOnly"]


ADAPTERS = {"helm": helm_args, "kustomize": kustomize_args}


class Renderer:
    def __init__(self, isolated=True):
        self.isolated = isolated
        self.checked = set()

    def render(self, root, target):
        def loc(path):
            return str(Path("/src") / path.relative_to(root)) if self.isolated else str(path)

        tool = target.type
        args = ADAPTERS[tool](target, loc)
        try:
            if self.isolated:
                if (root / ".git").exists():
                    raise Problem(
                        "Isolated rendering requires exported revision trees without .git metadata."
                    )
                name = "manifest-diff-" + uuid.uuid4().hex
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--log-driver=none",
                    "--name",
                    name,
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=32",
                    "--memory=512m",
                    "--cpus=1",
                    "--user=65534:65534",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=32m",
                    "--volume",
                    f"{root}:/src:ro",
                    "--workdir",
                    "/src",
                    IMAGE,
                    "/" + tool,
                    *args,
                ]
                try:
                    return capture(command, os.environ, "/")
                except BaseException:
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        check=False,
                    )
                    raise
            binary = shutil.which(tool)
            if not binary:
                raise Problem("Pinned renderer is missing. Install the documented version.")
            with tempfile.TemporaryDirectory() as home:
                env = {
                    "PATH": "/nonexistent",
                    "HOME": home,
                    "TMPDIR": home,
                    "HELM_NO_PLUGINS": "1",
                    "HELM_PLUGINS": home + "/disabled",
                    "KUBECONFIG": "/dev/null",
                    "TZ": "UTC",
                }
                if tool not in self.checked:
                    version_args = ["version", "--short"] if tool == "helm" else ["version"]
                    version = capture([binary, *version_args], env, home).strip().split(b"+")[0]
                    if version != VERSIONS[tool].encode():
                        raise Problem("Renderer version differs from the documented pin.")
                    self.checked.add(tool)
                return capture([binary, *args], env, home)
        except (OSError, subprocess.SubprocessError):
            raise Problem(
                "Renderer unavailable or timed out. Check the pinned tool installation."
            ) from None
