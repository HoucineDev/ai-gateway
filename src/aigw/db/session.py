from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from aigw.config import get_settings


class Database:
    def __init__(self, url: str | None = None, **engine_kwargs):
        self.url = url or get_settings().database_url
        self.engine: AsyncEngine = create_async_engine(self.url, pool_pre_ping=True, **engine_kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            yield s

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncSession]:
        """Session with an explicit transaction; commits on success, rolls back on error."""
        async with self.sessionmaker() as s:
            async with s.begin():
                yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
