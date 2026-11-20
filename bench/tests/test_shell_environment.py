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

from conftest import KUBECTL_FALLBACK_ENV_VARS


def test_a_test_gets_no_cluster_from_the_shell():
    """A `kubectl` that a test starts must not find the caller's cluster: not
    through the caller's kubeconfig, and not through the variables that
    `kubectl` reads when it has none."""
    assert os.environ["KUBECONFIG"] == os.devnull
    for name in KUBECTL_FALLBACK_ENV_VARS:
        assert name not in os.environ
