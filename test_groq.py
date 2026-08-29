"""Smoke-test the Groq key and the model the pipeline actually uses.

    python3 test_groq.py
"""

from openai import OpenAI

from agents_core import PROVIDERS

cfg = PROVIDERS["groq"]
key = input("Paste your Groq API key: ").strip()

client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=30)

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
