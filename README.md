# Cortes: Telegram Bot for Convenient Video Sharing

## Description

Cortes Bot allows you to easily download videos from **Instagram Reels** and **YouTube Shorts**. It automatically processes links, converting them into downloadable video files that you can save or share.

### Key Features:

- Download videos from Instagram Reels.
- Download YouTube Shorts up to 20 MB in size.
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

### 4. Run the Bot Locally

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

## Requirements

- Python 3.8+
- Telegram Bot API Token
- Stable Internet Connection

---
