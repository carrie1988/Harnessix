from src.agent_utils import redact_api_key

assert redact_api_key("api-live-123456789") == "api...6789"
assert redact_api_key("tiny") == "****"
assert "123456789" not in redact_api_key("api-live-123456789")
