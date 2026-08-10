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
# Resolve to an absolute path so the launcher does not depend on the caller's
# PATH: Claude Code and Codex run the status line with a reduced environment.
if ! python_bin=$(command -v "$python_bin"); then
  printf '%s\n' "llm-hud: no such Python interpreter: ${LLM_HUD_PYTHON:-$python_bin}" >&2
  exit 1
fi
case "$python_bin" in
  /*) ;;
  *) python_bin=$(CDPATH= cd -- "$(dirname -- "$python_bin")" && pwd)/$(basename -- "$python_bin") ;;
esac
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  printf '%s\n' "llm-hud requires Python 3.11 or newer: $python_bin is too old." >&2
  exit 1
fi

# Locate the source tree: the directory containing this script when run from a
# checkout, otherwise a fresh download of the repository tarball.
cleanup_dir=""
trap '[ -n "$cleanup_dir" ] && rm -rf "$cleanup_dir"' EXIT
# $0 is not a readable file when the script arrives on stdin (curl | sh); only
# then does dirname "$0" point at the caller's cwd rather than a checkout.
script_dir=""
if [ -f "$0" ]; then
  script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || script_dir=""
fi
if [ -n "$script_dir" ] && [ -f "$script_dir/src/llm_hud/cli.py" ]; then
  source_root=$script_dir
  printf 'Installing from %s\n' "$source_root"
else
  cleanup_dir=$(mktemp -d)
  printf 'Downloading llm-hud from %s\n' "$repo_tarball"
  archive="$cleanup_dir/llm-hud.tar.gz"
  # Download to a file first: in POSIX sh a pipeline reports only tar's status,
  # and bsdtar exits 0 on empty input, which would hide a failed download.
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$archive" "$repo_tarball"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$archive" "$repo_tarball"
  else
    printf '%s\n' "Neither curl nor wget is available to download llm-hud." >&2
    exit 1
  fi
  tar -xzf "$archive" -C "$cleanup_dir"
  source_root=$(find "$cleanup_dir" -mindepth 1 -maxdepth 1 -type d -print | head -n 1)
  if [ -z "$source_root" ] || [ ! -f "$source_root/src/llm_hud/cli.py" ]; then
    printf '%s\n' "The downloaded archive does not look like an llm-hud source tree." >&2
    exit 1
  fi
fi

# Canonicalize before comparing: a trailing slash, relative spelling, or
# symlinked path for LLM_HUD_INSTALL_DIR must not let the rm -rf below delete
# the source tree.
mkdir -p "$install_root"
install_root=$(CDPATH= cd -- "$install_root" && pwd -P)

if [ "$source_root" != "$install_root" ]; then
  rm -rf "$install_root/src" "$install_root/bin"
  cp -R "$source_root/src" "$install_root/src"
  cp -R "$source_root/bin" "$install_root/bin"
  cp "$source_root/README.md" "$install_root/README.md"
  cp "$source_root/LICENSE" "$install_root/LICENSE"
  cp "$source_root/pyproject.toml" "$install_root/pyproject.toml"
fi
chmod +x "$install_root/bin/llm-hud"

# A wrapper pinning the detected interpreter, not a symlink: the launcher's
# `env python3` shebang may resolve to a Python older than 3.11. Written to a
# temp file and renamed so a concurrent status-line refresh never sees a
# partial or non-executable launcher.
mkdir -p "$bin_dir"
wrapper_tmp="$bin_dir/.llm-hud.$$"
{
  printf '#!/bin/sh\n'
  printf 'exec %s %s "$@"\n' \
    "$(sh_quote "$python_bin")" "$(sh_quote "$install_root/bin/llm-hud")"
} >"$wrapper_tmp"
chmod +x "$wrapper_tmp"
mv -f "$wrapper_tmp" "$bin_dir/llm-hud"

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
