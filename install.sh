#!/bin/sh
# Installs llm-hud from a local checkout or, when piped from curl, from the
# published repository tarball:
#
#   curl -fsSL https://raw.githubusercontent.com/codermali/llm-hud/main/install.sh | sh
#
set -eu

default_repo_tarball=https://github.com/codermali/llm-hud/archive/refs/heads/main.tar.gz
repo_tarball=${LLM_HUD_TARBALL_URL:-"$default_repo_tarball"}
install_root=${LLM_HUD_INSTALL_DIR:-"$HOME/.local/share/llm-hud"}
bin_dir=${LLM_HUD_BIN_DIR:-"$HOME/.local/bin"}
install_marker_name=.llm-hud-install-root
install_marker_value=llm-hud-install-root-v1
carriage_return=$(printf '\r')

reject_line_break() {
  case "$2" in
    *"$carriage_return"*|*'
'*)
      printf '%s\n' "Refusing $1 containing a carriage return or newline." >&2
      exit 1
      ;;
  esac
}

canonical_directory() {
  canonical_result=$(
    CDPATH= cd -- "$1" 2>/dev/null
    pwd -P
    printf '.'
  ) || return 1
  canonical_suffix='
.'
  case "$canonical_result" in
    *"$canonical_suffix") ;;
    *) return 1 ;;
  esac
  canonical_result=${canonical_result%"$canonical_suffix"}
}

reject_line_break LLM_HUD_INSTALL_DIR "$install_root"
reject_line_break LLM_HUD_BIN_DIR "$bin_dir"
reject_line_break LLM_HUD_PYTHON "${LLM_HUD_PYTHON:-}"
reject_line_break HOME "$HOME"
reject_line_break PATH "$PATH"
reject_line_break installer-path "$0"

directory_is_empty() {
  set -- "$1"/* "$1"/.[!.]* "$1"/..?*
  for entry do
    if [ -e "$entry" ] || [ -L "$entry" ]; then
      return 1
    fi
  done
  return 0
}

claim_recovery_shape() {
  count=0
  set -- "$1"/* "$1"/.[!.]* "$1"/..?*
  for entry do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    case "${entry##*/}" in
      .llm-hud-claim-*)
        [ ! -L "$entry" ] && [ -f "$entry" ] || return 1
        count=$((count + 1))
        ;;
      *) return 1 ;;
    esac
  done
  [ "$count" -eq 1 ]
}

legacy_runtime_shape() {
  [ ! -e "$1/install.sh" ] &&
    [ ! -d "$1/.git" ] &&
    [ ! -d "$1/tests" ] &&
    [ -f "$1/src/llm_hud/cli.py" ] &&
    [ -f "$1/bin/llm-hud" ] &&
    [ -f "$1/README.md" ] &&
    [ -f "$1/LICENSE" ] &&
    [ -f "$1/pyproject.toml" ]
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
  *)
    if ! canonical_directory "$(dirname -- "$python_bin")"; then
      printf '%s\n' "llm-hud: cannot resolve Python interpreter: $python_bin" >&2
      exit 1
    fi
    python_dir=$canonical_result
    python_name=$(basename -- "$python_bin")
    python_bin="$python_dir/$python_name"
    ;;
esac
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  printf '%s\n' "llm-hud requires Python 3.11 or newer: $python_bin is too old." >&2
  exit 1
fi
reject_line_break resolved-python "$python_bin"

# Locate the source tree: the directory containing this script when run from a
# checkout, otherwise a fresh download of the repository tarball.
cleanup_dir=""
trap '[ -n "$cleanup_dir" ] && rm -rf "$cleanup_dir"' EXIT
# $0 is not a readable file when the script arrives on stdin (curl | sh); only
# then does dirname "$0" point at the caller's cwd rather than a checkout.
script_dir=""
if [ -f "$0" ]; then
  if canonical_directory "$(dirname -- "$0")"; then
    script_dir=$canonical_result
  fi
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

reject_line_break source-root "$source_root"

# Canonicalize before comparing so checkout aliases never enter the managed
# installation branch.
mkdir -p "$install_root"
if ! canonical_directory "$install_root"; then
  printf '%s\n' "Cannot resolve LLM_HUD_INSTALL_DIR: $install_root" >&2
  exit 1
fi
install_root=$canonical_result
reject_line_break canonical-install-root "$install_root"
mkdir -p "$bin_dir"
if ! canonical_directory "$bin_dir"; then
  printf '%s\n' "Cannot resolve LLM_HUD_BIN_DIR: $bin_dir" >&2
  exit 1
fi
bin_dir=$canonical_result
reject_line_break canonical-bin-dir "$bin_dir"
launcher_path="$bin_dir/llm-hud"
legacy_flat=0
legacy_unmarked=0
installer_bootstrap='import runpy
import sys
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module("llm_hud.installer", run_name="__main__")'

if [ "$source_root" != "$install_root" ]; then
  # Existing 0.1.0 installations predate the marker, so recognize their full
  # flat runtime shape once and adopt only the canonical default directory.
  home_root=""
  home_local_root=""
  home_local_share_root=""
  home_local_bin_root=""
  if canonical_directory "$HOME"; then
    home_root=$canonical_result
  fi
  if canonical_directory "$HOME/.local"; then
    home_local_root=$canonical_result
  fi
  if canonical_directory "$HOME/.local/share"; then
    home_local_share_root=$canonical_result
  fi
  if canonical_directory "$HOME/.local/bin"; then
    home_local_bin_root=$canonical_result
  fi
  case "$install_root" in
    /|//|"$home_root"|"$home_local_root"|"$home_local_share_root"|"$home_local_bin_root")
      printf '%s\n' "Refusing unsafe LLM_HUD_INSTALL_DIR: $install_root" >&2
      printf '%s\n' "Choose a dedicated directory such as $HOME/.local/share/llm-hud." >&2
      exit 1
      ;;
  esac

  legacy_default_root=""
  if [ -d "$HOME/.local/share/llm-hud" ]; then
    if canonical_directory "$HOME/.local/share/llm-hud"; then
      legacy_default_root=$canonical_result
    fi
  fi
  install_marker="$install_root/$install_marker_name"
  if [ -L "$install_marker" ]; then
    printf '%s\n' "Refusing symlink install marker: $install_marker" >&2
    exit 1
  elif [ -e "$install_marker" ] && [ ! -f "$install_marker" ]; then
    printf '%s\n' "Refusing non-file install marker: $install_marker" >&2
    exit 1
  elif [ -f "$install_marker" ]; then
    marker_value=$(sed -n '1p' "$install_marker" 2>/dev/null || true)
    if [ "$marker_value" != "$install_marker_value" ]; then
      printf '%s\n' "Refusing unrecognized install marker: $install_marker" >&2
      exit 1
    fi
    if legacy_runtime_shape "$install_root"; then
      legacy_flat=1
    fi
  elif [ "$install_root" = "$legacy_default_root" ] &&
    legacy_runtime_shape "$install_root"; then
    legacy_flat=1
    legacy_unmarked=1
  elif claim_recovery_shape "$install_root"; then
    : # Python verifies and repairs the interrupted exclusive ownership claim.
  elif ! directory_is_empty "$install_root"; then
    printf '%s\n' "Refusing non-empty unmanaged install directory: $install_root" >&2
    printf '%s\n' "Choose an empty directory dedicated to llm-hud." >&2
    exit 1
  fi

  set -- \
    --source "$source_root" \
    --root "$install_root" \
    --launcher "$launcher_path" \
    --python "$python_bin" \
    --claim-root
  if [ "$legacy_flat" -eq 1 ]; then
    set -- "$@" --allow-legacy-dispatcher
  fi
  if [ "$legacy_unmarked" -eq 1 ]; then
    set -- "$@" --allow-legacy-root
  fi
  "$python_bin" -I -B -c "$installer_bootstrap" "$source_root/src" "$@"
else
  "$python_bin" -I -B -c "$installer_bootstrap" "$source_root/src" \
    --source "$source_root" \
    --root "$install_root" \
    --launcher "$launcher_path" \
    --python "$python_bin" \
    --checkout
fi

configure_status=0
LLM_HUD_COMMAND_PATH="$launcher_path" \
  "$launcher_path" install || configure_status=$?

printf '%s\n' "LLM HUD installed."
printf 'Command: %s\n' "$bin_dir/llm-hud"
printf 'Check:   %s doctor\n' "$bin_dir/llm-hud"
if [ "$configure_status" -ne 0 ]; then
  printf '%s\n' \
    "Provider configuration did not complete; rerun after installing a supported CLI."
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    printf 'Note: %s is not on your PATH. Add this to your shell profile:\n' "$bin_dir"
    printf '  export PATH="%s:$PATH"\n' "$bin_dir"
    ;;
esac

exit "$configure_status"
