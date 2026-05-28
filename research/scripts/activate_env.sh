#!/usr/bin/env bash
# Activate the project's Python environment.
# The in-tree .venv/ is unusable on macOS Tahoe due to iCloud "dataless" flags on .pyc files;
# instead we keep the venv at ~/code/kvenv (outside iCloud's Optimize-Storage scope) and
# symlink it at research/.venv-research for callers that expect an in-tree path.
#
# Usage:   source research/scripts/activate_env.sh
#          # or:  ~/code/kvenv/bin/python3 <script>
export KVENV="${HOME}/code/kvenv"
source "${KVENV}/bin/activate"
export PYTHON="${KVENV}/bin/python3"
