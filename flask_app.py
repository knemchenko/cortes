from flask import Flask, render_template
import sqlite3

DB_FILE = "bot_usage.db"
app = Flask(__name__)

def query_db(query, args=()):
    """Run a query on the SQLite database and return results as dictionaries."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # This allows fetching rows as dictionaries
    cursor = conn.cursor()
    cursor.execute(query, args)
    rows = cursor.fetchall()
    conn.close()
    return rows

@app.route("/")
def index():
    """Render the main statistics page with improved data fetching."""
    # Fetch users and chats separately for display
    users = query_db("SELECT user_id, username, full_name, start_count FROM users")
    chats = query_db("SELECT chat_id, chat_title FROM chats")

    # Combined query for activity using JOIN
    activity = query_db("""
        SELECT
            u.username,
            c.chat_title,
            a.instagram_count,
            a.youtube_count,
            a.twitter_count,
            a.tiktok_count,
            a.threads_count
        FROM activity a
        LEFT JOIN users u ON a.user_id = u.user_id
        LEFT JOIN chats c ON a.chat_id = c.chat_id
        ORDER BY u.username, c.chat_title
    """)

    # Fetch cached videos correctly handling the url -> file_id mapping
    cached_videos = query_db("SELECT url, file_id FROM video_cache")
    cache_count = len(cached_videos)

    return render_template(
        "index.html",
        users=users,
        chats=chats,
        activity=activity,
        cached_videos=cached_videos,
        cache_count=cache_count
    )

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)