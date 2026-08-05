# Gemini setup

Set `GEMINI_API_KEY` only in the root `.env` or process environment. The API key is not returned by API, CLI, health, or errors. `GoogleGenAITransport` loads the official SDK only during an actual configured request; tests use `FakeGeminiTransport` and make no network requests.
