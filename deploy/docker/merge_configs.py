#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper script to deep merge YAML configurations during Docker image build."""

import argparse
import pathlib
import yaml


def merge(a, b):
  """Recursively deep merge two dictionaries/lists."""
  if isinstance(a, dict) and isinstance(b, dict):
    for k, v in b.items():
      a[k] = merge(a[k], v) if k in a else v
    return a
  elif isinstance(a, list) and isinstance(b, list):
    return list(dict.fromkeys(a + b))
  return b


def main():
  parser = argparse.ArgumentParser(
      description="Deep merge YAML configuration files."
  )
  parser.add_argument(
      "base",
      type=pathlib.Path,
      help="Base YAML configuration file to modify in place.",
  )
  parser.add_argument("overlay", type=pathlib.Path, help="Overlay YAML file.")
  args = parser.parse_args()

  s = yaml.safe_load(args.base.read_text()) if args.base.exists() else {}
  p = yaml.safe_load(args.overlay.read_text()) if args.overlay.exists() else {}

  merged = merge(s or {}, p or {})
  args.base.write_text(yaml.safe_dump(merged))


if __name__ == "__main__":
  main()
