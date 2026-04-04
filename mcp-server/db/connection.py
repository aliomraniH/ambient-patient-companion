"""Database connection pool using asyncpg."""

import logging
import sys
import asyncpg

from config import DATABASE_URL

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        logger.info("Creating asyncpg connection pool")
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=2, max_size=10, command_timeout=30
        )
    return _pool


async def close_pool() -> None:
    """Close the connection pool if it exists."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Connection pool closed")
