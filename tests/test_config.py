import os

import pytest


def test_config_loads():
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    # Re-import to pick up env var
    from app.config import Settings
    s = Settings()
    assert s.telegram_bot_token == "test-token"
    assert s.ollama_model == "llama3.2"
    assert s.max_context_tokens == 4096
    assert s.compaction_threshold == 0.75
    assert s.conversation_timeout_minutes == 30
