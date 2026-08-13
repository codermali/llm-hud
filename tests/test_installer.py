from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from llm_hud._version import __version__
from llm_hud.runtime import read_activation, validate_runtime


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
RELEASE_TAG_PLACEHOLDER = "__LLM_HUD_RELEASE_TAG__"
MARKER = ".llm-hud-install-root"
LAYOUT = ".llm-hud-layout"


def find_exact_python(major: int, minor: int) -> str | None:
    candidates = [sys.executable, shutil.which(f"python{major}.{minor}")]
    for candidate in candidates:
        if candidate is None:
            continue
        result = subprocess.run(
            [
                candidate,
                "-c",
                "import sys; raise SystemExit(sys.version_info[:2] != ("
                f"{major}, {minor}))",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return candidate
    return None


def tracked_source_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return sorted(
        Path(os.fsdecode(raw))
        for raw in result.stdout.split(b"\0")
        if raw and os.path.lexists(ROOT / Path(os.fsdecode(raw)))
    )


def make_source_archive(archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as bundle:
        for relative in tracked_source_paths():
            path = ROOT / relative
            bundle.add(
                path,
                arcname=Path("llm-hud-main") / relative,
                recursive=False,
            )


def installer_environment(
    root: Path,
    install_dir: Path,
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LLM_HUD_HOME": str(home),
            "LLM_HUD_INSTALL_DIR": str(install_dir),
            "LLM_HUD_BIN_DIR": str(root / "launcher-bin"),
            "LLM_HUD_STATE_DIR": str(root / "state"),
            "LLM_HUD_CLAUDE_SETTINGS": str(root / "claude-settings.json"),
            "LLM_HUD_CODEX_CONFIG": str(root / "codex-config.toml"),
            "LLM_HUD_CLAUDE_BIN": "1",
            "LLM_HUD_CODEX_BIN": "",
            "LLM_HUD_KIMI_BIN": "",
            "LLM_HUD_PYTHON": sys.executable,
        }
    )
    if overrides:
        env.update(overrides)
    return env


def run_installer(
    root: Path,
    install_dir: Path,
    *,
    cwd: Path = ROOT,
    installer: Path = INSTALLER,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(installer)],
        cwd=cwd,
        env=installer_environment(root, install_dir, overrides=overrides),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )


class InstallerRootSafetyTests(unittest.TestCase):
    def test_test_release_archive_contains_only_tracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "llm-hud.tar.gz"
            make_source_archive(archive)

            with tarfile.open(archive, "r:gz") as bundle:
                archived = {
                    Path(member.name).relative_to("llm-hud-main")
                    for member in bundle.getmembers()
                }

        self.assertEqual(archived, set(tracked_source_paths()))

    def test_python_detection_prefers_3_14_and_keeps_supported_fallbacks(self):
        self.assertIn(
            "for candidate in python3.14 python3.13 python3.12 python3.11 "
            "python3.10 python3.9 python3 python; do",
            INSTALLER.read_text(),
        )

    def test_release_installer_pins_all_downloads_to_its_embedded_tag(self):
        script = INSTALLER.read_text()
        release_script = script.replace(
            f"embedded_release_tag='{RELEASE_TAG_PLACEHOLDER}'",
            "embedded_release_tag='v9.8.7'",
        )

        self.assertIn("embedded_release_tag='v9.8.7'", release_script)
        self.assertIn(
            'releases/download/$embedded_release_tag',
            release_script,
        )
        self.assertIn(
            "[ \"$embedded_release_tag\" = '__LLM_HUD_RELEASE_TAG__' ] || return 0",
            script,
        )
        self.assertNotIn(
            f"embedded_release_tag='{RELEASE_TAG_PLACEHOLDER}'",
            release_script,
        )

    def test_python_3_9_can_verify_and_install_a_release_archive(self):
        python = find_exact_python(3, 9)
        if python is None:
            self.skipTest("Python 3.9 is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "llm-hud.tar.gz"
            make_source_archive(archive)
            checksum = root / "SHA256SUMS"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum.write_text(f"{digest}  llm-hud.tar.gz\n")
            install_dir = root / "runtime"
            environment = installer_environment(
                root,
                install_dir,
                overrides={
                    "LLM_HUD_PYTHON": python,
                    "LLM_HUD_TARBALL_URL": archive.as_uri(),
                    "LLM_HUD_CHECKSUM_URL": checksum.as_uri(),
                },
            )

            result = subprocess.run(
                ["sh"],
                input=INSTALLER.read_text(),
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = root / "launcher-bin" / "llm-hud"
            launched = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            self.assertEqual(launched.stdout.strip(), f"llm-hud {__version__}")

    def test_stdin_bootstrap_downloads_and_installs_a_versioned_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "llm-hud.tar.gz"
            make_source_archive(archive)
            checksum = root / "SHA256SUMS"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum.write_text(f"{digest}  llm-hud.tar.gz\n")
            install_dir = root / "runtime"
            environment = installer_environment(
                root,
                install_dir,
                overrides={
                    "LLM_HUD_TARBALL_URL": archive.as_uri(),
                    "LLM_HUD_CHECKSUM_URL": checksum.as_uri(),
                },
            )

            result = subprocess.run(
                ["sh"],
                input=INSTALLER.read_text(),
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            current = read_activation(install_dir)
            assert current is not None
            validate_runtime(install_dir, current.active)
            launcher = root / "launcher-bin" / "llm-hud"
            launched = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)

    def test_stdin_bootstrap_rejects_a_wrong_release_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "llm-hud.tar.gz"
            make_source_archive(archive)
            checksum = root / "SHA256SUMS"
            checksum.write_text(f"{'0' * 64}  llm-hud.tar.gz\n")
            install_dir = root / "runtime"
            environment = installer_environment(
                root,
                install_dir,
                overrides={
                    "LLM_HUD_TARBALL_URL": archive.as_uri(),
                    "LLM_HUD_CHECKSUM_URL": checksum.as_uri(),
                },
            )

            result = subprocess.run(
                ["sh"],
                input=INSTALLER.read_text(),
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum mismatch", result.stderr)
            self.assertFalse(install_dir.exists())

    def test_current_directory_cannot_shadow_the_downloaded_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            attacker = root / "attacker"
            package = attacker / "llm_hud"
            package.mkdir(parents=True)
            sentinel = root / "shadow-ran"
            (package / "__init__.py").write_text("")
            (package / "installer.py").write_text(
                f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
            )

            result = run_installer(root, install_dir, cwd=attacker)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())
            self.assertIsNotNone(read_activation(install_dir))

    def test_checkout_path_with_a_colon_is_not_split_as_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout:with-colon"
            shutil.copytree(
                ROOT,
                checkout,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            install_dir = root / "runtime"

            result = run_installer(
                root,
                install_dir,
                cwd=checkout,
                installer=checkout / "install.sh",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(read_activation(install_dir))

    def test_canonical_line_break_path_is_rejected_before_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            actual_bin = root / "bin-with-newline\n"
            actual_bin.mkdir()
            bin_alias = root / "safe-bin-alias"
            bin_alias.symlink_to(actual_bin, target_is_directory=True)

            result = run_installer(
                root,
                install_dir,
                overrides={"LLM_HUD_BIN_DIR": str(bin_alias)},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("canonical-bin-dir", result.stderr)
            self.assertFalse((install_dir / MARKER).exists())
            self.assertFalse((install_dir / "activation").exists())

    def test_fresh_dedicated_directory_gets_an_ownership_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"

            result = run_installer(root, install_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (install_dir / MARKER).read_text().strip(),
                "llm-hud-install-root-v1",
            )
            self.assertEqual(
                (install_dir / LAYOUT).read_text().strip(),
                "llm-hud-versioned-layout-v1",
            )
            activation = read_activation(install_dir)
            assert activation is not None
            validate_runtime(install_dir, activation.active)
            self.assertTrue((install_dir / "control" / "runtime_control.py").is_file())
            self.assertTrue((install_dir / "bin" / "llm-hud").is_file())
            launcher = root / "launcher-bin" / "llm-hud"
            self.assertIn("llm-hud-managed-launcher-v1", launcher.read_text())
            version = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), f"llm-hud {__version__}")

            activation_before = (install_dir / "activation").read_bytes()
            versions_before = sorted(path.name for path in (install_dir / "versions").iterdir())
            repeated = run_installer(root, install_dir)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual((install_dir / "activation").read_bytes(), activation_before)
            self.assertEqual(
                sorted(path.name for path in (install_dir / "versions").iterdir()),
                versions_before,
            )

    def test_refuses_to_use_home_as_the_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            sentinel = home / "src" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep")

            result = run_installer(root, home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing unsafe install root", result.stderr)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_refuses_broad_directories_below_home(self):
        for relative in (".local", ".local/share", ".local/bin"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_dir = root / "home" / relative
                sentinel = install_dir / "keep.txt"
                sentinel.parent.mkdir(parents=True)
                sentinel.write_text("keep")

                result = run_installer(root, install_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing unsafe install root", result.stderr)
                self.assertEqual(sentinel.read_text(), "keep")

    def test_refuses_a_nonempty_unmanaged_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "shared"
            sentinel = install_dir / "notes.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep")

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-empty unmanaged install root", result.stderr)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_same_version_reinstall_notes_and_continues_without_a_tty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            install_dir.mkdir()
            first = run_installer(root, install_dir)
            self.assertEqual(first.returncode, 0, first.stderr)

            repeated = run_installer(root, install_dir)

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("is already installed", repeated.stdout)
            self.assertIn("no terminal; continuing", repeated.stdout)

    def test_older_version_upgrades_with_a_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            install_dir.mkdir()
            first = run_installer(root, install_dir)
            self.assertEqual(first.returncode, 0, first.stderr)

            newer_source = root / "newer-source"
            for name in ("src", "bin", "scripts"):
                shutil.copytree(ROOT / name, newer_source / name)
            for name in ("README.md", "LICENSE", "pyproject.toml", "install.sh"):
                shutil.copy2(ROOT / name, newer_source / name)
            (newer_source / "src" / "llm_hud" / "_version.py").write_text(
                '__version__ = "9.9.9"\n'
            )

            upgraded = run_installer(
                root,
                install_dir,
                cwd=newer_source,
                installer=newer_source / "install.sh",
            )

            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertIn("upgrading to 9.9.9", upgraded.stdout)
            launcher = root / "launcher-bin" / "llm-hud"
            version = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(version.stdout.strip(), "llm-hud 9.9.9")

    def test_refuses_a_flat_legacy_installation(self):
        # Flat 0.1.0 layouts are no longer adopted: the directory is simply
        # a non-empty unmanaged root and must be cleared and reinstalled.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "home" / ".local" / "share" / "llm-hud"
            install_dir.mkdir(parents=True)
            for name in ("src", "bin"):
                shutil.copytree(ROOT / name, install_dir / name)
            for name in ("README.md", "LICENSE", "pyproject.toml"):
                shutil.copy2(ROOT / name, install_dir / name)

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "non-empty unmanaged install root", result.stderr
            )
            self.assertFalse((install_dir / MARKER).exists())

    def test_refuses_a_source_tree_as_an_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "checkout"
            for relative in (
                "src/llm_hud/cli.py",
                "bin/llm-hud",
                "README.md",
                "LICENSE",
                "pyproject.toml",
                "install.sh",
            ):
                path = install_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("keep")

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-empty unmanaged install root", result.stderr)
            self.assertEqual((install_dir / "src/llm_hud/cli.py").read_text(), "keep")

    def test_running_from_the_install_root_does_not_mark_or_replace_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli_before = (ROOT / "src/llm_hud/cli.py").read_bytes()
            launcher_before = (ROOT / "bin/llm-hud").read_bytes()
            launcher_mode = (ROOT / "bin/llm-hud").stat().st_mode

            result = run_installer(root, ROOT)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((ROOT / "src/llm_hud/cli.py").read_bytes(), cli_before)
            self.assertEqual((ROOT / "bin/llm-hud").read_bytes(), launcher_before)
            self.assertEqual((ROOT / "bin/llm-hud").stat().st_mode, launcher_mode)
            self.assertFalse((ROOT / MARKER).exists())
            self.assertFalse((ROOT / LAYOUT).exists())

    def test_rejects_an_unrecognized_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            install_dir.mkdir()
            (install_dir / MARKER).write_text("not-llm-hud\n")

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized install marker", result.stderr)

    def test_refuses_to_replace_a_foreign_external_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            launcher = root / "launcher-bin" / "llm-hud"
            launcher.parent.mkdir()
            launcher.write_text("user command")

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unmanaged launcher", result.stderr)
            self.assertEqual(launcher.read_text(), "user command")
            self.assertFalse((install_dir / "activation").exists())

    def test_paths_with_spaces_and_single_quotes_install_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "space's root"
            root.mkdir()
            install_dir = root / "runtime space's"

            result = run_installer(root, install_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = root / "launcher-bin" / "llm-hud"
            completed = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_newline_install_path_is_rejected_before_it_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = Path(f"{root}/bad\npath")

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("carriage return or newline", result.stderr)
            self.assertFalse(install_dir.exists())


if __name__ == "__main__":
    unittest.main()
