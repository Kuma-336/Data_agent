import pytest
from pydantic import ValidationError
from app.config import Settings

TEST_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def valid_settings(**overrides):
    values = {"app_secret_key": "secret", "field_encryption_key": TEST_FERNET_KEY, "llm_api_key": "key", "llm_base_url": "https://example.test", "llm_model": "model"}
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("overrides", [{"llm_api_key": ""}, {"field_encryption_key": "replace-with-a-fernet-key"}, {"field_encryption_key": "invalid"}])
def test_invalid_runtime_configuration_fails_fast(overrides):
    with pytest.raises(ValidationError):
        valid_settings(**overrides)


def test_valid_runtime_configuration_is_accepted():
    assert valid_settings().llm_model == "model"
