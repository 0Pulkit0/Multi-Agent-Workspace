"""Smoke-test the Gemini key and the model the pipeline actually uses.

    python3 test_gemini.py
"""

from openai import OpenAI

from agents_core import PROVIDERS

cfg = PROVIDERS["gemini"]
key = input("Paste your Gemini API key: ").strip()

client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=30)

# Pinned to the same model ID as the pipeline, so a passing test means the
# pipeline will work. (This script used to hard-code gemini-2.0-flash, which
# has since been shut down, so it could fail while the pipeline was fine.)
print("model:", cfg["model"])
try:
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role": "user", "content": "Say hello in one word."}],
    )
    print("SUCCESS:", resp.choices[0].message.content)
except Exception as e:
    print("FAILED:", type(e).__name__)
    print(e)
