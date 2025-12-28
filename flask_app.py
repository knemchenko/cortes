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
    users = query_db("SELECT user_id, username, full_name, start_count FROM users")
    chats = query_db("SELECT chat_id, chat_title FROM chats")
    activity = query_db("""
        SELECT 
            a.user_id, 
            u.username, 
            a.chat_id, 
            c.chat_title, 
            a.instagram_count, 
            a.youtube_count,
            a.twitter_count,
            a.tiktok_count
        FROM activity a
        LEFT JOIN users u ON a.user_id = u.user_id
        LEFT JOIN chats c ON a.chat_id = c.chat_id
    """)

    # --- Summary Statistics ---

    # Total users
    total_users = query_db("SELECT COUNT(*) FROM users", one=True)[0]

    # Total chats
    total_chats = query_db("SELECT COUNT(*) FROM chats", one=True)[0]

    # Total conversions per platform
    total_conversions = query_db("""
        SELECT
            SUM(instagram_count),
            SUM(youtube_count),
            SUM(twitter_count),
            SUM(tiktok_count)
        FROM activity
    """, one=True)

    # Prepare conversion data for the template
    conversion_data = {
        "instagram": total_conversions[0] or 0,
        "youtube": total_conversions[1] or 0,
        "twitter": total_conversions[2] or 0,
        "tiktok": total_conversions[3] or 0,
    }

    return render_template(
        "index.html",
        users=users,
        chats=chats,
        activity=activity,
        total_users=total_users,
        total_chats=total_chats,
        conversion_data=conversion_data,
    )

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
