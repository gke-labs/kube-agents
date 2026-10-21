#!/bin/sh
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

# The report-store step of bench/tf/prebuilt/audit-chat-report, run in the shell
# sandbox. Two commands, each taking the stream's store directory:
#
#   move <store>     rename every latest.json under it to latest.json.bench-moved
#   restore <store>  put back each renamed copy whose latest.json the run did not
#                    replace, and delete the rest
#
# A store that does not exist is left alone by both.
set -eu

suffix=.bench-moved
command=$1
store=$2
[ -d "$store" ] || exit 0

case "$command" in
  move)
    find "$store" -name latest.json | while read -r f; do mv "$f" "$f$suffix"; done
    ;;
  restore)
    find "$store" -name "latest.json$suffix" | while read -r f; do
      original=${f%"$suffix"}
      if [ -e "$original" ]; then rm -f "$f"; else mv "$f" "$original"; fi
    done
    ;;
  *)
    echo "unknown command $command" >&2
    exit 2
    ;;
esac
