"""
Stage 1: quick OpenAI-compatible API test.

Usage:
  set OPENAI_API_KEY=sk-...
  python scripts/openai_quickstart.py

Optional:
  set OPENAI_BASE_URL=http://localhost:8080/v1
  set OPENAI_MODEL=gpt-4o-mini
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request


BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def post_chat(stream: bool = False) -> None:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a concise AI engineering assistant."},
            {"role": "user", "content": "用三句话解释 LLM Gateway 为什么需要模型路由、fallback 和成本统计。"},
        ],
        "temperature": 0.2,
        "stream": stream,
    }
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "X-User-Id": "demo",
        },
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=60) as response:
        print("status:", response.status)
        if stream:
            for raw in response:
                print(raw.decode("utf-8", errors="replace").rstrip())
        else:
            payload = json.loads(response.read().decode("utf-8"))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("elapsed_seconds:", round(time.time() - started, 3))


if __name__ == "__main__":
    if not API_KEY and "localhost" not in BASE_URL and "127.0.0.1" not in BASE_URL:
        print("OPENAI_API_KEY is required when calling a remote provider.", file=sys.stderr)
        sys.exit(1)
    post_chat(stream="--stream" in sys.argv)
