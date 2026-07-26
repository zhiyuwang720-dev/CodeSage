"""Shared test fixtures and configuration."""

import os
import sys

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def pytest_configure(config):
    """Auto-enable asyncio for all async test functions."""
    config.option.asyncio_mode = "auto"
    config.option.asyncio_default_fixture_loop_scope = "function"


@pytest.fixture(scope="session")
def deepseek_api_key():
    """DeepSeek API key from environment variable."""
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        pytest.skip("DEEPSEEK_API_KEY not set — skipping integration tests")
    return key
