from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from llm_hud.runtime import (
    ACTIVATION_NAME,
    INSTALL_MARKER_NAME,
    INSTALL_MARKER_VALUE,
    MAX_MANAGED_TEXT_SIZE,
    RUNTIME_MARKER_NAME,
    RuntimeLayoutError,
    initialize_layout,
    runtime_path,
    validate_runtime,
)
from tests.support import activate, finalize_runtime


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER_SOURCE = ROOT / "scripts" / "llm-hud-dispatcher"
CONTROL_SOURCE = ROOT / "scripts" / "runtime_control.py"


def install_stable_control(root: Path) -> Path:
    (root / INSTALL_MARKER_NAME).write_text(f"{INSTALL_MARKER_VALUE}\n")
    initialize_layout(root)
    (root / "bin").mkdir()
    (root / "control").mkdir()
    dispatcher = root / "bin" / "llm-hud"
    control = root / "control" / "runtime_control.py"
    shutil.copyfile(DISPATCHER_SOURCE, dispatcher)
    shutil.copyfile(CONTROL_SOURCE, control)
    dispatcher.chmod(0o755)
    control.chmod(0o755)
    return dispatcher


def stage_runtime(root: Path, label: str, version: str) -> Path:
    path = root / f".llm-hud-stage-{label}"
    package = path / "src" / "llm_hud"
    package.mkdir(parents=True)
    (path / "bin").mkdir()
    launcher = path / "bin" / "llm-hud"
    launcher.write_text("#!/usr/bin/env python3\nraise SystemExit('not dispatched')\n")
    launcher.chmod(0o755)
    (package / "__init__.py").write_text("")
    (package / "_version.py").write_text(f'__version__ = "{version}"\n')
    (package / "cli.py").write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "def main():\n"
        f"    print(json.dumps({{'label': {label!r}, 'arguments': sys.argv[1:], "
        "'pid': os.getpid()}, sort_keys=True))\n"
        "    return 0\n"
    )
    (path / "README.md").write_text(f"readme {label}\n")
    (path / "LICENSE").write_text("license\n")
    (path / "pyproject.toml").write_text(f'version = "{version}"\n')
    return path


def create_runtime(root: Path, label: str, version: str) -> str:
    return finalize_runtime(root, stage_runtime(root, label, version), version).release_id


def run_dispatcher(
    dispatcher: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(dispatcher), *arguments],
        cwd="/",
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )


def load_installed_control(root: Path):
    path = root / "control" / "runtime_control.py"
    name = f"_test_stable_control_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class StableDispatcherTests(unittest.TestCase):
    def test_dispatches_arguments_from_a_path_with_spaces_and_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install space's"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)

            result = run_dispatcher(dispatcher, "render", "a b", "quote's")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["label"], "first")
            self.assertEqual(payload["arguments"], ["render", "a b", "quote's"])

    def test_rejects_a_symlinked_activation_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)
            activation = root / ACTIVATION_NAME
            external = root / "external-activation"
            external.write_bytes(activation.read_bytes())
            activation.unlink()
            activation.symlink_to(external.name)

            result = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("activation record", result.stderr)
            self.assertEqual(external.read_text(), f"llm-hud-activation-v1 {release_id} -\n")

    def test_rejects_a_noncanonical_activation_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)
            activation = root / ACTIVATION_NAME
            activation.write_text(f"llm-hud-activation-v1  {release_id} -\n")

            malformed = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(malformed.returncode, 0)
            self.assertIn("invalid activation", malformed.stderr)

    def test_dispatches_from_a_legacy_activation_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)
            (root / ACTIVATION_NAME).write_text(
                f"llm-hud-activation-v1 {release_id} 0.9.0-deadbeefdead\n"
            )

            result = run_dispatcher(dispatcher, "doctor")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["label"], "first")

    def test_rejects_a_symlinked_stable_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            control = root / "control" / "runtime_control.py"
            external = root / "external-control.py"
            external.write_bytes(control.read_bytes())
            control.unlink()
            control.symlink_to(external)

            result = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stable runtime control", result.stderr)

    def test_reports_a_truncated_stable_control_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            control = root / "control" / "runtime_control.py"
            source = control.read_text()
            control.write_text(source[: source.index("def run(") + len("def ")])

            result = run_dispatcher(dispatcher, "doctor")

            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            lines = result.stderr.splitlines()
            self.assertEqual(len(lines), 1, result.stderr)
            self.assertIn("llm-hud: stable runtime control is damaged", lines[0])
            self.assertIn("install.sh", lines[0])

    def test_rejects_a_symlinked_ownership_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            marker = root / INSTALL_MARKER_NAME
            external = root / "external-marker"
            external.write_bytes(marker.read_bytes())
            marker.unlink()
            marker.symlink_to(external.name)

            result = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install marker", result.stderr)


class RuntimeValidatorParityTests(unittest.TestCase):
    def test_package_and_stable_control_apply_the_same_marker_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            install_stable_control(root)
            release_id = create_runtime(root, "current", "1.0.0")
            control = load_installed_control(root)
            marker = runtime_path(root, release_id) / RUNTIME_MARKER_NAME
            canonical = marker.read_bytes()
            maximum_size = canonical + b" " * (
                MAX_MANAGED_TEXT_SIZE - len(canonical)
            )

            def altered(**updates: object) -> bytes:
                payload = json.loads(canonical)
                payload.update(updates)
                return (json.dumps(payload, sort_keys=True) + "\n").encode()

            fixtures = (
                ("canonical", canonical, True),
                ("maximum-size", maximum_size, True),
                ("too-large", maximum_size + b" ", False),
                ("malformed-json", b"{\n", False),
                ("boolean-schema", altered(schema=True), False),
                ("unexpected-field", altered(unexpected=True), False),
                ("invalid-release", altered(release_id="../escape"), False),
                ("invalid-version", altered(version="latest"), False),
                ("invalid-digest", altered(content_sha256="A" * 64), False),
            )

            self.assertEqual(control.MAX_MANAGED_TEXT_SIZE, MAX_MANAGED_TEXT_SIZE)
            for label, content, expected in fixtures:
                with self.subTest(label=label):
                    marker.write_bytes(content)
                    try:
                        validate_runtime(root, release_id)
                    except RuntimeLayoutError:
                        package_accepts = False
                    else:
                        package_accepts = True
                    try:
                        control._validate_runtime(root, release_id)
                    except control.ControlError:
                        stable_control_accepts = False
                    else:
                        stable_control_accepts = True

                    self.assertEqual(package_accepts, expected)
                    self.assertEqual(stable_control_accepts, expected)

if __name__ == "__main__":
    unittest.main()
