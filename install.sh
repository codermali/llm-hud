#!/bin/sh
# Installs llm-hud from a local checkout or, when piped from curl, from the
# published repository tarball:
#
#   curl -fsSL https://raw.githubusercontent.com/codermali/llm-hud/main/install.sh | sh
#
set -eu

repo_tarball=${LLM_HUD_TARBALL_URL:-"https://github.com/codermali/llm-hud/archive/refs/heads/main.tar.gz"}
install_root=${LLM_HUD_INSTALL_DIR:-"$HOME/.local/share/llm-hud"}
bin_dir=${LLM_HUD_BIN_DIR:-"$HOME/.local/bin"}

sh_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

python_bin=${LLM_HUD_PYTHON:-$(find_python || true)}
if [ -z "$python_bin" ]; then
  printf '%s\n' "llm-hud requires Python 3.11 or newer, and none was found on PATH." >&2
  printf '%s\n' "Set LLM_HUD_PYTHON=/path/to/python3.11 and run the installer again." >&2
  exit 1
fi
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  printf '%s\n' "llm-hud requires Python 3.11 or newer: $python_bin is too old." >&2
  exit 1
fi

# Locate the source tree: the directory containing this script when run from a
# checkout, otherwise a fresh download of the repository tarball.
cleanup_dir=""
trap '[ -n "$cleanup_dir" ] && rm -rf "$cleanup_dir"' EXIT
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || script_dir=""
if [ -n "$script_dir" ] && [ -f "$script_dir/src/llm_hud/cli.py" ]; then
  source_root=$script_dir
else
  cleanup_dir=$(mktemp -d)
  printf 'Downloading llm-hud from %s\n' "$repo_tarball"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$repo_tarball" | tar -xz -C "$cleanup_dir"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$repo_tarball" | tar -xz -C "$cleanup_dir"
  else
    printf '%s\n' "Neither curl nor wget is available to download llm-hud." >&2
    exit 1
  fi
  source_root=$(find "$cleanup_dir" -mindepth 1 -maxdepth 1 -type d -print | head -n 1)
  if [ -z "$source_root" ] || [ ! -f "$source_root/src/llm_hud/cli.py" ]; then
    printf '%s\n' "The downloaded archive does not look like an llm-hud source tree." >&2
    exit 1
  fi
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

# A wrapper pinning the detected interpreter, not a symlink: the launcher's
# `env python3` shebang may resolve to a Python older than 3.11.
mkdir -p "$bin_dir"
rm -f "$bin_dir/llm-hud"
{
  printf '#!/bin/sh\n'
  printf 'exec %s %s "$@"\n' \
    "$(sh_quote "$python_bin")" "$(sh_quote "$install_root/bin/llm-hud")"
} >"$bin_dir/llm-hud"
chmod +x "$bin_dir/llm-hud"

configure_status=0
LLM_HUD_COMMAND_PATH="$bin_dir/llm-hud" \
  "$python_bin" "$install_root/bin/llm-hud" install || configure_status=$?

printf '%s\n' "LLM HUD installed."
printf 'Command: %s\n' "$bin_dir/llm-hud"
printf 'Check:   %s doctor\n' "$bin_dir/llm-hud"
if [ "$configure_status" -ne 0 ]; then
  printf '%s\n' "Provider configuration did not complete; run the install command again once a supported CLI is installed."
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    printf 'Note: %s is not on your PATH. Add this to your shell profile:\n' "$bin_dir"
    printf '  export PATH="%s:$PATH"\n' "$bin_dir"
    ;;
esac

exit "$configure_status"
