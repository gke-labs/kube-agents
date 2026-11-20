# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""What bench/tests/conftest.py keeps from the developer's shell away from each test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import KUBECTL_FALLBACK_ENV_VARS


@pytest.fixture(scope="module")
def a_shell_with_a_cluster():
    """The shell of a developer or of a pod: a kubeconfig, and the variables
    that `kubectl` reads when it has none. The module scope puts them in place
    before the conftest's fixture runs for a test, as a real shell does."""
    patch = pytest.MonkeyPatch()
    patch.setenv("KUBECONFIG", "/made-up/kubeconfig")
    for name in KUBECTL_FALLBACK_ENV_VARS:
        patch.setenv(name, "made-up")
    yield
    patch.undo()


def test_a_test_gets_no_cluster_from_the_shell(a_shell_with_a_cluster):
    """A `kubectl` that a test starts must not find the caller's cluster: not
    through the caller's kubeconfig, and not through the variables that
    `kubectl` reads when it has none."""
    assert os.environ["KUBECONFIG"] == os.devnull
    for name in KUBECTL_FALLBACK_ENV_VARS:
        assert name not in os.environ


def test_a_test_that_starts_kubectl_gets_an_error(tmp_path):
    """A bench test that starts the `kubectl` on PATH makes the run red, with
    the command in the report. The test file runs in a pytest of its own, with
    this conftest and with that pytest's own recording `kubectl`."""
    test_file = tmp_path / "test_starts_kubectl.py"
    test_file.write_text(
        "import subprocess\n\n\n"
        "def test_starts_kubectl():\n"
        '    subprocess.run(["kubectl", "get", "pods", "-n", "line one\\nline two"], check=False)\n'
    )
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-p", "conftest", "-p", "no:cacheprovider", "-q",
         f"--rootdir={tmp_path}", f"--basetemp={tmp_path / 'tmp'}"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
    )  # fmt: skip
    assert done.returncode == 1, done.stdout + done.stderr
    assert "1 passed, 1 error" in done.stdout, done.stdout
    assert "started the kubectl on PATH" in done.stdout
    assert "kubectl get pods -n line one line two" in done.stdout
