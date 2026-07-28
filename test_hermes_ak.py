#!/usr/bin/env python3
"""验证 Hermes 使用的 LLM API 密钥。"""

from __future__ import annotations

import os

import httpx


def main() -> None:
    """调用兼容接口验证环境变量中的 API 密钥。"""
    api_key = os.environ["LLM_API_KEY"]
    api_url = os.environ["LLM_API_URL"]
    model = os.environ["LLM_MODEL"]
    timeout = float(os.environ["LLM_TIMEOUT"])

    response = httpx.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    print("API 密钥验证成功")


if __name__ == "__main__":
    main()
