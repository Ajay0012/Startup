from pangu.model_runtime import (
    CloudContextSanitizer,
    DeterministicProvider,
    GeminiProvider,
    ModelRouter,
    PrivacyOutcome,
    ProviderHealth,
)


def test_cloud_sanitizer_redacts_tokens_without_retaining_secret() -> None:
    decision = CloudContextSanitizer().sanitize("Authorization: Bearer secret-value")
    assert decision.outcome == PrivacyOutcome.ALLOW_WITH_REDACTION
    assert "secret-value" not in decision.sanitized_content
    assert "token" in decision.redactions


def test_private_key_is_rejected() -> None:
    assert (
        CloudContextSanitizer().sanitize("-----BEGIN PRIVATE KEY-----\nsecret").outcome
        == PrivacyOutcome.REJECT
    )


def test_router_prefers_deterministic_and_missing_gemini_degrades() -> None:
    gemini = GeminiProvider(None)
    assert gemini.health() == ProviderHealth.UNCONFIGURED
    route = ModelRouter(DeterministicProvider(), gemini, CloudContextSanitizer()).route(
        "create folder"
    )
    assert route.provider == "deterministic"
