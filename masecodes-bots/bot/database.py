import aiosqlite
import asyncio
from typing import Any


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._conn.commit()
        await self._init_tables()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _init_tables(self):
        async with self._lock:
            await self._conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS blog_posts (
                id INTEGER PRIMARY KEY,
                guid TEXT UNIQUE,
                title TEXT,
                url TEXT,
                published INTEGER,
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS blog_subscribers (
                id INTEGER PRIMARY KEY,
                user_id INTEGER UNIQUE,
                subscribed_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                moderator_id INTEGER,
                reason TEXT,
                timestamp INTEGER
            );

            CREATE TABLE IF NOT EXISTS moderation_actions (
                id INTEGER PRIMARY KEY,
                action TEXT,
                user_id INTEGER,
                moderator_id INTEGER,
                reason TEXT,
                duration INTEGER,
                timestamp INTEGER
            );

            CREATE TABLE IF NOT EXISTS temporary_bans (
                id INTEGER PRIMARY KEY,
                user_id INTEGER UNIQUE,
                moderator_id INTEGER,
                reason TEXT,
                expires_at INTEGER,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS appeals (
                id INTEGER PRIMARY KEY,
                discord_user_id INTEGER,
                username TEXT,
                submitted_at INTEGER,
                reason TEXT,
                explanation TEXT,
                additional_info TEXT,
                status TEXT,
                moderator_id INTEGER,
                decision TEXT,
                decision_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS github_events (
                id INTEGER PRIMARY KEY,
                repo TEXT,
                event_id TEXT UNIQUE,
                type TEXT,
                payload TEXT,
                timestamp INTEGER
            );
            """
            )
            await self._conn.commit()

    async def execute(self, query: str, params: tuple | None = None) -> Any:
        async with self._lock:
            cursor = await self._conn.execute(query, params or ())
            await self._conn.commit()
            return cursor

    async def fetchall(self, query: str, params: tuple | None = None):
        async with self._lock:
            cursor = await self._conn.execute(query, params or ())
            rows = await cursor.fetchall()
            return rows

    async def fetchone(self, query: str, params: tuple | None = None):
        async with self._lock:
            cursor = await self._conn.execute(query, params or ())
            row = await cursor.fetchone()
            return row
