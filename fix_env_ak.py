#!/usr/bin/env python3
"""从 Hermes 配置同步 LLM API 密钥到项目环境文件。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def main() -> None:
    """从配置读取密钥并更新环境文件。"""
    config_path = Path(os.environ["HERMES_CONFIG_PATH"])
    env_path = Path(os.environ["HL_MEM_ENV_FILE"])

    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    api_key = config["providers"]["dashscope"]["api_key"]
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    replacement = f"LLM_API_KEY={api_key}\n"
    updated_lines = [
        replacement if line.strip().startswith("LLM_API_KEY=") else line
        for line in lines
    ]

    if updated_lines == lines:
        raise RuntimeError("未找到 LLM_API_KEY 配置项")

    env_path.write_text("".join(updated_lines), encoding="utf-8")
    print("LLM_API_KEY 已更新")


if __name__ == "__main__":
    main()
