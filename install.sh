#!/bin/sh
set -eu

source_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_root=${LLM_HUD_INSTALL_DIR:-"$HOME/.local/share/llm-hud"}
bin_dir=${LLM_HUD_BIN_DIR:-"$HOME/.local/bin"}
python_bin=${LLM_HUD_PYTHON:-$(command -v python3 || true)}

if [ -z "$python_bin" ] || ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf '%s\n' "llm-hud requires Python 3.11 or newer." >&2
  exit 1
fi

if [ "$source_root" != "$install_root" ]; then
  mkdir -p "$install_root"
  rm -rf "$install_root/src" "$install_root/bin"
  cp -R "$source_root/src" "$install_root/src"
  cp -R "$source_root/bin" "$install_root/bin"
  cp "$source_root/README.md" "$install_root/README.md"
  cp "$source_root/LICENSE" "$install_root/LICENSE"
  cp "$source_root/pyproject.toml" "$install_root/pyproject.toml"
fi

chmod +x "$install_root/bin/llm-hud"
mkdir -p "$bin_dir"
ln -sfn "$install_root/bin/llm-hud" "$bin_dir/llm-hud"

LLM_HUD_COMMAND_PATH="$bin_dir/llm-hud" \
  "$python_bin" "$install_root/bin/llm-hud" install

printf '%s\n' "LLM HUD installed."
printf 'Command: %s\n' "$bin_dir/llm-hud"
printf 'Check:   %s doctor\n' "$bin_dir/llm-hud"
