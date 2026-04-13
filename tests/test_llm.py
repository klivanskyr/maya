import pytest

from app.llm import estimate_tokens


def test_estimate_tokens_basic():
    assert estimate_tokens("hello") == 1
    assert estimate_tokens("hello world this is a test") > 1


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 1


def test_estimate_tokens_long_text():
    text = "a" * 400
    assert estimate_tokens(text) == 100
