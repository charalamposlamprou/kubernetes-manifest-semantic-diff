import shutil
from pathlib import Path

import pytest

from manifest_diff.engine import compare
from manifest_diff.render import Renderer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pair(tmp_path):
    paths = []
    for side in ("base", "head"):
        root = tmp_path / side
        shutil.copytree(ROOT / "examples", root / "examples")
        shutil.copyfile(ROOT / ".manifest-diff.yaml", root / ".manifest-diff.yaml")
        paths.append(root)
    return paths


@pytest.fixture
def run():
    renderer = Renderer(isolated=False)
    return lambda pair: compare(*pair, renderer)
