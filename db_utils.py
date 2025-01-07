import sqlite3

DB_FILE = "bot_usage.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Table for users
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        start_count INTEGER DEFAULT 0
    )
    """)

    # Table for chats
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        chat_title TEXT
    )
    """)

    # Table for user activity
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity (
        user_id INTEGER,
        chat_id INTEGER,
        instagram_count INTEGER DEFAULT 0,
        youtube_count INTEGER DEFAULT 0,
        twitter_count INTEGER DEFAULT 0,
        tiktok_count INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    )
    """)

    conn.commit()
    conn.close()

def log_user_start(user_id, username, full_name):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO users (user_id, username, full_name, start_count)
    VALUES (?, ?, ?, 1)
    ON CONFLICT(user_id) DO UPDATE SET start_count = start_count + 1
    """, (user_id, username, full_name))
    conn.commit()
    conn.close()

def log_chat_usage(chat_id, chat_title):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO chats (chat_id, chat_title)
    VALUES (?, ?)
    ON CONFLICT(chat_id) DO NOTHING
    """, (chat_id, chat_title))
    conn.commit()
    conn.close()

def log_activity(user_id, chat_id, instagram=False, youtube=False, twitter=False, tiktok=False):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO activity (user_id, chat_id, instagram_count, youtube_count, twitter_count, tiktok_count)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id, chat_id) DO UPDATE SET
        instagram_count = instagram_count + ?,
        youtube_count = youtube_count + ?
    """, (user_id, chat_id, int(instagram), int(youtube), int(instagram), int(youtube), int(twitter), int(tiktok)))
    conn.commit()
    conn.close()
