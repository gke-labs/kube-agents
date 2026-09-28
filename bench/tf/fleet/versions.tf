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

terraform {
  required_version = ">= 1.5.0"

  # Partial config on purpose: the reconcile model is re-apply from any
  # checkout, which only works against shared remote state -- a fresh local
  # state would plan full creates and 409 against the live fleet. The bucket
  # and prefix arrive at init time (see README.md); local validation uses
  # `tofu init -backend=false`.
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      # One major each, because the scheduled reconcile re-resolves providers
      # on every run (the lock file is not committed): the next major must be
      # adopted by a person, not applied unattended across the pool. These are
      # the majors the fleet is applied with today (8.4.0 and 3.2.1 resolved
      # on 2026-09-28).
      version = "~> 8.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
  }
}
