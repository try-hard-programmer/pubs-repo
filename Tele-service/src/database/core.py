"""SQLite database core infrastructure."""
import aiosqlite
import asyncio
import logging
from typing import Any, Optional
from src.config.config import config
from src.database.schema import SCHEMA_SQL

logger = logging.getLogger(__name__)

class DatabaseCore:
    """Manage SQLite database connection and write queue."""
    
    def __init__(self):
        self.db_path = config.SQLITE_DB_PATH
        self.conn: Optional[aiosqlite.Connection] = None
        self.write_queue = asyncio.Queue()
        self.writer_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def connect(self) -> None:
        """Connect to SQLite and create tables."""
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        
        # Enable Write-Ahead Logging for concurrency
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        
        await self._create_tables()
        
        self._running = True
        self.writer_task = asyncio.create_task(self._process_write_queue())
        logger.info(f"💾 Database connected: {self.db_path}")
    
    async def close(self) -> None:
        self._running = False
        if self.writer_task:
            await self.write_queue.join()
            self.writer_task.cancel()
        if self.conn:
            await self.conn.close()
    
    async def _create_tables(self) -> None:
        """Execute schema definition."""
        # Split schema by semicolon to execute statement by statement
        statements = SCHEMA_SQL.split(';')
        for stmt in statements:
            if stmt.strip():
                await self.conn.execute(stmt)
        await self.conn.commit()

    async def _process_write_queue(self) -> None:
        """Sequential writer to prevent SQLite locking errors."""
        while self._running:
            try:
                query, args, future = await self.write_queue.get()
                try:
                    cursor = await self.conn.execute(query, args)
                    await self.conn.commit()
                    if not future.done():
                        future.set_result(cursor.lastrowid)
                except Exception as e:
                    if not future.done():
                        future.set_exception(e)
                finally:
                    self.write_queue.task_done()
            except asyncio.CancelledError:
                break

    async def _execute_write(self, query: str, args: tuple) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.write_queue.put((query, args, future))
        return await future