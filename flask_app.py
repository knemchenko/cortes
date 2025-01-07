from flask import Flask, render_template
import sqlite3

DB_FILE = "bot_usage.db"
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
            a.twitter_count
        FROM activity a
        LEFT JOIN users u ON a.user_id = u.user_id
        LEFT JOIN chats c ON a.chat_id = c.chat_id
    """)

    return render_template(
        "index.html",
        users=users,
        chats=chats,
        activity=activity
    )

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
