"""测试夹具：每个用例重建 schema（DROP SCHEMA 不受只追加触发器限制）。

模块级 AsyncEngine 的连接池绑定首次使用的事件循环，必须在用例间 dispose，
否则 pytest-asyncio 每个用例新事件循环会报 "Task attached to a different loop"。
"""
import pytest_asyncio
from sqlalchemy import text

from app.database import engine
from app.init_db import init_db


@pytest_asyncio.fixture
async def db():
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await init_db()
    from sqlalchemy.ext.asyncio import async_sessionmaker
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await session.rollback()
    await engine.dispose()
