from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OfflineReasoningResult:
    text: str
    available: bool
    provider: str
    normalized_error: str | None = None


class LlamaCppOfflineProvider:
    """Optional local GGUF fallback. Never downloads models and never uses Ollama."""

    def __init__(
        self,
        model_path: Path,
        *,
        context_tokens: int = 4096,
        max_output_tokens: int = 512,
        threads: int = 4,
    ) -> None:
        self.model_path = model_path
        self.context_tokens = context_tokens
        self.max_output_tokens = max_output_tokens
        self.threads = threads
        self._model: object | None = None
        self._lock = threading.Lock()

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        if not self.model_path.is_file():
            raise RuntimeError("OFFLINE_MODEL_UNAVAILABLE")
        try:
            from llama_cpp import Llama
        except ImportError as error:
            raise RuntimeError("LLAMA_CPP_BACKEND_UNAVAILABLE") from error
        with self._lock:
            if self._model is None:
                self._model = Llama(
                    model_path=str(self.model_path),
                    n_ctx=self.context_tokens,
                    n_threads=self.threads,
                    verbose=False,
                )
        return self._model

    def generate(self, prompt: str) -> OfflineReasoningResult:
        clean = prompt.strip()
        if not clean:
            return OfflineReasoningResult("", False, "llama.cpp", "EMPTY_PROMPT")
        if len(clean) > 24_000:
            return OfflineReasoningResult("", False, "llama.cpp", "PROMPT_TOO_LARGE")
        try:
            model = self._load()
            result = model(  # type: ignore[operator]
                clean,
                max_tokens=self.max_output_tokens,
                temperature=0.2,
                top_p=0.9,
                echo=False,
            )
            choices = result.get("choices", [])  # type: ignore[union-attr]
            if not choices:
                return OfflineReasoningResult("", False, "llama.cpp", "EMPTY_LOCAL_RESPONSE")
            text = str(choices[0].get("text", "")).strip()
            return OfflineReasoningResult(
                text, bool(text), "llama.cpp", None if text else "EMPTY_LOCAL_RESPONSE"
            )
        except RuntimeError as error:
            return OfflineReasoningResult("", False, "llama.cpp", str(error))
        except (OSError, ValueError) as error:
            return OfflineReasoningResult(
                "", False, "llama.cpp", f"LOCAL_INFERENCE_FAILED:{type(error).__name__}"
            )


class DeterministicOfflineIntelligence:
    """Zero-model fallback for basic local summarization and intent continuity."""

    _sentence = re.compile(r"(?<=[.!?])\s+")

    @classmethod
    def summarize(cls, text: str, max_sentences: int = 3) -> OfflineReasoningResult:
        clean = " ".join(text.strip().split())
        if not clean:
            return OfflineReasoningResult("", False, "deterministic", "EMPTY_TEXT")
        sentences = [item.strip() for item in cls._sentence.split(clean) if item.strip()]
        if not sentences:
            sentences = [clean]
        terms = [token for token in re.findall(r"[a-z0-9]+", clean.casefold()) if len(token) > 3]
        frequency: dict[str, int] = {}
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1

        def score(sentence: str) -> float:
            tokens = re.findall(r"[a-z0-9]+", sentence.casefold())
            return sum(frequency.get(token, 0) for token in tokens) / max(1, len(tokens))

        ranked = sorted(enumerate(sentences), key=lambda item: score(item[1]), reverse=True)
        selected_indexes = sorted(index for index, _ in ranked[: max(1, max_sentences)])
        summary = " ".join(sentences[index] for index in selected_indexes)
        return OfflineReasoningResult(summary, True, "deterministic")
