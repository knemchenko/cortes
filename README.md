# Cortes: Telegram Bot for Convenient Video Sharing

## Description

Cortes Bot allows you to easily download videos from **Instagram Reels**, **YouTube Shorts** and **Twitter Videos*. It automatically processes links, converting them into downloadable video files that you can save or share.

### Key Features:

- Download videos from Instagram Reels.
- Download YouTube Shorts up to 20 MB in size.
- Download videos from Twitter.
- Automatically delete the original message with the link after processing.
- Display information about the sender and the source of the video.
- Supports group chats (requires admin permissions).

---

## How to Run the Bot

### 1. Clone the Repository

```bash
git clone <repository_link>
cd <folder_name>
```

### 2. Install Dependencies

Make sure you have Python 3.8+ and `pip` installed.

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the root directory and add the following variables:

```env
TELEGRAM_BOT_TOKEN=<your_bot_token>
TELEGRAM_ADMIN_ID=<your_telegram_id>
```

### 4. Create SQLlite DB
 ```bash
   python db_utils.py
   ```

### 5. Run the Bot Locally to try

```bash
python <script_name>.py
```

---

## How to Set Up as a Service

### 1. Create a systemd Service

Create a file at `/etc/systemd/system/cortes.service`:

```ini
[Unit]
Description=Telegram Bot for Instagram Reels and YouTube Shorts
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/your/script/<script_name>.py
WorkingDirectory=/path/to/your/script
Environment="PYTHONUNBUFFERED=1"
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=5s
MemoryLimit=100M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
```

### 2. Enable the Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable cortes.service
sudo systemctl start cortes.service
```

### 3. Check the Service Status

```bash
sudo systemctl status cortes.service
```

To view logs:

```bash
journalctl -u cortes.service -f
```

---
### Flask Server Setup

The bot includes a Flask web application for monitoring usage statistics.

1. **Install Flask:**
   Make sure Flask is installed by running:
   ```bash
   pip install flask
   ```

2. **Run the Flask app:**
   Start the Flask application with:
   ```bash
   python flask_app.py
   ```

3. **Access the dashboard:**
   Open your browser and navigate to:
   ```
   http://localhost:5000
   ```
   The dashboard provides an overview of:
   - User activity (counts for Instagram, YouTube, Twitter, TikTok).
   - Chat usage statistics.

4. **Deploy as a service:**
   You can also run the Flask app as a service for continuous monitoring:
   - Create a systemd service file (e.g., `/etc/systemd/system/flask-server.service`):
     ```
     [Unit]
     Description=Flask Server for Bot Statistics
     After=network.target

     [Service]
     ExecStart=/usr/bin/python3 /path/to/flask_app.py
     WorkingDirectory=/path/to
     Environment="PYTHONUNBUFFERED=1"
     StandardOutput=journal
     StandardError=journal
     Restart=always

     [Install]
     WantedBy=multi-user.target
     ```
   - Enable and start the service:
     ```bash
     sudo systemctl daemon-reload
     sudo systemctl enable flask-server
     sudo systemctl start flask-server
     ```

---
## Requirements

- Python 3.8+
- Telegram Bot API Token
- Flask
- Stable Internet Connection

---
