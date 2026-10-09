#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
python3 "$ROOT/scripts/verify_source.py"
python3 "$ROOT/tests/preflight_test.py"
python3 "$ROOT/tests/build_plugin_test.py"
python3 "$ROOT/tests/validate_game_test.py"
python3 "$ROOT/tests/python_client_test.py"
python3 "$ROOT/tests/demo_tools_test.py"
python3 "$ROOT/tests/owner_dispatch_test.py"
python3 "$ROOT/tests/run_demo_test.py"
python3 "$ROOT/tests/package_demo_test.py"
python3 "$ROOT/tests/native_evidence_test.py"
python3 "$ROOT/tests/native_showcase_source_test.py"
c++ -std=c++17 -Wall -Wextra -Werror -pedantic -pthread \
  -I"$ROOT/Source/AuroraViewRuntime/Private" -I"$ROOT/Source/AuroraViewEditor/Private" "$ROOT/tests/mailbox_test.cpp" -o "$BUILD/mailbox_test"
"$BUILD/mailbox_test"
c++ -std=c++17 -Wall -Wextra -Werror -pedantic \
  -I"$ROOT/Source/AuroraViewRuntime/Private" -I"$ROOT/Source/AuroraViewEditor/Private" "$ROOT/tests/native_interaction_guards_test.cpp" -o "$BUILD/native_interaction_guards_test"
"$BUILD/native_interaction_guards_test"
node --test "$ROOT/tests/bridge.test.cjs" "$ROOT/tests/native_showcase_ui.test.cjs" "$ROOT/tests/live_demo_ui.test.cjs"
python3 "$ROOT/scripts/preflight_engine.py"
