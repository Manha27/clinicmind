import pytest
from google.genai import errors

from app import llm
from app.config import settings


class FakeUsage:
    prompt_token_count = 5
    candidates_token_count = 3
    thoughts_token_count = None


class FakeResponse:
    text = "hello"
    usage_metadata = FakeUsage()


def overloaded():
    return errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})


@pytest.fixture
def setup(monkeypatch):
    calls, logs = [], []

    def install(failing, fallback="backup", error=overloaded):
        class Models:
            def generate_content(self, model, contents, config):
                calls.append(model)
                if model in failing:
                    raise error()
                return FakeResponse()

        class Client:
            models = Models()

        monkeypatch.setattr(settings, "llm_model", "primary")
        monkeypatch.setattr(settings, "llm_fallback_model", fallback)
        monkeypatch.setattr(llm, "get_client", lambda: Client())
        monkeypatch.setattr(llm, "log_call", lambda **kw: logs.append(kw))
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)  # no real waiting

    return install, calls, logs


def test_fallback_model_is_used_when_primary_is_down(setup):
    install, calls, logs = setup
    install(failing={"primary"})

    assert llm.generate("hi", purpose="test") == "hello"
    # primary: first try + 1 quick retry, then one call to the backup
    assert calls == ["primary", "primary", "backup"]
    assert [(entry["model"], entry.get("ok", True)) for entry in logs] == [
        ("primary", False),
        ("backup", True),
    ]


def test_primary_is_used_alone_when_healthy(setup):
    install, calls, logs = setup
    install(failing=set())

    assert llm.generate("hi", purpose="test") == "hello"
    assert calls == ["primary"]
    assert [(entry["model"], entry.get("ok", True)) for entry in logs] == [("primary", True)]


def test_all_models_down_raises_llm_unavailable(setup):
    install, calls, logs = setup
    install(failing={"primary", "backup"})

    with pytest.raises(llm.LLMUnavailable):
        llm.generate("hi", purpose="test")
    assert [entry.get("ok", True) for entry in logs] == [False, False]


def test_without_fallback_it_retries_the_primary_twice(setup):
    install, calls, logs = setup
    install(failing={"primary"}, fallback="")

    with pytest.raises(llm.LLMUnavailable):
        llm.generate("hi", purpose="test")
    assert calls == ["primary", "primary", "primary"]  # first try + 2 retries


def test_a_bad_request_is_not_retried_or_sent_to_the_fallback(setup):
    install, calls, logs = setup

    def bad_request():
        return errors.ClientError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})

    install(failing={"primary"}, error=bad_request)
    with pytest.raises(errors.ClientError):
        llm.generate("hi", purpose="test")
    assert calls == ["primary"]
