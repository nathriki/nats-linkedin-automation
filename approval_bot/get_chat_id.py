"""One-off helper: find your Telegram chat ID.

1. Message your bot anything at all (search it by the username you gave
   @BotFather, open the chat, send "hi").
2. Run this script -- it reads TELEGRAM_BOT_TOKEN from .env and prints the
   chat ID from your most recent message.
3. Copy that number into TELEGRAM_CHAT_ID in .env.

Run: .venv/Scripts/python.exe approval_bot/get_chat_id.py
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def main():
    if not BOT_TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN in .env -- create a bot via @BotFather first.", file=sys.stderr)
        sys.exit(1)

    resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        print(f"Telegram API error: {data}", file=sys.stderr)
        sys.exit(1)

    updates = data.get("result", [])
    if not updates:
        print("No messages found yet. Send your bot a message first, then re-run this.", file=sys.stderr)
        sys.exit(1)

    latest = updates[-1]
    message = latest.get("message") or latest.get("channel_post")
    if not message:
        print(f"Unexpected update shape, no message found: {latest}", file=sys.stderr)
        sys.exit(1)

    chat = message["chat"]
    print(f"Chat ID: {chat['id']}")
    print(f"Chat type: {chat.get('type')}, name/title: {chat.get('first_name') or chat.get('title')}")
    print("\nCopy the Chat ID above into TELEGRAM_CHAT_ID in .env.")


if __name__ == "__main__":
    main()
