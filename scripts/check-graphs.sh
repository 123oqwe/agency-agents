#!/usr/bin/env bash
#
# check-graphs.sh — validate the team graphs in strategy/graphs/ and run the
# engine's unit tests.
#
# Team graphs compose agents into automated teams (see strategy/graphs/README.md).
# This fails the build when:
#   1. a graph references an agent slug that doesn't match an agent .md file
#      (e.g. after an agent was renamed or removed)
#   2. a graph references an unknown sub-team, or sub-teams recurse
#   3. a loop is unbounded, `after` edges form a cycle, or a template/condition
#      reads a node that isn't upstream
#   4. the engine's tests (scripts/tests/) fail
#
# Python 3 standard library only. Mirrors scripts/check-runbooks.sh.
#
# Usage: ./scripts/check-graphs.sh

set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: python3 is required for the graphs check." >&2
  exit 2
}

python3 scripts/agency_graph.py validate
python3 -m unittest discover -s scripts/tests
