"""Fixtures for dashboard tests."""

import os
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app, ENV_FILE, ALL_KEYS, SECRET_KEYS, SERVER_MAP

from httpx import AsyncClient, ASGITransport


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env():
    """Ensure a clean .env and isolated os.environ for each test."""
    backup = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    ENV_FILE.write_text("")
    saved = {k: os.environ.pop(k, None) for k in ALL_KEYS}
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]
    ENV_FILE.write_text(backup)


@pytest.fixture
async def client():
    """Async HTTP client wired to the FastAPI app (no real server needed)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
