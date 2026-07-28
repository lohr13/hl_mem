#!/usr/bin/env python3
"""Fix hl_mem .env LLM_API_KEY using the working AK from Hermes config."""
import yaml

# Read Hermes config to get the correct AK
with open(r"C:\Users\Administrator\AppData\Local\hermes\config.yaml") as f:
    config = yaml.safe_load(f)

correct_ak = config["providers"]["dashscope"]["api_key"]
print("Correct AK from Hermes: " + correct_ak[:10] + "..." + correct_ak[-4:])

# Read hl_mem .env
env_path = r"D:\workspace\hl_agent\hl_mem\.env"
with open(env_path, "r") as f:
    lines = f.readlines()

# Find and replace LLM_API_KEY line
new_lines = []
replaced = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("LLM_API_KEY=***        new_line = "LLM_API_KEY=***        new_lines.append(new_line)
        replaced = True
    else:
        new_lines.append(line)

if not replaced:
    print("WARNING: LLM_API_KEY line not found!")

# Write back
with open(env_path, "w") as f:
    f.writelines(new_lines)

print("Updated .env successfully")

# Verify
with open(env_path, "r") as f:
    for line in f:
        stripped = line.strip()
        if stripped.startswith("LLM_API_KEY=***            print("Verified: " + stripped[:20] + "..." + stripped[-10:])
            break

# Test the API
import httpx
try:
    resp = httpx.post(
        "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + correct_ak},
        json={"model": "qwen3.7-plus", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
        timeout=15
    )
    print("\nAPI test: " + str(resp.status_code))
    if resp.status_code == 200:
        print("SUCCESS! AK is now valid.")
    else:
        print("Response: " + resp.text[:200])
except Exception as e:
    print("API test error: " + str(e))
