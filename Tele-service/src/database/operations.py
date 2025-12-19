"""
Database Operations.
Handles Chat History and Message Logging only.
"""
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class DatabaseOperationsMixin:
    """Mixin containing business-specific database operations."""

    # --- Conversation & Message Operations ---

    async def get_or_create_conversation(
        self, telegram_account_id: str, chat_id: str, chat_name: str = None, customer_data: dict = None
    ) -> int:
        """Upsert conversation with customer details."""
        async with self.conn.execute(
            "SELECT id FROM conversations WHERE telegram_account_id = ? AND chat_id = ?",
            (telegram_account_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            
        timestamp = datetime.now(timezone.utc)
        c_data = customer_data or {}
        
        if row:
            # Update existing
            await self._execute_write(
                """
                UPDATE conversations SET 
                    last_message_at = ?,
                    chat_name = COALESCE(?, chat_name),
                    customer_first_name = COALESCE(?, customer_first_name),
                    customer_username = COALESCE(?, customer_username),
                    customer_phone = COALESCE(?, customer_phone)
                WHERE id = ?
                """,
                (timestamp, chat_name, c_data.get('first_name'), c_data.get('username'), c_data.get('phone'), row[0])
            )
            return row[0]
        else:
            # Insert new
            cursor = await self._execute_write(
                """
                INSERT INTO conversations (
                    telegram_account_id, chat_id, chat_name, last_message_at,
                    customer_first_name, customer_username, customer_phone, customer_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_account_id, chat_id, chat_name, timestamp,
                    c_data.get('first_name'), c_data.get('username'), c_data.get('phone'), str(c_data.get('user_id', ''))
                )
            )
            return cursor

    async def save_message(self, telegram_account_id: str, chat_id: str, message_id: str, direction: str, text: str, status: str = "received") -> Optional[int]:
        try:
            timestamp = datetime.now(timezone.utc)
            cursor = await self._execute_write(
                """
                INSERT INTO messages (telegram_account_id, chat_id, message_id, direction, text, status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_account_id, chat_id, message_id) DO NOTHING
                """,
                (telegram_account_id, str(chat_id), str(message_id), direction, text, status, timestamp)
            )
            return cursor 
        except Exception as e:
            logger.error(f"Error saving message: {e}")
            return None
            
    async def get_messages(self, telegram_account_id: str, chat_id: str, limit: int = 50):
        async with self.conn.execute(
            """
            SELECT * FROM messages 
            WHERE telegram_account_id = ? AND chat_id = ? 
            ORDER BY timestamp DESC LIMIT ?
            """,
            (telegram_account_id, chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows][::-1]

    # --- SESSION MANAGEMENT (The "Brain" of the Worker) ---

    async def save_session(self, account_id: str, api_id: str, api_hash: str, session_string: str, phone: str = "unknown"):
        """
        Save session credentials to SQLite.
        This ensures that even if the server crashes, we remember the user.
        """
        # 1. Ensure table exists (Run this once)
        await self._execute_write(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                account_id TEXT PRIMARY KEY,
                api_id TEXT,
                api_hash TEXT,
                session_string TEXT,
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """, ()
        )
        
        # 2. Upsert (Insert or Update) - Save the key to the lock!
        await self._execute_write(
            """
            INSERT OR REPLACE INTO sessions (account_id, api_id, api_hash, session_string, phone)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, str(api_id), api_hash, session_string, phone)
        )

    async def get_all_sessions(self) -> List[Dict[str, Any]]:
        """Fetch all saved sessions so we can auto-start them on boot."""
        # Ensure table exists so we don't crash on first run
        await self._execute_write(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                account_id TEXT PRIMARY KEY,
                api_id TEXT,
                api_hash TEXT,
                session_string TEXT,
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """, ()
        )
        
        async with self.conn.execute("SELECT * FROM sessions") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
            
    async def delete_session(self, account_id: str):
        """Remove session from DB if user explicitly logs out."""
        await self._execute_write("DELETE FROM sessions WHERE account_id = ?", (account_id,))