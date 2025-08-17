# simple_poll_reply_check.py
import os
import json
import base64
import asyncio
import logging
import re
import html
from typing import List, Dict, Any, Optional, Set, Tuple

from dotenv import load_dotenv
from bs4 import BeautifulSoup

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError

# ----------------- CONFIG -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
if OWNER_ID and str(OWNER_ID).isdigit():
    OWNER_ID = int(OWNER_ID)

if not BOT_TOKEN or not OWNER_ID:
    logging.critical("BOT_TOKEN or OWNER_ID missing in environment variables.")
    raise SystemExit(1)

EMAILS_FILE = os.getenv("EMAILS_FILE", "emails.json")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("TOKEN_FILE", "token.json")

# fixed simple behavior requested:
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "50"))   # seconds between checks (user asked 30s)
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))         # process 20 emails each poll

# Read-only by default
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# -------------- STATE -------------------
PROCESSED_EMAIL_IDS: Set[str] = set()
MY_EMAIL: Optional[str] = None

# -------------- Gmail auth & helpers ----------------
def authenticate_gmail():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logging.warning("Failed to load token: %s", e)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logging.warning("Failed to refresh token: %s", e)
                creds = None
        if not creds:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                logging.critical("Missing credentials.json (CREDENTIALS_FILE).")
                return None
            except Exception as e:
                logging.critical("OAuth flow error: %s", e)
                return None
            try:
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
            except Exception:
                pass

    if not creds:
        logging.critical("Could not obtain credentials.")
        return None
    return build("gmail", "v1", credentials=creds)

def _decode_base64url(data: str) -> str:
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        try:
            return base64.b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return data

# -------------- persistence ----------------
def load_processed_emails():
    global PROCESSED_EMAIL_IDS
    if not os.path.exists(EMAILS_FILE):
        return
    try:
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            PROCESSED_EMAIL_IDS = {e["id"] for e in data if "id" in e}
            logging.info("Loaded %d processed IDs.", len(PROCESSED_EMAIL_IDS))
    except Exception as e:
        logging.warning("Could not load emails.json: %s", e)

def save_email_record(record: Dict[str, Any]):
    data = []
    if os.path.exists(EMAILS_FILE):
        try:
            with open(EMAILS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    data.append(record)
    try:
        with open(EMAILS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("Failed writing emails.json: %s", e)

def update_email_record(email_id: str, decision: bool):
    if not os.path.exists(EMAILS_FILE):
        return
    try:
        with open(EMAILS_FILE, "r+", encoding="utf-8") as f:
            data = json.load(f)
            for e in data:
                if e.get("id") == email_id:
                    e["needs_response"] = decision
                    break
            f.seek(0); f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("Failed updating emails.json: %s", e)

# -------------- email body parsing -------------
def find_email_parts(parts: List[Dict[str, Any]]) -> Dict[str, str]:
    body = {"plain": "", "html": ""}
    for part in parts:
        if "parts" in part:
            nested = find_email_parts(part["parts"])
            if nested["plain"] and not body["plain"]:
                body["plain"] = nested["plain"]
            if nested["html"] and not body["html"]:
                body["html"] = nested["html"]
        mime = part.get("mimeType", "")
        if mime == "text/plain" and not body["plain"]:
            data = part.get("body", {}).get("data")
            if data:
                body["plain"] = _decode_base64url(data)
        elif mime == "text/html" and not body["html"]:
            data = part.get("body", {}).get("data")
            if data:
                body["html"] = _decode_base64url(data)
        # handle message/rfc822 nested payloads
        if part.get("mimeType") == "message/rfc822":
            nested = part.get("body", {}).get("data")
            if nested:
                try:
                    nested_decoded = _decode_base64url(nested)
                    # try to extract something sensible
                    if not body["plain"]:
                        body["plain"] = nested_decoded
                except Exception:
                    pass
    return body

def extract_latest_reply(payload: Dict[str, Any]) -> Tuple[str, str]:
    body_parts = {"plain": "", "html": ""}
    if "parts" in payload:
        body_parts = find_email_parts(payload["parts"])
    elif "data" in payload.get("body", {}):
        content = _decode_base64url(payload["body"]["data"])
        if payload.get("mimeType") == "text/html":
            body_parts["html"] = content
        else:
            body_parts["plain"] = content

    original_body = body_parts["html"] or body_parts["plain"]
    cleaned = ""

    if body_parts["html"]:
        soup = BeautifulSoup(body_parts["html"], "html.parser")
        for blockquote in soup.find_all("blockquote"):
            txt = blockquote.get_text().lower()
            if "wrote:" in txt or "from:" in txt:
                blockquote.decompose()
        gmail_q = soup.find("div", class_="gmail_quote")
        if gmail_q:
            gmail_q.decompose()
        cleaned = soup.get_text(separator="\n", strip=True)
    else:
        plain = body_parts["plain"]
        if plain:
            parts = re.split(r'\n_+\n|\nOn .* wrote:\n', plain, 1)
            cleaned = parts[0].strip()

    if not cleaned and body_parts["plain"]:
        cleaned = body_parts["plain"].strip()

    return original_body, cleaned or "(No readable content found)"

def format_body_for_telegram(body: str) -> str:
    escaped = html.escape(body)
    return re.sub(r'(https?://[^\s<]+)', r'<a href="\1">link</a>', escaped)

# -------------- replied detection -------------
def get_my_email(service) -> Optional[str]:
    global MY_EMAIL
    if MY_EMAIL:
        return MY_EMAIL
    try:
        profile = service.users().getProfile(userId="me").execute()
        MY_EMAIL = profile.get("emailAddress")
        logging.info("Detected account email: %s", MY_EMAIL)
        return MY_EMAIL
    except Exception as e:
        logging.warning("Could not detect account email: %s", e)
        return None

def is_last_message_from_me(service, thread_id: str, my_email: Optional[str]) -> bool:
    if not thread_id:
        return False
    try:
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs = thread.get("messages", []) or []
        if not msgs:
            return False
        try:
            last = max(msgs, key=lambda m: int(m.get("internalDate", "0")))
        except Exception:
            last = msgs[-1]
        labels = [l.upper() for l in (last.get("labelIds") or [])]
        if "SENT" in labels:
            return True
        headers = last.get("payload", {}).get("headers", []) or []
        from_hdr = next((h["value"] for h in headers if h.get("name", "").lower() == "from"), "") or ""
        if my_email and my_email in from_hdr:
            return True
        return False
    except HttpError as e:
        logging.warning("Gmail thread fetch error for %s: %s", thread_id, e)
        return False
    except Exception as e:
        logging.exception("Unexpected error in last-message check: %s", e)
        return False

# -------------- Telegram bot & simple polling logic -------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def check_new_emails(service):
    """
    Simple: list up to BATCH_SIZE messages from INBOX (newest first),
    for each message check if the last message in the thread is from me.
    If not, fetch full message and send immediately (no queue).
    """
    if not service:
        logging.warning("No Gmail service available.")
        return

    my_email = get_my_email(service)

    try:
        results = service.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=BATCH_SIZE).execute()
        messages = results.get("messages", []) or []
        logging.info("Fetched %d messages (batch size %d).", len(messages), BATCH_SIZE)

        # messages list is newest-first typically; keep that
        for m in messages:
            mid = m.get("id")
            if not mid or mid in PROCESSED_EMAIL_IDS:
                continue

            # quick metadata to get threadId
            try:
                meta = service.users().messages().get(userId="me", id=mid, format="metadata").execute()
            except Exception as e:
                logging.warning("Failed to get metadata for %s: %s", mid, e)
                continue

            thread_id = meta.get("threadId")
            if is_last_message_from_me(service, thread_id, my_email):
                logging.info("Skipping %s because last message in thread %s is from me.", mid, thread_id)
                save_email_record({"id": mid, "subject": "(skipped)", "from": "(skipped)", "original_body": "", "needs_response": False})
                PROCESSED_EMAIL_IDS.add(mid)
                continue

            # fetch full message and send now
            try:
                msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
                payload = msg.get("payload", {}) or {}
                headers = payload.get("headers", []) or []

                subject = next((h["value"] for h in headers if h.get("name", "").lower() == "subject"), "No Subject")
                subject = re.sub(r'^(re:|Re:|RE:)\s*', '', subject)
                sender = next((h["value"] for h in headers if h.get("name", "").lower() == "from"), "Unknown Sender")

                # safety re-check before sending
                if is_last_message_from_me(service, thread_id, my_email):
                    logging.info("Skipping %s at send-time because thread %s now last-message from me.", mid, thread_id)
                    save_email_record({"id": mid, "subject": subject, "from": sender, "original_body": "", "needs_response": False})
                    PROCESSED_EMAIL_IDS.add(mid)
                    continue

                original_body, cleaned = extract_latest_reply(payload)
                processed = format_body_for_telegram(cleaned)
                if len(processed) > 3800:
                    processed = processed[:3800] + "\n\n<b>[Message truncated]</b>"

                message_text = (f"📧 <b>New Email</b>\n\n"
                                f"<b>From:</b> <code>{html.escape(sender)}</code>\n"
                                f"<b>Subject:</b> <code>{html.escape(subject)}</code>\n\n"
                                f"---\n\n{processed}")

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Needs Response", callback_data=f"response:{mid}:yes"),
                        InlineKeyboardButton(text="❌ No Response", callback_data=f"response:{mid}:no"),
                    ]
                ])

                # send immediately (user asked no queue). handle RetryAfter gracefully.
                try:
                    await bot.send_message(chat_id=OWNER_ID, text=message_text, reply_markup=keyboard, parse_mode="HTML")
                except TelegramRetryAfter as tre:
                    retry = int(getattr(tre, "timeout", None) or getattr(tre, "retry_after", 0) or 5)
                    logging.warning("Telegram retry_after %s seconds. Sleeping then retrying once.", retry)
                    await asyncio.sleep(retry + 1)
                    await bot.send_message(chat_id=OWNER_ID, text=message_text, reply_markup=keyboard, parse_mode="HTML")
                except TelegramNetworkError as tn:
                    logging.warning("Telegram network error: %s — sleeping 5s and retrying once.", tn)
                    await asyncio.sleep(5)
                    await bot.send_message(chat_id=OWNER_ID, text=message_text, reply_markup=keyboard, parse_mode="HTML")
                except Exception as sent_e:
                    logging.exception("Unexpected error sending Telegram message for %s: %s", mid, sent_e)
                    # if we failed hard, don't mark processed — retry next run
                    continue

                PROCESSED_EMAIL_IDS.add(mid)
                save_email_record({"id": mid, "subject": subject, "from": sender, "original_body": original_body, "needs_response": None})
                # small throttle to avoid burst
                await asyncio.sleep(0.3)

            except HttpError as he:
                logging.error("Gmail API error fetching message %s: %s", mid, he)
            except Exception as e:
                logging.exception("Unexpected error processing message %s: %s", mid, e)

    except HttpError as e:
        logging.error("Gmail list error: %s", e)
    except Exception as e:
        logging.exception("Unexpected error in check_new_emails: %s", e)

# -------------- callbacks & commands --------------
@dp.callback_query(F.data.startswith("response:"))
async def handle_decision_callback(query: CallbackQuery):
    try:
        _, email_id, decision_str = query.data.split(":")
        decision = (decision_str == "yes")
        update_email_record(email_id, decision)
        response_text = "✅ Marked as 'Needs Response'" if decision else "❌ Marked as 'No Response Needed'"
        orig = getattr(query.message, "html_text", None) or getattr(query.message, "text", "") or ""
        try:
            await query.message.edit_text(text=orig + f"\n\n--- \n<b>Status:</b> {response_text}", parse_mode="HTML")
        except Exception:
            await query.answer(response_text)
        await query.answer("Decision saved")
    except Exception as e:
        logging.exception("Callback handling error: %s", e)
        await query.answer("Error processing")

@dp.message(CommandStart())
async def start_command(message: Message):
    await message.answer("✅ Bot running: checking 20 newest inbox emails every 30s and notifying if last message in thread is from other side.")

# -------------- main loop --------------
async def main():
    service = authenticate_gmail()
    if not service:
        logging.critical("Gmail authentication failed.")
        return

    load_processed_emails()

    # Start dispatcher so callbacks work
    asyncio.create_task(dp.start_polling(bot))

    logging.info("Starting simple poll loop: batch=%s interval=%s", BATCH_SIZE, POLL_INTERVAL)
    try:
        while True:
            await check_new_emails(service)
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        logging.info("Cancelled — exiting.")
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())