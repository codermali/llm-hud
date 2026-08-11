from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
MARKER = ".llm-hud-install-root"


def run_installer(root: Path, install_dir: Path) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        [str(INSTALLER)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )


class InstallerRootSafetyTests(unittest.TestCase):
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

            repeated = run_installer(root, install_dir)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)

    def test_refuses_to_use_home_as_the_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            sentinel = home / "src" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep")

            result = run_installer(root, home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing unsafe LLM_HUD_INSTALL_DIR", result.stderr)
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
                self.assertIn("Refusing unsafe LLM_HUD_INSTALL_DIR", result.stderr)
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
            self.assertIn("Refusing non-empty unmanaged install directory", result.stderr)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_adopts_an_unmarked_legacy_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "home" / ".local" / "share" / "llm-hud"
            first = run_installer(root, install_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            (install_dir / MARKER).unlink()

            second = run_installer(root, install_dir)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue((install_dir / MARKER).is_file())

    def test_does_not_adopt_a_source_tree_as_a_legacy_installation(self):
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
            self.assertIn("Refusing non-empty unmanaged install directory", result.stderr)
            self.assertEqual((install_dir / "src/llm_hud/cli.py").read_text(), "keep")

    def test_running_from_the_install_root_does_not_mark_or_replace_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli_before = (ROOT / "src/llm_hud/cli.py").read_bytes()

            result = run_installer(root, ROOT)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((ROOT / "src/llm_hud/cli.py").read_bytes(), cli_before)
            self.assertFalse((ROOT / MARKER).exists())

    def test_rejects_an_unrecognized_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "runtime"
            install_dir.mkdir()
            (install_dir / MARKER).write_text("not-llm-hud\n")

            result = run_installer(root, install_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing unrecognized install marker", result.stderr)


if __name__ == "__main__":
    unittest.main()
