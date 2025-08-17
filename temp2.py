import os
import json
import base64
import asyncio
import logging
import re
import html
from typing import List, Dict, Any, Optional, Set, Tuple

# Using python-dotenv to load environment variables from a .env file
# You'll need to run: pip install python-dotenv
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
# Set up basic logging to see the bot's activity and any potential errors.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Load environment variables from a .env file in the same directory.
# Create a .env file with your BOT_TOKEN and OWNER_ID.
# Example .env file:
# BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
# OWNER_ID="YOUR_TELEGRAM_USER_ID"
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if not BOT_TOKEN or not OWNER_ID:
    logging.error("BOT_TOKEN or OWNER_ID not found in environment variables. Please create a .env file.")
    exit()

# --- Constants ---
EMAILS_FILE = "emails.json"
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# Using an in-memory set for faster lookups of processed email IDs to avoid re-sending.
PROCESSED_EMAIL_IDS: Set[str] = set()

# --- Gmail Authentication ---
def authenticate_gmail():
    """
    Handles Gmail API authentication. It uses a saved token.json if available,
    refreshes it if expired, or creates a new one via a browser flow.
    Returns a Gmail API service object.
    """
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logging.error(f"Failed to load credentials from {TOKEN_FILE}: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logging.error(f"Failed to refresh token: {e}")
                creds = None # Force re-authentication if refresh fails
        
        if not creds:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                logging.error(f"Error: {CREDENTIALS_FILE} not found. Please download it from Google Cloud Console.")
                return None
            except Exception as e:
                logging.error(f"An error occurred during the OAuth flow: {e}")
                return None

        try:
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
            logging.info("Credentials saved successfully.")
        except IOError as e:
            logging.error(f"Could not write to {TOKEN_FILE}: {e}")

    if creds:
        logging.info("Gmail authentication successful.")
        return build("gmail", "v1", credentials=creds)
    
    logging.error("Could not authenticate with Gmail.")
    return None

# --- Data Persistence ---
def load_processed_emails():
    """Loads processed email IDs from the JSON file into the in-memory set on startup."""
    global PROCESSED_EMAIL_IDS
    if not os.path.exists(EMAILS_FILE):
        return
    try:
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            emails_data = json.load(f)
            PROCESSED_EMAIL_IDS = {e["id"] for e in emails_data if 'id' in e}
            logging.info(f"Loaded {len(PROCESSED_EMAIL_IDS)} processed email IDs.")
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Could not read or parse {EMAILS_FILE}: {e}")

def save_email_record(record: Dict[str, Any]):
    """Appends a new email record to the JSON file for data collection."""
    data = []
    if os.path.exists(EMAILS_FILE):
        try:
            with open(EMAILS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Could not read {EMAILS_FILE} for saving, starting fresh: {e}")
            data = []
    
    data.append(record)
    
    try:
        with open(EMAILS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logging.error(f"Could not write to {EMAILS_FILE}: {e}")

def update_email_record(email_id: str, decision: bool):
    """Updates an email record in the JSON file with the user's response decision."""
    if not os.path.exists(EMAILS_FILE):
        return
    try:
        with open(EMAILS_FILE, "r+", encoding="utf-8") as f:
            data = json.load(f)
            for e in data:
                if e.get("id") == email_id:
                    e["needs_response"] = decision
                    break
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Could not update {EMAILS_FILE}: {e}")

# --- Email Processing ---
def find_email_parts(parts: List[Dict[str, Any]]) -> Dict[str, str]:
    """Recursively searches through MIME parts to find the email body."""
    body = {"plain": "", "html": ""}
    for part in parts:
        if "parts" in part:
            nested_parts = find_email_parts(part["parts"])
            if nested_parts["plain"] and not body["plain"]: body["plain"] = nested_parts["plain"]
            if nested_parts["html"] and not body["html"]: body["html"] = nested_parts["html"]
        
        mime_type = part.get("mimeType")
        if mime_type == "text/plain" and not body["plain"]:
            if data := part.get("body", {}).get("data"):
                body["plain"] = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        elif mime_type == "text/html" and not body["html"]:
            if data := part.get("body", {}).get("data"):
                body["html"] = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return body

def extract_latest_reply(payload: Dict[str, Any]) -> Tuple[str, str]:
    """
    Extracts the original email content and a clean version of the latest reply.
    Handles nested multipart emails by searching recursively.
    """
    body_parts = {"plain": "", "html": ""}
    if "parts" in payload:
        body_parts = find_email_parts(payload["parts"])
    elif "data" in payload.get("body", {}):
        content = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        if payload.get("mimeType") == "text/html": body_parts["html"] = content
        else: body_parts["plain"] = content
    
    original_body = body_parts["html"] or body_parts["plain"]
    cleaned_reply = ""
    
    if html_body := body_parts["html"]:
        soup = BeautifulSoup(html_body, "html.parser")
        for blockquote in soup.find_all("blockquote"): blockquote.decompose()
        if gmail_quote := soup.find('div', class_='gmail_quote'): gmail_quote.decompose()
        cleaned_reply = soup.get_text(separator="\n", strip=True)
    elif plain_text_body := body_parts["plain"]:
        parts = re.split(r'\n_+\n|\nOn .* wrote:\n', plain_text_body, 1)
        cleaned_reply = parts[0].strip()

    if not cleaned_reply.strip() and body_parts["plain"]:
        cleaned_reply = body_parts["plain"].strip()
        
    return original_body, cleaned_reply or "(No readable content found)"

def format_body_for_telegram(body: str) -> str:
    """Escapes HTML and converts URLs into clickable links for Telegram."""
    escaped_body = html.escape(body)
    return re.sub(r'(https?://[^\s<]+)', r'<a href="\1">link</a>', escaped_body)

# --- Telegram Bot & Main Logic ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def check_new_emails(service):
    """Checks for all new emails using pagination and triggers sending them."""
    if not service:
        logging.warning("Gmail service not available. Skipping email check.")
        return
    try:
        all_messages = []
        page_token = None
        
        while True:
            results = service.users().messages().list(
                userId="me",
                q="is:unread in:inbox",
                maxResults=500,
                pageToken=page_token
            ).execute()
            
            messages = results.get("messages", [])
            all_messages.extend(messages)
            
            page_token = results.get("nextPageToken")
            if not page_token:
                break

        if not all_messages:
            logging.info("No new unread emails found.")
            return
            
        logging.info(f"Found {len(all_messages)} unread emails. Processing...")
        for m in reversed(all_messages):
            await send_email_to_telegram(service, m["id"])

    except HttpError as error:
        logging.error(f"An API error occurred while checking for new emails: {error}")
    except Exception as e:
        logging.error(f"An unexpected error in check_new_emails: {e}", exc_info=True)

async def send_email_to_telegram(service, email_id: str):
    """Fetches a single email, formats it, and sends it to the owner via Telegram."""
    if email_id in PROCESSED_EMAIL_IDS:
        return

    try:
        msg = service.users().messages().get(userId="me", id=email_id).execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        
        subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "No Subject")
        subject = subject.removeprefix("Re: ").removeprefix("RE: ").removeprefix("re: ")
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "Unknown Sender")

        original_body, clean_body = extract_latest_reply(payload)
        processed_body = format_body_for_telegram(clean_body)
        
        if len(processed_body) > 3800:
            processed_body = processed_body[:3800] + "\n\n<b>[Message truncated]</b>"

        record = {"id": email_id, "subject": subject, "from": sender, "original_body": original_body, "needs_response": None}
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Needs Response", callback_data=f"response:{email_id}:yes"),
                InlineKeyboardButton(text="❌ No Response", callback_data=f"response:{email_id}:no"),
            ]
        ])

        message_text = (f"📧 <b>New Email</b>\n\n"
                        f"<b>From:</b> <code>{html.escape(sender)}</code>\n"
                        f"<b>Subject:</b> <code>{html.escape(subject)}</code>\n\n"
                        f"---\n\n{processed_body}")

        await bot.send_message(chat_id=OWNER_ID, text=message_text, reply_markup=keyboard, parse_mode="HTML")
        
        PROCESSED_EMAIL_IDS.add(email_id)
        save_email_record(record)

    except HttpError as error:
        logging.error(f"An API error occurred fetching email {email_id}: {error}")
    except Exception as e:
        logging.error(f"Unexpected error in send_email_to_telegram for {email_id}: {e}", exc_info=True)

@dp.callback_query(F.data.startswith("response:"))
async def handle_decision_callback(query: CallbackQuery):
    """Handles the user's decision from the inline keyboard."""
    try:
        _, email_id, decision_str = query.data.split(":")
        decision = (decision_str == "yes")
        update_email_record(email_id, decision)
        response_text = "✅ Marked as 'Needs Response'" if decision else "❌ Marked as 'No Response Needed'"
        await query.message.edit_text(
            text=query.message.html_text + f"\n\n--- \n<b>Status:</b> {response_text}",
            parse_mode="HTML"
        )
        await query.answer(f"Decision saved: {response_text}")
    except Exception as e:
        logging.error(f"Error handling callback: {e}")
        await query.answer("Error processing your request.")

@dp.message(CommandStart())
async def start_command(message: Message):
    """Handler for the /start command."""
    await message.answer("✅ Bot is online. I will check for new emails every minute.")

async def polling_task(service):
    """The main background task that periodically checks for emails."""
    while True:
        logging.info("Running periodic email check...")
        await check_new_emails(service)
        await asyncio.sleep(30)

async def main():
    """Main function to initialize and run the bot."""
    gmail_service = authenticate_gmail()
    if not gmail_service:
        logging.critical("Could not start bot due to Gmail authentication failure.")
        return
        
    load_processed_emails()
    asyncio.create_task(polling_task(gmail_service))
    await bot.send_message(chat_id=OWNER_ID, text="✅ Bot is online and ready.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
