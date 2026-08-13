#!/bin/sh
# Installs llm-hud from a local checkout or, when piped from curl, from the
# published repository tarball:
#
#   curl -fsSL https://github.com/codermali/llm-hud/releases/latest/download/install.sh | sh
#
set -eu

embedded_release_tag='__LLM_HUD_RELEASE_TAG__'
if [ "$embedded_release_tag" = '__LLM_HUD_RELEASE_TAG__' ]; then
  default_release_base=https://github.com/codermali/llm-hud/releases/latest/download
else
  default_release_base="https://github.com/codermali/llm-hud/releases/download/$embedded_release_tag"
fi
default_repo_tarball=$default_release_base/llm-hud.tar.gz
default_repo_checksum=$default_release_base/SHA256SUMS
if [ "${LLM_HUD_TARBALL_URL+x}" = x ]; then
  if [ -z "$LLM_HUD_TARBALL_URL" ]; then
    printf '%s\n' "LLM_HUD_TARBALL_URL is set but empty." >&2
    printf '%s\n' "Unset it to use the release download, or set it to a tarball URL." >&2
    exit 1
  fi
  repo_tarball=$LLM_HUD_TARBALL_URL
  repo_checksum=${LLM_HUD_CHECKSUM_URL:-}
else
  repo_tarball=$default_repo_tarball
  repo_checksum=${LLM_HUD_CHECKSUM_URL:-"$default_repo_checksum"}
fi
install_root=${LLM_HUD_INSTALL_DIR:-"$HOME/.local/share/llm-hud"}
bin_dir=${LLM_HUD_BIN_DIR:-"$HOME/.local/bin"}
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
reject_line_break LLM_HUD_TARBALL_URL "$repo_tarball"
reject_line_break LLM_HUD_CHECKSUM_URL "$repo_checksum"
reject_line_break HOME "$HOME"
reject_line_break PATH "$PATH"
reject_line_break installer-path "$0"

find_python() {
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

download_file() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 15 --max-time 300 --retry 3 --retry-delay 1 \
      -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --timeout=15 --tries=3 -O "$2" "$1"
  else
    printf '%s\n' "Neither curl nor wget is available to download llm-hud." >&2
    exit 1
  fi
}

# The two release assets must come from the same release: resolve the latest
# tag once and pin both downloads to it, so an install that races a release
# publication cannot pair a new tarball with an old checksum file.
pin_release_assets() {
  # A release-attached installer has its own tag embedded by release.yml. Keep
  # all three assets on that tag instead of resolving `latest` a second time.
  [ "$embedded_release_tag" = '__LLM_HUD_RELEASE_TAG__' ] || return 0
  if command -v curl >/dev/null 2>&1; then
    release_url=$(
      curl -fsSL --connect-timeout 15 --max-time 60 -o /dev/null \
        -w '%{url_effective}' \
        https://github.com/codermali/llm-hud/releases/latest 2>/dev/null
    ) || return 0
  elif command -v wget >/dev/null 2>&1; then
    # GNU wget reports the redirect target on stderr; a wget without these
    # options leaves release_url empty and keeps the unpinned fallback.
    release_url=$(
      wget --spider --server-response --max-redirect=0 --timeout=15 --tries=1 \
        https://github.com/codermali/llm-hud/releases/latest 2>&1 |
        sed -n 's/^[[:space:]]*[Ll]ocation:[[:space:]]*//p' |
        head -n 1 |
        tr -d '\r'
    ) || release_url=""
    [ -n "$release_url" ] || return 0
  else
    return 0
  fi
  case "$release_url" in
    https://github.com/codermali/llm-hud/releases/tag/*) ;;
    *) return 0 ;;
  esac
  release_tag=${release_url##*/}
  reject_line_break release-tag "$release_tag"
  release_base="https://github.com/codermali/llm-hud/releases/download/$release_tag"
  repo_tarball="$release_base/llm-hud.tar.gz"
  if [ "$repo_checksum" = "$default_repo_checksum" ]; then
    repo_checksum="$release_base/SHA256SUMS"
  fi
}

python_bin=${LLM_HUD_PYTHON:-$(find_python || true)}
if [ -z "$python_bin" ]; then
  printf '%s\n' "llm-hud requires Python 3.9 or newer, and none was found on PATH." >&2
  printf '%s\n' "Set LLM_HUD_PYTHON=/path/to/python3.9 and run the installer again." >&2
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
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' 2>/dev/null; then
  printf '%s\n' "llm-hud requires Python 3.9 or newer: $python_bin is too old." >&2
  exit 1
fi
reject_line_break resolved-python "$python_bin"

# Locate the source tree: the directory containing this script when run from a
# checkout, otherwise a fresh download of the repository tarball.
cleanup_dir=""
cleanup_temp_dir() {
  [ -z "$cleanup_dir" ] || rm -rf "$cleanup_dir" || :
}
trap 'cleanup_temp_dir' EXIT
# dash runs the EXIT trap only on normal exit, so Ctrl-C or a kill during the
# download would leak the mktemp directory; clean up on the signal too and
# leave the conventional 128+signal exit status in place.
trap 'cleanup_temp_dir; trap - EXIT; exit 129' HUP
trap 'cleanup_temp_dir; trap - EXIT; exit 130' INT
trap 'cleanup_temp_dir; trap - EXIT; exit 143' TERM
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
  if [ "$repo_tarball" = "$default_repo_tarball" ]; then
    pin_release_assets
  fi
  cleanup_dir=$(mktemp -d)
  printf 'Downloading llm-hud from %s\n' "$repo_tarball"
  archive="$cleanup_dir/llm-hud.tar.gz"
  # Download to a file first: in POSIX sh a pipeline reports only tar's status,
  # and bsdtar exits 0 on empty input, which would hide a failed download.
  download_file "$repo_tarball" "$archive"
  if [ -n "$repo_checksum" ]; then
    checksum="$cleanup_dir/SHA256SUMS"
    printf 'Verifying llm-hud with %s\n' "$repo_checksum"
    download_file "$repo_checksum" "$checksum"
    "$python_bin" -I -B -c '
import hashlib
import pathlib
import sys

checksum_path = pathlib.Path(sys.argv[1])
archive_path = pathlib.Path(sys.argv[2])
# A custom mirror may list the artifact under its published filename rather
# than llm-hud.tar.gz, so accept the tarball URL basename as well.
expected_names = {"llm-hud.tar.gz", sys.argv[3].rpartition("/")[2]}
expected_names.discard("")
matches = []
for line in checksum_path.read_text(encoding="ascii").splitlines():
    fields = line.split()
    if len(fields) == 2 and fields[1].lstrip("*") in expected_names:
        matches.append(fields[0])
if (
    len(matches) != 1
    or len(matches[0]) != 64
    or any(character not in "0123456789abcdef" for character in matches[0])
):
    raise SystemExit("llm-hud: invalid SHA256SUMS")
digest = hashlib.sha256()
with archive_path.open("rb") as archive_file:
    for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != matches[0]:
    raise SystemExit(
        "llm-hud: release archive checksum mismatch "
        "(a release may be publishing right now; retry in a minute)"
    )
' "$checksum" "$archive" "$repo_tarball"
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
mkdir -p -- "$install_root"
if ! canonical_directory "$install_root"; then
  printf '%s\n' "Cannot resolve LLM_HUD_INSTALL_DIR: $install_root" >&2
  exit 1
fi
install_root=$canonical_result
reject_line_break canonical-install-root "$install_root"
mkdir -p -- "$bin_dir"
if ! canonical_directory "$bin_dir"; then
  printf '%s\n' "Cannot resolve LLM_HUD_BIN_DIR: $bin_dir" >&2
  exit 1
fi
bin_dir=$canonical_result
reject_line_break canonical-bin-dir "$bin_dir"
launcher_path="$bin_dir/llm-hud"
installer_bootstrap='import runpy
import sys
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module("llm_hud.installer", run_name="__main__")'

# Ask on /dev/tty because stdin is the script itself under curl | sh; without
# a terminal (CI, automation) continue so reinstalls stay non-interactive.
confirm_or_keep() {
  if (exec < /dev/tty) 2>/dev/null; then
    printf '%s [y/N] ' "$1" > /dev/tty
    IFS= read -r reinstall_answer < /dev/tty || reinstall_answer=""
    case "$reinstall_answer" in
      y|Y|yes|YES) ;;
      *)
        printf '%s\n' "Keeping the existing installation."
        exit 0
        ;;
    esac
  else
    printf '%s\n' "$1 (no terminal; continuing)"
  fi
}

source_version=$(
  "$python_bin" -I -B -c \
    'import runpy, sys; print(runpy.run_path(sys.argv[1])["__version__"])' \
    "$source_root/src/llm_hud/_version.py" 2>/dev/null
) || source_version=""
installed_version=""
if [ -x "$launcher_path" ]; then
  installed_version=$("$launcher_path" --version 2>/dev/null | sed 's/^llm-hud //') || installed_version=""
fi
if [ -n "$installed_version" ] && [ -n "$source_version" ]; then
  relation=$(
    "$python_bin" -I -B -c '
import sys

def core(value):
    parts = value.split("+")[0].split("-")[0].split(".")
    try:
        return [int(part) for part in parts]
    except ValueError:
        return None

installed, source = sys.argv[1], sys.argv[2]
if installed == source:
    print("same")
else:
    a, b = core(installed), core(source)
    if a is None or b is None or a == b:
        print("same")
    elif a < b:
        print("upgrade")
    else:
        print("downgrade")
' "$installed_version" "$source_version" 2>/dev/null
  ) || relation=""
  case "$relation" in
    same)
      confirm_or_keep "llm-hud $installed_version is already installed. Reinstall?"
      ;;
    upgrade)
      printf 'Detected llm-hud %s; upgrading to %s.\n' \
        "$installed_version" "$source_version"
      ;;
    downgrade)
      confirm_or_keep "Installed llm-hud $installed_version is newer than $source_version. Downgrade?"
      ;;
  esac
fi

# Root safety (ownership marker, dangerous or non-empty directories) is the
# Python installer's job; the shell only decides checkout vs managed install.
if [ "$source_root" != "$install_root" ]; then
  "$python_bin" -I -B -c "$installer_bootstrap" "$source_root/src" \
    --source "$source_root" \
    --root "$install_root" \
    --launcher "$launcher_path" \
    --python "$python_bin" \
    --claim-root
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

# PATH may spell the launcher directory differently (trailing slash, symlink),
# so canonicalize each entry before deciding to print the hint; entries that do
# not resolve cannot be $bin_dir, which exists.
bin_dir_on_path=""
path_remainder="$PATH:"
while [ -n "$path_remainder" ]; do
  path_entry=${path_remainder%%:*}
  path_remainder=${path_remainder#*:}
  [ -n "$path_entry" ] || continue
  canonical_directory "$path_entry" || continue
  if [ "$canonical_result" = "$bin_dir" ]; then
    bin_dir_on_path=x
    break
  fi
done
if [ -z "$bin_dir_on_path" ]; then
  printf 'Note: %s is not on your PATH. Add this to your shell profile:\n' "$bin_dir"
  printf '  export PATH="%s:$PATH"\n' "$bin_dir"
fi

exit "$configure_status"
