import os
from flask import Flask, render_template
import sqlite3

# Absolute path to the database file
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(APP_DIR, "bot_usage.db")
app = Flask(__name__)

def query_db(query, args=(), one=False):
    """Run a query on the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, args)
    rows = cursor.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows

@app.route("/")
def index():
    """Render the main statistics page."""
    # --- Summary Statistics ---
    total_users = query_db("SELECT COUNT(*) FROM users", one=True)[0]
    total_chats = query_db("SELECT COUNT(*) FROM chats", one=True)[0]
    total_conversions = query_db(
        """
        SELECT
            SUM(instagram_count),
            SUM(youtube_count),
            SUM(twitter_count),
            SUM(tiktok_count)
        FROM activity
    """,
        one=True,
    )

    conversion_data = {
        "instagram": total_conversions[0] or 0,
        "youtube": total_conversions[1] or 0,
        "twitter": total_conversions[2] or 0,
        "tiktok": total_conversions[3] or 0,
    }

    # --- Grouped Data ---
    activity_query = """
        SELECT
            c.chat_id,
            c.chat_title,
            u.user_id,
            u.username,
            u.full_name,
            a.instagram_count,
            a.youtube_count,
            a.twitter_count,
            a.tiktok_count
        FROM activity a
        JOIN users u ON a.user_id = u.user_id
        JOIN chats c ON a.chat_id = c.chat_id
        ORDER BY c.chat_title, u.username
    """
    activity_data = query_db(activity_query)

    # Process data into a nested structure
    chats_data = {}
    for row in activity_data:
        chat_id, chat_title, user_id, username, full_name, insta, yt, tw, tk = row
        if chat_id not in chats_data:
            # If chat title is None (e.g., private chat), create a descriptive title
            final_chat_title = chat_title
            if not final_chat_title:
                final_chat_title = f"Private chat with {full_name or username}"
            chats_data[chat_id] = {"chat_title": final_chat_title, "users": []}

        chats_data[chat_id]["users"].append(
            {
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "instagram": insta,
                "youtube": yt,
                "twitter": tw,
                "tiktok": tk,
            }
        )

    return render_template(
        "index.html",
        total_users=total_users,
        total_chats=total_chats,
        conversion_data=conversion_data,
        chats_data=chats_data,
    )

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
