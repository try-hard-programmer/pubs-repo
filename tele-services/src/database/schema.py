"""Database Schema Definitions."""

SCHEMA_SQL = """
-- 1. Conversations Table (Stores Chat Metadata)
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_name TEXT,
    customer_user_id TEXT,
    customer_first_name TEXT,
    customer_last_name TEXT,
    customer_username TEXT,
    customer_phone TEXT,
    last_message_at TIMESTAMP,
    UNIQUE(telegram_account_id, chat_id)
);

-- 2. Messages Table (Stores History)
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    direction TEXT CHECK(direction IN ('incoming', 'outgoing')),
    text TEXT,
    status TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(telegram_account_id, chat_id, message_id)
);
"""