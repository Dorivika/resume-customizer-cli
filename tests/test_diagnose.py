from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from tailor_resume import app, diagnose_route_config, find_latex_engine


class DiagnoseCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_diagnose_accepts_sample_inputs_without_calling_provider(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "diagnose",
                "--resume",
                "samples/resume.md",
                "--job",
                "samples/job_description.txt",
                "--route",
                "openrouter-free",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("resume input", result.output)
        self.assertIn("Provider Route", result.output)

    def test_diagnose_strict_fails_for_missing_input(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "diagnose",
                "--resume",
                "missing-resume.md",
                "--route",
                "openrouter-free",
                "--strict",
            ],
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("File not found", result.output)

    def test_route_config_reports_configured_openrouter_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")
            old_value = os.environ.get("OPENROUTER_API_KEY")
            os.environ.pop("OPENROUTER_API_KEY", None)
            try:
                from tailor_resume import load_dotenv

                load_dotenv(env_path)
                rows = diagnose_route_config("openrouter-free", "OPENROUTER_API_KEY")
            finally:
                if old_value is None:
                    os.environ.pop("OPENROUTER_API_KEY", None)
                else:
                    os.environ["OPENROUTER_API_KEY"] = old_value

        self.assertIn(("openrouter-free key", True, "configured"), rows)

    def test_find_latex_engine_returns_none_for_unknown_engine(self) -> None:
        self.assertIsNone(find_latex_engine("definitely-not-a-latex-engine"))


if __name__ == "__main__":
    unittest.main()
