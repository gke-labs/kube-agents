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
# sandbox. Two commands, each taking the stream's store directory and the path
# of the digest record its last scheduled run left:
#
#   move <store> <digest>     rename every latest.json under the store to
#                             latest.json.bench-moved, and the digest record
#                             likewise
#   restore <store> <digest>  put back each renamed copy the run did not replace,
#                             and delete the rest
#
# The digest record goes with the report because `precheck` reads it alone to
# call a fleet unchanged, and then closes a scheduled run from the newest stored
# document, which the run ring still holds when latest.json is gone.
#
# A store or a record that does not exist is left alone by both.
set -eu

suffix=.bench-moved
command=$1
store=$2
digest=$3

case "$command" in
  move)
    if [ -e "$digest" ]; then mv "$digest" "$digest$suffix"; fi
    [ -d "$store" ] || exit 0
    find "$store" -name latest.json | while read -r f; do mv "$f" "$f$suffix"; done
    ;;
  restore)
    if [ -e "$digest$suffix" ]; then
      if [ -e "$digest" ]; then rm -f "$digest$suffix"; else mv "$digest$suffix" "$digest"; fi
    fi
    [ -d "$store" ] || exit 0
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
