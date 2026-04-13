import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from app.models import User


@pytest.fixture
def make_user():
    def _make(tokens_used=0, token_quota=50000, hours_until_reset=24):
        user = MagicMock(spec=User)
        user.id = 1
        user.tokens_used = tokens_used
        user.token_quota = token_quota
        user.quota_reset_at = datetime.utcnow() + timedelta(hours=hours_until_reset)
        return user
    return _make


def test_user_under_quota(make_user):
    user = make_user(tokens_used=1000, token_quota=50000)
    assert user.tokens_used < user.token_quota


def test_user_over_quota(make_user):
    user = make_user(tokens_used=50001, token_quota=50000)
    assert user.tokens_used >= user.token_quota


def test_user_quota_reset_needed(make_user):
    user = make_user(tokens_used=50000, hours_until_reset=-1)
    assert datetime.utcnow() >= user.quota_reset_at
