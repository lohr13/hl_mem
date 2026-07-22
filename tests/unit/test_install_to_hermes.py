"""Hermes 插件安装脚本的单元测试。"""

from pathlib import Path

import install_to_hermes
import pytest


def test_main_prints_start_and_success_messages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """实际安装应在开始时提示，并在校验成功后打印完成提示。"""
    exit_code = install_to_hermes.main(["--hermes-home", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Installing HL-Mem Hermes plugin" in output
    assert "Installation succeeded" in output
    assert output.index("Installing HL-Mem Hermes plugin") < output.index("Installation succeeded")
