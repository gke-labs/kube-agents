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

# The report-store step of bench/tf/prebuilt/upgrade-retro-recorded-weeks, run in
# the shell sandbox. Three commands, each taking the store's reports directory
# (an absolute path at least two levels deep) and the seeded report's stamp:
#
#   write <dir> <stamp> <md|json>   write stdin to <dir>/<stamp>.<ext>, owned as
#                                   the store's files are
#   seed <dir> <stamp>              record where the latest link points and
#                                   point it at <stamp>.md
#   restore <dir> <stamp>           remove the seeded pair and put the link back
#                                   where it pointed (or remove it, if it did
#                                   not exist)
set -eu

link=upgrade-retro-report.md
marker_prefix=.bench-recorded-weeks.
owner=agent
command=${1:?command}
dir=${2:?reports directory}
stamp=${3:?stamp}

# The removals below are confined to the store: an absolute directory at least
# two levels deep and a stamp of letters and digits, as the collector's are.
case "$dir" in
  /*/*) ;;
  *) echo "refusing a reports directory that is not an absolute path two levels deep: $dir" >&2; exit 2 ;;
esac
case "$stamp" in
  *[!A-Za-z0-9]* | "") echo "refusing a stamp outside [A-Za-z0-9]: $stamp" >&2; exit 2 ;;
esac
seeded_md="$dir/$stamp.md"
seeded_json="$dir/$stamp.json"
marker="$dir/$marker_prefix$stamp"

case "$command" in
  write)
    ext=${4:?md or json}
    case "$ext" in md | json) ;; *) echo "unknown extension $ext" >&2; exit 2 ;; esac
    # The store belongs to the agent user; the sandbox exec runs as root, so a
    # store this stack has to create is created owned as the collector creates it.
    if [ ! -d "$dir" ]; then
      install -d -m 0755 -o "$owner" -g "$owner" "$(dirname "$dir")" "$dir" 2>/dev/null || mkdir -p "$dir"
    fi
    cat > "$dir/$stamp.$ext"
    chmod 0644 "$dir/$stamp.$ext"
    chown "$owner:$owner" "$dir/$stamp.$ext" 2>/dev/null || true
    ;;
  seed)
    previous=$(readlink "$dir/$link" 2>/dev/null || true)
    printf '%s\n' "$previous" > "$marker"
    ln -sfn "$stamp.md" "$dir/$link"
    chown -h "$owner:$owner" "$dir/$link" 2>/dev/null || true
    ;;
  restore)
    # The pair goes whatever the marker says (an apply that failed between
    # `write` and `seed` left no marker); the link moves back only when it
    # still points at the seeded report, so a full run the collector made in
    # between keeps the newer report it linked.
    for seeded in "$seeded_md" "$seeded_json"; do
      [ -f "$seeded" ] && unlink "$seeded"
    done
    previous=""
    [ -e "$marker" ] && previous=$(cat "$marker")
    if [ "$(readlink "$dir/$link" 2>/dev/null || true)" = "$stamp.md" ]; then
      if [ -n "$previous" ] && [ -e "$dir/$previous" ]; then
        ln -sfn "$previous" "$dir/$link"
        chown -h "$owner:$owner" "$dir/$link" 2>/dev/null || true
      else
        unlink "$dir/$link"
      fi
    fi
    [ -e "$marker" ] && unlink "$marker"
    exit 0
    ;;
  *)
    echo "unknown command $command" >&2
    exit 2
    ;;
esac
