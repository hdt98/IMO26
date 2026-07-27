#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
elan_bin="${ELAN_BIN:-$HOME/.elan/bin/elan}"
lake_bin="${LAKE_BIN:-$HOME/.elan/bin/lake}"

if [[ ! -x "$elan_bin" ]]; then
  curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh |
    sh -s -- -y --default-toolchain none
fi

if ! "$elan_bin" toolchain list | grep -qx "leanprover/lean4:v4.28.0"; then
  "$elan_bin" toolchain install leanprover/lean4:v4.28.0
fi
cd "$repo_root/lean"
"$lake_bin" update
"$lake_bin" exe cache get
"$lake_bin" env lean --version
