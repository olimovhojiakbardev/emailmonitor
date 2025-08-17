import os
import json
import base64
import asyncio
import logging
import re
import html
from typing import List, Dict, Any, Optional, Set, Tuple

# --- AI DEPENDENCY ---
import google.generativeai as genai

# Using python-dotenv to load environment variables
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from aiogram.filters import CommandStart
from bs4 import BeautifulSoup

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()

# --- API Keys and IDs ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN or not OWNER_ID:
    logging.error("BOT_TOKEN or OWNER_ID not found in environment variables.")
    exit()
if not GEMINI_API_KEY:
    logging.warning("GEMINI_API_KEY not found. AI features will be disabled.")
else:
    genai.configure(api_key=GEMINI_API_KEY)


# --- Constants ---
EMAILS_FILE = "emails.json"
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PROCESSED_EMAIL_IDS: Set[str] = set()


# --- AI Decision Function ---
def decide_notify_ai(record: dict) -> Optional[bool]:
    """
    Uses Gemini to decide if an email requires a reply.
    Returns True (needs reply), False (informational), or None (uncertain/error).
    """
    if not GEMINI_API_KEY:
        return None # Fallback if no API key is set

    subject = record.get("subject", "")
    sender = record.get("from", "")
    body = record.get("clean_body", "")

    prompt = f"""
    You are an assistant for a logistics company. Your task is to classify incoming emails about trucking loads.
    Determine if an email requires a direct, urgent response or if it is just an informational update.

    - **YES**: The email asks a question, requires a status update, presents a problem, or is a direct offer that needs acceptance/rejection.
    - **NO**: The email is a confirmation, a receipt, an automated status update (e.g., "appointment updated," "load accepted"), or general marketing.

    Analyze the following email and respond with only the word YES or NO.

    **From:** {sender}
    **Subject:** {subject}
    **Body:**
    {body[:1500]}
    """

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        answer = response.text.strip().lower()
        logging.info(f"Gemini decision for '{subject}': {answer}")

        if "yes" in answer:
            return True
        elif "no" in answer:
            return False
        return None # If the model gives an ambiguous answer
    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        return None


# --- Gmail Authentication & Data Persistence (No changes needed here) ---
def authenticate_gmail():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

def load_processed_emails():
    global PROCESSED_EMAIL_IDS
    if not os.path.exists(EMAILS_FILE): return
    try:
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            PROCESSED_EMAIL_IDS = {e["id"] for e in json.load(f)}
    except (json.JSONDecodeError, IOError):
        pass

def save_email_record(record: Dict[str, Any]):
    data = []
    if os.path.exists(EMAILS_FILE):
        try:
            with open(EMAILS_FILE, "r", encoding="utf-8") as f: data = json.load(f)
        except (json.JSONDecodeError, IOError): pass
    data.append(record)
    with open(EMAILS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def update_email_record(email_id: str, decision: bool):
    if not os.path.exists(EMAILS_FILE): return
    try:
        with open(EMAILS_FILE, "r+", encoding="utf-8") as f:
            data = json.load(f)
            for e in data:
                if e.get("id") == email_id: e["needs_response"] = decision; break
            f.seek(0); f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, IOError): pass


# --- Email Processing (No changes needed here) ---
def find_email_parts(parts: List[Dict[str, Any]]) -> Dict[str, str]:
    body = {"plain": "", "html": ""}
    for part in parts:
        if "parts" in part:
            nested = find_email_parts(part["parts"])
            body["plain"] = body["plain"] or nested["plain"]
            body["html"] = body["html"] or nested["html"]
        mime_type = part.get("mimeType")
        if data := part.get("body", {}).get("data"):
            decoded_data = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if mime_type == "text/plain": body["plain"] = body["plain"] or decoded_data
            elif mime_type == "text/html": body["html"] = body["html"] or decoded_data
    return body

def extract_latest_reply(payload: Dict[str, Any]) -> Tuple[str, str]:
    body_parts = {"plain": "", "html": ""}
    if "parts" in payload:
        body_parts = find_email_parts(payload["parts"])
    elif data := payload.get("body", {}).get("data"):
        content = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if payload.get("mimeType") == "text/html": body_parts["html"] = content
        else: body_parts["plain"] = content
    original_body = body_parts["html"] or body_parts["plain"]
    cleaned_reply = original_body
    if html_body := body_parts["html"]:
        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["blockquote", "div.gmail_quote"]): tag.decompose()
        cleaned_reply = soup.get_text(separator="\n", strip=True)
    elif plain_text_body := body_parts["plain"]:
        cleaned_reply = re.split(r'\n_+\n|\nOn .* wrote:\n', plain_text_body, 1)[0].strip()
    return original_body, cleaned_reply or "(No readable content found)"


# --- Telegram Bot & Main Logic ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def check_new_emails(service):
    try:
        results = service.users().messages().list(userId="me", q="is:unread in:inbox").execute()
        messages = results.get("messages", [])
        if not messages:
            logging.info("No new unread emails found.")
            return
        logging.info(f"Found {len(messages)} unread emails. Processing...")
        for m in reversed(messages):
            await send_email_to_telegram(service, m["id"])
    except HttpError as error:
        logging.error(f"An API error occurred: {error}")

async def send_email_to_telegram(service, email_id: str):
    if email_id in PROCESSED_EMAIL_IDS: return
    try:
        msg = service.users().messages().get(userId="me", id=email_id).execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "No Subject")
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "Unknown Sender")
        original_body, clean_body = extract_latest_reply(payload)
        
        record = {"id": email_id, "subject": subject, "from": sender, "original_body": original_body, "clean_body": clean_body}
        
        # --- AI FILTER DECISION ---
        decision = decide_notify_ai(record)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Needs Response", callback_data=f"response:{email_id}:yes"),
            InlineKeyboardButton(text="❌ No Response", callback_data=f"response:{email_id}:no"),
        ]])
        
        processed_body = html.escape(clean_body)
        if len(processed_body) > 3800:
            processed_body = processed_body[:3800] + "\n\n<b>[Message truncated]</b>"

        if decision is True:
            message_text = (f"‼️ <b>AI: Action Needed</b>\n\n<b>From:</b> <code>{html.escape(sender)}</code>\n"
                            f"<b>Subject:</b> <code>{html.escape(subject)}</code>\n\n---\n\n{processed_body}")
        elif decision is False:
            message_text = (f"ℹ️ <b>AI: Informational</b>\n\n<b>From:</b> <code>{html.escape(sender)}</code>\n"
                            f"<b>Subject:</b> <code>{html.escape(subject)}</code>\n\n---\n\n<b>[Body omitted by AI filter]</b>")
        else:
            message_text = (f"📧 <b>New Email (AI Uncertain)</b>\n\n<b>From:</b> <code>{html.escape(sender)}</code>\n"
                            f"<b>Subject:</b> <code>{html.escape(subject)}</code>\n\n---\n\n{processed_body}")

        await bot.send_message(chat_id=OWNER_ID, text=message_text, reply_markup=keyboard, parse_mode="HTML")
        PROCESSED_EMAIL_IDS.add(email_id)
        save_email_record(record)
    except Exception as e:
        logging.error(f"Error processing email {email_id}: {e}", exc_info=True)


@dp.callback_query(F.data.startswith("response:"))
async def handle_decision_callback(query: CallbackQuery):
    _, email_id, decision_str = query.data.split(":")
    update_email_record(email_id, decision_str == "yes")
    response_text = "✅ Marked as 'Needs Response'" if decision_str == "yes" else "❌ Marked as 'No Response Needed'"
    await query.message.edit_text(query.message.html_text + f"\n\n--- \n<b>Status:</b> {response_text}", parse_mode="HTML")
    await query.answer(f"Decision saved.")

@dp.message(CommandStart())
async def start_command(message: Message):
    await message.answer("✅ AI Bot is online. Checking for new emails.")

async def polling_task(service):
    while True:
        logging.info("Running periodic email check...")
        await check_new_emails(service)
        await asyncio.sleep(30)

async def main():
    gmail_service = authenticate_gmail()
    if not gmail_service: return
    load_processed_emails()
    asyncio.create_task(polling_task(gmail_service))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())