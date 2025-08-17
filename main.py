import os
import json
import base64
import asyncio
import logging
import re
import html
from typing import List, Dict, Any, Optional, Set

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
# Set up basic logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Load environment variables from a .env file in the same directory
# Create a .env file with your BOT_TOKEN and OWNER_ID
# Example .env file:
# BOT_TOKEN="7228331804:AAH5itplNa7gjgy1srjl0KG0xwLPwBN-N2c"
# OWNER_ID="5292391509"
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
# Using an in-memory set for faster lookups of processed email IDs
PROCESSED_EMAIL_IDS: Set[str] = set()

# --- Gmail Authentication ---
def authenticate_gmail():
    """Handles Gmail API authentication and returns a service object."""
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
                creds = None # Force re-authentication
        
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
    """Loads processed email IDs from the JSON file into the in-memory set."""
    global PROCESSED_EMAIL_IDS
    if not os.path.exists(EMAILS_FILE):
        return
    try:
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            emails_data = json.load(f)
            PROCESSED_EMAIL_IDS = {e["id"] for e in emails_data}
            logging.info(f"Loaded {len(PROCESSED_EMAIL_IDS)} processed email IDs.")
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Could not read or parse {EMAILS_FILE}: {e}")

def save_email_record(record: Dict[str, Any]):
    """Appends a new email record to the JSON file."""
    data = []
    if os.path.exists(EMAILS_FILE):
        try:
            with open(EMAILS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Could not read for saving, starting fresh: {e}")
    
    data.append(record)
    
    try:
        with open(EMAILS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logging.error(f"Could not write to {EMAILS_FILE}: {e}")

def update_email_record(email_id: str, decision: bool):
    """Updates an email record in the JSON file with the user's decision."""
    if not os.path.exists(EMAILS_FILE):
        return
    try:
        with open(EMAILS_FILE, "r+", encoding="utf-8") as f:
            data = json.load(f)
            for e in data:
                if e["id"] == email_id:
                    e["needs_response"] = decision
                    break
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Could not update {EMAILS_FILE}: {e}")


# --- Email Processing ---
def get_email_body(msg_payload: Dict[str, Any]) -> str:
    """
    Extracts the email body. Prefers plain text, but falls back to
    stripping text from HTML if plain text is not available.
    """
    body = ""
    if "parts" in msg_payload:
        # First, search for a plain text part
        for part in msg_payload["parts"]:
            if part["mimeType"] == "text/plain" and "data" in part["body"]:
                body_data = part["body"]["data"]
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        
        # If no plain text, search for an HTML part
        for part in msg_payload["parts"]:
            if part["mimeType"] == "text/html" and "data" in part["body"]:
                body_data = part["body"]["data"]
                html_content = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
                soup = BeautifulSoup(html_content, "html.parser")
                return soup.get_text(separator="\n", strip=True)

    # Fallback for simple, non-multipart emails
    elif "data" in msg_payload["body"]:
        body_data = msg_payload["body"]["data"]
        content = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        if msg_payload["mimeType"] == "text/html":
            soup = BeautifulSoup(content, "html.parser")
            return soup.get_text(separator="\n", strip=True)
        return content
        
    return "(No readable content found)"

def format_body_for_html(body: str) -> str:
    """
    Escapes HTML and converts URLs into clickable links for Telegram.
    """
    # Regex to find URLs (http/https)
    url_pattern = re.compile(r'(https?://[^\s]+)')
    
    # Split the body by URLs. The resulting list will be [text, url, text, url, ...].
    parts = url_pattern.split(body)
    
    result_html = ""
    for i, part in enumerate(parts):
        if not part: # Skip empty parts from the split
            continue
        if i % 2 == 0:
            # This is a normal text part, escape it for HTML safety.
            result_html += html.escape(part)
        else:
            # This is a URL part, create a clickable link.
            result_html += f'<a href="{html.escape(part)}">link</a>'
            
    return result_html

# --- Telegram Bot Logic ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def send_email_to_telegram(service, email_id: str):
    """Fetches a single email and sends it to the owner via Telegram."""
    if email_id in PROCESSED_EMAIL_IDS:
        return  # Skip already processed email

    try:
        msg = service.users().messages().get(userId="me", id=email_id).execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        
        subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "No Subject")
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "Unknown Sender")

        body = get_email_body(payload)
        
        # Process the body to handle HTML and create links
        processed_body = format_body_for_html(body)
        
        # Truncate long bodies for Telegram message limits
        if len(processed_body) > 3800:
            processed_body = processed_body[:3800] + "\n\n<b>[Message truncated]</b>"

        record = {"id": email_id, "subject": subject, "from": sender, "body": body, "needs_response": None}
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Needs Response", callback_data=f"response:{email_id}:yes"),
                InlineKeyboardButton(text="❌ No Response", callback_data=f"response:{email_id}:no"),
            ]
        ])

        # Escape sender and subject to be safe
        sender_safe = html.escape(sender)
        subject_safe = html.escape(subject)

        message_text = f"📧 <b>New Email</b>\n\n<b>From:</b> <code>{sender_safe}</code>\n<b>Subject:</b> <code>{subject_safe}</code>\n\n---\n\n{processed_body}"

        await bot.send_message(
            chat_id=OWNER_ID,
            text=message_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
        # Add to processed list and save
        PROCESSED_EMAIL_IDS.add(email_id)
        save_email_record(record)

    except HttpError as error:
        logging.error(f"An API error occurred while fetching email {email_id}: {error}")
    except Exception as e:
        logging.error(f"An unexpected error occurred in send_email_to_telegram: {e}")


async def check_new_emails(service):
    """Checks for new emails and triggers sending them."""
    if not service:
        logging.warning("Gmail service not available. Skipping email check.")
        return
    try:
        results = service.users().messages().list(userId="me", maxResults=10, q="is:unread").execute()
        messages = results.get("messages", [])
        if not messages:
            logging.info("No new unread emails found.")
            return
            
        logging.info(f"Found {len(messages)} unread emails. Processing...")
        for m in reversed(messages): # Process oldest unread first
            await send_email_to_telegram(service, m["id"])

    except HttpError as error:
        logging.error(f"An API error occurred while checking for new emails: {error}")
    except Exception as e:
        logging.error(f"An unexpected error occurred in check_new_emails: {e}")

@dp.callback_query(F.data.startswith("response:"))
async def handle_decision_callback(query: CallbackQuery):
    """Handles the user's decision from the inline keyboard."""
    try:
        _, email_id, decision_str = query.data.split(":")
        decision = (decision_str == "yes")
        
        update_email_record(email_id, decision)
        
        response_text = "✅ Marked as 'Needs Response'" if decision else "❌ Marked as 'No Response Needed'"
        
        # Edit the message to show the decision and remove the buttons
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

# --- Main Application Loop ---
async def polling_task(service):
    """The main background task that periodically checks for emails."""
    while True:
        logging.info("Running periodic email check...")
        await check_new_emails(service)
        await asyncio.sleep(60)

async def main():
    """Main function to initialize and run the bot."""
    # Authenticate and load initial data
    gmail_service = authenticate_gmail()
    if not gmail_service:
        logging.critical("Could not start bot due to Gmail authentication failure.")
        return
        
    load_processed_emails()

    # Start the background polling task
    asyncio.create_task(polling_task(gmail_service))
    
    # Start the bot
    await bot.send_message(chat_id=OWNER_ID, text="✅ Bot is online and ready.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
