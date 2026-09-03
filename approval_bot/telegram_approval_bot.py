"""Telegram approval bot: post.pending_review -> human decision -> post.approved.

Architecturally different from every other stage in this pipeline: it's a
reactive, always-on service (must be able to receive a button click at any
time -- Next Step 0a's "approval timeout: intentionally none" means a
pending post can sit for hours or days), not a periodic batch script like
the scraper/scorer/drafter/classifier.

Design choice worth noting: the underlying NATS message is acked as soon as
it's been delivered to Telegram, NOT held unacked while waiting for a human
decision. JetStream's default ack-wait is far too short for human-timescale
review, and holding a message unacked for hours would trigger redelivery
storms. Instead, the pending item's full content is stored in a NATS KV
bucket (pending_approvals, keyed by news_id) -- the same "check a store"
pattern used everywhere else in this pipeline -- and the approve/reject
decision operates against that KV entry, not the original NATS message. The
original message stays in the POSTS stream's audit trail regardless (LIMITS
retention, ack status doesn't delete it).

Run: .venv/Scripts/python.exe approval_bot/telegram_approval_bot.py
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import nats
from dotenv import load_dotenv
from nats.js.errors import BucketNotFoundError, KeyNotFoundError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from approval_bot.kill_switch import is_paused, record_flag, set_paused  # noqa: E402
from schema.messages import POST_APPROVED, POST_PENDING_REVIEW, ClassifiedPost  # noqa: E402

load_dotenv()

# Without this, an exception raised inside a handler is caught by PTB's
# default error handling and logged via the `logging` module -- which, with
# no handler configured, goes nowhere. Silent handler failures with zero
# trace were the actual symptom that led here: a live /status command
# produced no reply and no visible error anywhere.
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

NATS_URL = "nats://localhost:4222"
CONSUMER_DURABLE_NAME = "approval-bot-pending-review"
PENDING_BUCKET = "pending_approvals"


async def get_or_create_pending_kv(js):
    try:
        return await js.key_value(PENDING_BUCKET)
    except BucketNotFoundError:
        return await js.create_key_value(bucket=PENDING_BUCKET)


# --- Pure / lightly-testable pieces -----------------------------------


def format_pending_message(item: ClassifiedPost) -> str:
    return (
        f"⚠️ Needs review\n\n"
        f"{item.draft_text}\n\n"
        f"Source: {item.source_link}\n"
        f"Reason: {item.verdict_reason}"
    )


def build_approval_keyboard(news_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{news_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{news_id}"),
            ]
        ]
    )


def parse_callback_data(data: str) -> tuple[str, str]:
    action, news_id = data.split(":", 1)
    return action, news_id


# --- Business logic (testable with a mocked kv/js, no Telegram objects) --


async def load_pending(kv, news_id: str) -> Optional[ClassifiedPost]:
    try:
        entry = await kv.get(news_id)
    except KeyNotFoundError:
        return None
    return ClassifiedPost.model_validate_json(entry.value)


async def store_pending(kv, item: ClassifiedPost) -> None:
    await kv.put(item.news_id, item.model_dump_json().encode())


async def delete_pending(kv, news_id: str) -> None:
    try:
        await kv.delete(news_id)
    except KeyNotFoundError:
        pass


async def process_approval_decision(action: str, news_id: str, kv, js) -> str:
    """Core approve/reject logic. Returns the text to show the reviewer.
    Publishing to post.approved only happens on "approve"."""
    item = await load_pending(kv, news_id)
    if item is None:
        return "(already handled or expired)"

    if action == "approve":
        approved = item.model_copy(update={"reviewed_by": "human"})
        await js.publish(POST_APPROVED, approved.model_dump_json().encode())
        await delete_pending(kv, news_id)
        return f"✅ Approved\n\n{item.draft_text}"

    if action == "reject":
        await delete_pending(kv, news_id)
        return f"❌ Rejected\n\n{item.draft_text}"

    return f"Unknown action: {action}"


# --- Telegram / NATS glue ------------------------------------------------


async def deliver_pending_item(bot, kv, item: ClassifiedPost) -> None:
    await store_pending(kv, item)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=format_pending_message(item),
        reply_markup=build_approval_keyboard(item.news_id),
    )


def make_nats_callback(bot, kv):
    async def on_pending_review(msg):
        item = ClassifiedPost.model_validate_json(msg.data)
        await deliver_pending_item(bot, kv, item)
        await msg.ack()  # ack on delivery, not on human decision -- see module docstring

    return on_pending_review


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, news_id = parse_callback_data(query.data)
    kv = context.bot_data["pending_kv"]
    js = context.bot_data["js"]
    response_text = await process_approval_decision(action, news_id, kv, js)
    await query.edit_message_text(text=response_text)


async def flag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /flag <news_id> [reason]")
        return
    news_id = context.args[0]
    reason = " ".join(context.args[1:]) or "(no reason given)"
    js = context.bot_data["js"]
    triggered = await record_flag(js, news_id, reason)
    if triggered:
        await update.message.reply_text(
            "\U0001f6d1 Kill-switch triggered: 2+ flagged posts within 24h. "
            "Auto-posting is now PAUSED until you run /unpause.\n\n"
            "Incident response: check the flagged post(s) on LinkedIn, delete/edit as needed, "
            "and note what went wrong before resuming."
        )
    else:
        await update.message.reply_text(f"Flagged {news_id}: {reason}")


async def unpause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    js = context.bot_data["js"]
    await set_paused(js, False)
    await update.message.reply_text("Auto-posting resumed.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    js = context.bot_data["js"]
    paused = await is_paused(js)
    await update.message.reply_text(f"Poster paused: {paused}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


async def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env.", file=sys.stderr)
        sys.exit(1)

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    kv = await get_or_create_pending_kv(js)

    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["js"] = js
    application.bot_data["pending_kv"] = kv
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("flag", flag_command))
    application.add_handler(CommandHandler("unpause", unpause_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_error_handler(on_error)

    # Deliberately just one durable subscription, not a separate "catch-up"
    # sweep: a JetStream durable consumer remembers its delivery position
    # server-side by durable name, so re-subscribing with the SAME name after
    # a restart already delivers backlog + new messages correctly. An earlier
    # version of this file used a second, throwaway "-catchup" consumer for
    # the backlog sweep -- that consumer had no persisted position of its own,
    # so it defaulted to redelivering the ENTIRE stream history on every
    # restart (confirmed live: it re-sent an already-approved test post after
    # a restart). Removed rather than patched.
    cb = make_nats_callback(application.bot, kv)
    await js.subscribe(POST_PENDING_REVIEW, cb=cb, durable=CONSUMER_DURABLE_NAME, stream="POSTS", manual_ack=True)

    await application.initialize()
    # start_polling() BEFORE start() -- matches the documented manual-lifecycle
    # order exactly. Had these reversed initially: Telegram's getUpdates showed
    # the bot successfully fetching (and advancing past) queued commands with
    # zero trace of them reaching any handler or the error handler -- the
    # fetch loop was running, but nothing was consuming what it produced yet.
    # allowed_updates=ALL_TYPES also matters: without it, command messages
    # didn't reach handlers even though button callback_query updates did.
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await application.start()
    print("Approval bot running. Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
