import os
import re
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


values = dotenv_values(Path.cwd() / ".env")

api_key = (
    os.getenv("GEMINI_API_KEY")
    or values.get("GEMINI_API_KEY")
    or ""
).strip()

model = (
    os.getenv("GEMINI_FAST_MODEL")
    or values.get("GEMINI_FAST_MODEL")
    or "gemini-3.5-flash-lite"
).strip()

if not api_key:
    raise SystemExit("GEMINI_API_KEY is missing.")


def sanitize(value: object) -> str:
    text = str(value).replace(api_key, "[REDACTED]")
    text = re.sub(
        r"AIza[0-9A-Za-z_-]{20,}",
        "[REDACTED_API_KEY]",
        text,
    )
    return text[:1200]


base = "https://generativelanguage.googleapis.com/v1beta"
headers = {
    "x-goog-api-key": api_key,
    "Content-Type": "application/json",
}

with httpx.Client(timeout=120, trust_env=False) as client:
    print("MODEL:", model)

    metadata = client.get(
        f"{base}/models/{model}",
        headers=headers,
    )

    print()
    print("MODEL GET STATUS:", metadata.status_code)
    print("MODEL GET TYPE:", metadata.headers.get("content-type"))
    print("MODEL GET BODY:", sanitize(metadata.text))

    started = time.perf_counter()

    generation = client.post(
        f"{base}/models/{model}:generateContent",
        headers=headers,
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Reply with exactly OK."}
                    ],
                }
            ]
        },
    )

    elapsed = time.perf_counter() - started

    print()
    print("GENERATE STATUS:", generation.status_code)
    print("GENERATE TYPE:", generation.headers.get("content-type"))
    print("GENERATE SECONDS:", round(elapsed, 2))
    print("GENERATE BODY:", sanitize(generation.text))
