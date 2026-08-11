from __future__ import annotations

import contextlib
import io
import tomllib
import unittest
from pathlib import Path

from llm_hud import __version__
from llm_hud._version import __version__ as source_version
from llm_hud.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]


class VersionMetadataTests(unittest.TestCase):
    def test_package_and_cli_read_the_version_module(self):
        self.assertEqual(__version__, source_version)
        parser = build_parser()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            parser.parse_args(["--version"])
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"llm-hud {source_version}")

    def test_build_metadata_uses_the_same_version_module(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        project = metadata["project"]
        self.assertNotIn("version", project)
        self.assertIn("version", project["dynamic"])
        self.assertEqual(
            metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "llm_hud._version.__version__",
        )


if __name__ == "__main__":
    unittest.main()
