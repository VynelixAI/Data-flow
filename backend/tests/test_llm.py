from app.agent.llm import chat_configuration_error, parse_qwen_tool_calls, provider, resolve_provider


def test_qwen_tool_calls_use_the_openai_function_shape():
    text = (
        "I will inspect both paths.\n"
        '<tool_call>\n{"name": "inspect_uc_object", "arguments": {"name": "/Volumes/workspace/default/customervolume/orders.csv"}}\n</tool_call>'
        '<tool_call>\n{"name": "search_catalog", "arguments": {"query": "orders"}}\n</tool_call>'
    )
    visible, calls = parse_qwen_tool_calls(text)
    assert visible == "I will inspect both paths."
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "inspect_uc_object"
    assert '"/Volumes/workspace/default/customervolume/orders.csv"' in calls[0]["function"]["arguments"]
    assert calls[1]["function"]["name"] == "search_catalog"
    assert calls[0]["id"] != calls[1]["id"]


def test_plain_qwen_text_has_no_tool_call():
    visible, calls = parse_qwen_tool_calls("The destination is missing.")
    assert visible == "The destination is missing."
    assert calls == []


def test_openai_provider_still_requires_the_api_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    error = chat_configuration_error()
    assert error is not None
    assert error.code == "openai_not_configured"
    assert provider() == "openai"


def test_qwen_provider_does_not_require_the_openai_key(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("LOCAL_MODEL_PATH", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert provider() == "qwen"
    assert chat_configuration_error() is None


def test_a_chat_choice_overrides_the_server_default(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LOCAL_MODEL_PATH", str(tmp_path))
    assert resolve_provider(None) == "openai"
    assert resolve_provider("qwen") == "qwen"
    assert resolve_provider("local") == "qwen"
    assert chat_configuration_error("qwen") is None
    assert chat_configuration_error("openai") is None


def test_qwen_provider_reports_a_missing_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("LOCAL_MODEL_PATH", str(tmp_path / "missing"))
    error = chat_configuration_error()
    assert error is not None
    assert error.code == "local_model_missing"
