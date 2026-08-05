from pangu.model_runtime import CircuitBreaker, CircuitState, ModelBudget, StructuredOutputValidator


def test_circuit_breaker_opens_after_qualifying_failures() -> None:
    breaker = CircuitBreaker(2)
    breaker.failure("NETWORK_UNAVAILABLE")
    assert breaker.allow()
    breaker.failure("NETWORK_UNAVAILABLE")
    assert breaker.state == CircuitState.OPEN and not breaker.allow()


def test_budget_refuses_excess_calls() -> None:
    budget = ModelBudget(max_calls=1)
    budget.record()
    assert not budget.permit()


def test_structured_output_accepts_fenced_json_and_rejects_missing_keys() -> None:
    validator = StructuredOutputValidator()
    assert validator.validate('```json\n{"intent":"open"}\n```', {"intent"})["intent"] == "open"
    try:
        validator.validate("{}", {"intent"})
    except ValueError as error:
        assert str(error) == "STRUCTURED_OUTPUT_INVALID"
    else:
        raise AssertionError("missing schema key must fail")
