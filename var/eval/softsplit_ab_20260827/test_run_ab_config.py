from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

EQUIPMENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EQUIPMENT_DIR))

import run_ab  # noqa: E402
from run_ab import (  # noqa: E402
    BASE_URL,
    MODEL,
    PROVIDER,
    Settings,
    _configure_non_delta_settings,
    _parser,
)


class ConfigureSettingsTests(unittest.TestCase):
    def test_respect_llm_config_flag_is_opt_in(self) -> None:
        self.assertFalse(_parser().parse_args([]).respect_llm_config)
        self.assertTrue(_parser().parse_args(["--respect-llm-config"]).respect_llm_config)

    def test_default_non_delta_run_overrides_loaded_llm_config(self) -> None:
        loaded = Settings(
            llm_provider="zhipu",
            llm_model="glm-5.3-flash",
            llm_base_url="https://open.bigmodel.cn/api/paas/v4",
        )

        settings = _configure_non_delta_settings(
            loaded,
            respect_llm_config=False,
        )

        self.assertEqual(settings.llm_provider, PROVIDER)
        self.assertEqual(settings.llm_model, MODEL)
        self.assertEqual(settings.llm_base_url, BASE_URL)

    def test_respect_flag_preserves_loaded_llm_config(self) -> None:
        loaded = Settings(
            llm_provider="zhipu",
            llm_model="glm-5.3-flash",
            llm_base_url="https://open.bigmodel.cn/api/paas/v4",
        )

        settings = _configure_non_delta_settings(
            loaded,
            respect_llm_config=True,
        )

        self.assertEqual(settings.llm_provider, "zhipu")
        self.assertEqual(settings.llm_model, "glm-5.3-flash")
        self.assertEqual(settings.llm_base_url, "https://open.bigmodel.cn/api/paas/v4")

    def test_main_reports_respected_llm_config(self) -> None:
        loaded = Settings(
            llm_api_key="unit-test-llm-api-key",
            llm_provider="zhipu",
            llm_model="glm-5.3-flash",
            llm_base_url="https://open.bigmodel.cn/api/paas/v4",
        )
        received_settings: list[Settings] = []

        def fake_run_manifest(*args, **kwargs):
            received_settings.append(args[2])
            return {"written": 0}

        stdout = io.StringIO()
        with (
            patch.object(run_ab, "load_settings", return_value=loaded),
            patch.object(run_ab, "run_manifest", side_effect=fake_run_manifest),
            patch.object(sys, "argv", ["run_ab.py", "--respect-llm-config"]),
            redirect_stdout(stdout),
        ):
            self.assertEqual(run_ab.main(), 0)

        self.assertEqual(received_settings[0].llm_provider, "zhipu")
        self.assertEqual(
            stdout.getvalue().splitlines()[0],
            "LLM configuration: provider=zhipu model=glm-5.3-flash base_url=https://open.bigmodel.cn/api/paas/v4",
        )


if __name__ == "__main__":
    unittest.main()
