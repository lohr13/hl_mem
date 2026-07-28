
import httpx
api_key = "sk-sp-...d3f6
sk-sp-...d3f6
sk-sp-...d3f6
sk-sp-...d3f6"
try:
    resp = httpx.post(
        "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "qwen3.7-plus", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
        timeout=15
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        print("SUCCESS!")
    else:
        print(f"Response: {resp.text[:200]}")
except Exception as e:
    print(f"Error: {e}")
