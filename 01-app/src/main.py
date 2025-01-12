# Standard library imports
import os
import logging
import datetime
import threading
from datetime import timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
import uuid
import requests

# Third-party imports
import uvicorn
import telebot
from telebot import types
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from dotenv import load_dotenv

# Google Cloud imports
import vertexai
from vertexai.generative_models import GenerativeModel
from google.cloud import firestore

from templates import (
    START_TEXT,
    SYSTEM_INSTRUCTIONS,
    SHOULD_SEND_MESSAGE_PROMPT,
)

load_dotenv()


# (1) ENVIRONMENT & CONFIGURATION

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
URL = os.environ.get("URL")
WHOOP_CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET")
BOT_MODE = os.environ.get("BOT_MODE", "webhook")

logging.basicConfig(level=logging.INFO)


# (2) STARTUP & GLOBAL OBJECTS

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, model

    try:
        # Initialize Firestore and Vertex AI as before
        db = firestore.Client(project=GCP_PROJECT_ID)
        logging.info("Firestore client initialized")

        vertexai.init(project=GCP_PROJECT_ID, location="us-central1")
        model = GenerativeModel("gemini-exp-1206")
        logging.info("Vertex AI model initialized")
        
        # Only set up new webhook if we're in webhook mode
        if BOT_MODE == "webhook":
            webhook_url = f"{URL}/webhook"
            bot.set_webhook(url=webhook_url)
            logging.info(f"Telegram webhook set to: {webhook_url}")

    except Exception as e:
        logging.error(f"Error during initialization: {e}")
        raise e

    yield

    logging.info("Shutting down...")


app = FastAPI(lifespan=lifespan)
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
logging.info("Telegram bot initialized")


# (3) FIRESTORE DATA MODEL & INTERACTIONS

class UserProfile(BaseModel):
    telegram_id: str
    name: Optional[str] = None
    joined_at: Optional[datetime.datetime] = None

def store_chat_message(telegram_id: str, role: str, content: str) -> None:
    """Store a chat message in Firestore using the new data structure."""
    try:
        # Generate a unique message ID (you could also use Firestore's auto-ID)
        message_id = str(uuid.uuid4())
        
        # Create the chat document in the chats subcollection
        db.collection("users").document(telegram_id)\
          .collection("chats").document(message_id).set({
            "timestamp": datetime.datetime.utcnow(),
            "content": content,
            "role": "user" if role == "user" else "assistant"
        })
    except Exception as e:
        logging.error(f"Error storing chat message for user {telegram_id}: {e}")
        raise

def get_chat_history(telegram_id: str, limit: int = 100) -> list:
    """Retrieve recent chat history from the new data structure."""
    print(f"GETTING CHAT HISTORY FOR {telegram_id}")
    try:
        # Query the chats subcollection, ordered by timestamp
        chats_ref = db.collection("users").document(telegram_id)\
                     .collection("chats")\
                     .order_by("timestamp", direction=firestore.Query.ASCENDING)\
                     .limit(limit)
        
        # Get the documents and convert to the format expected by the bot
        messages = []
        for chat in chats_ref.stream():
            chat_data = chat.to_dict()
            messages.append({
                "role": chat_data["role"],
                "content": chat_data["content"],
                "timestamp": chat_data["timestamp"].isoformat()
            })
        
        # Remove the reversal to keep chronological order (oldest first)
        return messages
    except Exception as e:
        logging.error(f"Error retrieving chat history for user {telegram_id}: {e}")
        return []

def get_health_data(telegram_id: str) -> dict:
    """Retrieve recent health metrics for a user from Firestore.
    
    Args:
        telegram_id (str): The user's Telegram ID
        
    Returns:
        dict: Dictionary containing recent health metrics, or empty if none found
    """
    try:
        # Get the most recent health metrics document
        metrics_ref = (
            db.collection("users")
            .document(telegram_id)
            .collection("health_metrics")
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        
        metrics_docs = metrics_ref.stream()
        latest_metrics = next(metrics_docs, None)
        
        if not latest_metrics:
            return {"sleep_data": "No recent health data available"}
            
        metrics_data = latest_metrics.to_dict()
        
        # Format the data into a readable string
        sleep_data = (
            f"Sleep Duration: {metrics_data.get('sleep_duration', 'N/A')} hours\n"
            f"Sleep Quality: {metrics_data.get('sleep_quality', 'N/A')}\n"
            f"Daily Strain: {metrics_data.get('strain', 'N/A')}"
        )
        
        return {
            "sleep_data": sleep_data,
            "timestamp": metrics_data.get("timestamp"),
            "raw_metrics": metrics_data
        }
        
    except Exception as e:
        logging.error(f"Error retrieving health metrics for user {telegram_id}: {e}")
        return {"sleep_data": "Error retrieving health data"}


# (4) TELEGRAM BOT HANDLERS

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    """Handle /start command with the new data structure."""
    telegram_id = str(message.from_user.id)
    
    try:
        # Create user profile document if it doesn't exist
        user_doc_ref = db.collection("users").document(telegram_id)
        if not user_doc_ref.get().exists:
            user_doc_ref.set({
                "telegram_id": telegram_id,
                "name": message.from_user.first_name,
                "joined_at": datetime.datetime.utcnow()
            })
        
        bot.reply_to(message, START_TEXT)
        
    except Exception as e:
        logging.error(f"Error in start handler for user {telegram_id}: {e}")
        bot.reply_to(message, "Sorry, I encountered an error while setting up your profile. Please try again later.")

@bot.message_handler(commands=["linkwhoop"])
def handle_link_whoop(message: types.Message):
    """Handle /linkwhoop command to connect WHOOP account."""
    telegram_id = str(message.from_user.id)
    state_value = create_oauth_state_for_user(telegram_id)
    redirect_uri = f"{URL}/whoop/callback"
    scope = "offline read:profile read:recovery read:sleep read:workout"
    
    auth_url = (
        "https://api.prod.whoop.com/oauth/oauth2/auth"
        f"?response_type=code"
        f"&client_id={WHOOP_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&state={state_value}"
    )

    bot.reply_to(
        message,
        (
            "Please click the link below to authorize your WHOOP account:\n"
            f'<a href="{auth_url}">Authorize Bot</a>\n\n'
            "After you approve access, you'll be redirected back, and I'll store your tokens."
        ),
    )

@bot.message_handler(commands=["sleep"])
def handle_sleep(message: types.Message):
    telegram_id = str(message.from_user.id)
    user_doc_ref = db.collection("users").document(telegram_id).get()

    if not user_doc_ref.exists:
        bot.reply_to(message, "Please /start first.")
        return

    user_data = user_doc_ref.to_dict()
    whoop_token = user_data.get("whoop_access_token")

    if not whoop_token:
        bot.reply_to(message, "Please link your WHOOP account first using /linkwhoop")
        return

    parts = message.text.split()
    date = parts[1] if len(parts) > 1 else (
        datetime.datetime.now() - datetime.timedelta(days=1)
    ).strftime("%Y-%m-%d")

    try:
        # Call the new version of fetch_whoop_sleep_data that handles refresh
        sleep_data_response = fetch_whoop_sleep_data(telegram_id, start_date=date)

        if not sleep_data_response.get("success"):
            bot.reply_to(message, f"Error: {sleep_data_response.get('error')}")
            return

        sleep_data = sleep_data_response.get("records")
        if not sleep_data:
            bot.reply_to(message, f"No sleep data available for {date}")
            return

        # Format your message as usual
        response = f"🛏️ Sleep Report for {date}:\n\n"
        for entry in sleep_data:
            stage_summary = entry["score"]["stage_summary"]
            slow_wave = stage_summary["total_slow_wave_sleep_time_milli"]
            rem = stage_summary["total_rem_sleep_time_milli"]
            total = slow_wave + rem

            response += f"* Slow wave sleep: {millis_to_hhmm(slow_wave)}\n"
            response += f"* REM sleep: {millis_to_hhmm(rem)}\n"
            response += f"* Total: {millis_to_hhmm(total)}\n\n"

        bot.reply_to(message, response)
    except Exception as e:
        logging.error(f"Error processing sleep data: {e}")
        bot.reply_to(message, "Sorry, there was an error fetching your sleep data.")

@bot.message_handler(func=lambda message: True)
def handle_chat(message: types.Message):
    """Default handler that forwards user messages to Gemini for a response"""
    try:
        telegram_id = str(message.from_user.id)
        user_message = message.text
        
        # Get recent chat history
        chat_history = get_chat_history(telegram_id)
        
        # Get health data
        health_data = get_health_data(telegram_id)
              
        # Add context and system instructions to the prompt
        prompt = SYSTEM_INSTRUCTIONS.format(
                user_name=message.from_user.first_name,
                health_data=health_data.get('sleep_data', 'No data'),
                chat_history=chat_history[-3:] if chat_history else 'No history',
                current_message=user_message
            )
        
        # Generate response from Gemini
        response = model.generate_content(prompt)
        
        if response.text:
            # Convert markdown to HTML before sending
            formatted_response = convert_markdown_to_html(response.text)
            
            # Store both messages after getting the response
            store_chat_message(telegram_id, "user", user_message)
            store_chat_message(telegram_id, "assistant", formatted_response)
            bot.reply_to(message, formatted_response)
        else:
            bot.reply_to(message, "I apologize, but I couldn't generate a response. Please try again.")
            
    except Exception as e:
        logging.error(f"Error in chat handler: {e}")
        bot.reply_to(message, "Sorry, I encountered an error while processing your message. Please try again later.")


# (5) FASTAPI ROUTES

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    The Telegram webhook endpoint. All updates from Telegram come here.
    """
    try:
        update = await request.json()
        logging.info(f"Received webhook update: {update}")
        bot.process_new_updates([telebot.types.Update.de_json(update)])
        logging.info("Successfully processed webhook update")
    except Exception as e:
        logging.error(f"Telegram webhook error: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}

@app.post("/scheduled-check-in")
async def scheduled_check_in(background_tasks: BackgroundTasks):
    """Endpoint triggered by Cloud Scheduler every x hours to check users and send proactive messages if appropriate."""
    print("CHECK IN")
    try:
        # Get all users
        users_ref = db.collection("users")
        users_list = users_ref.stream()

        for user_doc in users_list:
            telegram_id = user_doc.id
            print(f"USER ID: {telegram_id}")
            
            # Get user data directly from the user document
            user_data = user_doc.to_dict()
            print(f"USER DATA: {user_data}")
            
            if not user_data:
                print(f"No user data found for user {telegram_id}")
                continue
                
            # Get recent chat history
            chat_history = get_chat_history(telegram_id)
            print(f"CHAT HISTORY: {chat_history}")
            
            # Check if we should message this user
            if should_send_message(chat_history):
                # Generate appropriate message
                message = generate_proactive_message(user_data, chat_history)
                
                print(f"MESSAGE: {message}")
                
                if message:
                    # Send message in background to avoid timeout
                    background_tasks.add_task(bot.send_message, telegram_id, message)
                    # Store bot's message in chat history
                    background_tasks.add_task(
                        store_chat_message, 
                        telegram_id, 
                        "assistant", 
                        message
                    )
        
        return {"status": "success", "message": "Proactive check completed"}
    except Exception as e:
        logging.error(f"Error in scheduled check: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
def root():
    return {"message": "Fitness & Health Telegram Bot up and running."}

@app.get("/whoop/callback")
async def whoop_callback(request: Request):
    """Handle WHOOP OAuth callback."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return {"error": "Missing code or state in the WHOOP callback."}

    oauth_state_doc = db.collection("oauth_states").document(state).get()
    if not oauth_state_doc.exists:
        return {"error": "Invalid or expired state. Cannot link WHOOP account."}

    telegram_id = oauth_state_doc.to_dict().get("telegram_id")
    if not telegram_id:
        return {"error": "Could not determine Telegram user from state."}

    token_url = "https://api.prod.whoop.com/oauth/oauth2/token"
    redirect_uri = f"{URL}/whoop/callback"

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
    }

    try:
        resp = requests.post(token_url, data=payload, timeout=10)
        resp.raise_for_status()
        token_data = resp.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not access_token:
            return {"error": "No access token returned from WHOOP."}

        # Store tokens in Firestore
        db.collection("users").document(telegram_id).set(
            {"whoop_access_token": access_token, "whoop_refresh_token": refresh_token},
            merge=True,
        )

        # Clean up used state
        db.collection("oauth_states").document(state).delete()

        # Notify user
        bot.send_message(
            telegram_id,
            "Your WHOOP account is now linked! You can use /sleep to view your sleep data.",
        )

        return {"message": "WHOOP authorization successful! You can close this page."}
    except Exception as e:
        logging.error(f"Error exchanging code for token: {e}")
        return {"error": "Failed to exchange token with WHOOP."}


# (6) HELPER FUNCTIONS

def should_send_message(chat_history: List[Dict]) -> bool:
    """Determines if it's appropriate to send a proactive message"""
    if not chat_history:
        return True
        
    last_message = chat_history[-1]
    last_timestamp = datetime.datetime.fromisoformat(last_message['timestamp'])
    # Convert utcnow to timezone-aware datetime
    current_time = datetime.datetime.now(datetime.timezone.utc)
    hours_since_last = (current_time - last_timestamp).total_seconds() / 3600
    
    # Don't message if less than 4 hours have passed
    #if hours_since_last < 4:
    #    return False
        
    # Don't message during typical sleeping hours (11 PM - 6 AM local time)
    current_hour = datetime.datetime.now().hour
    if current_hour >= 22 or current_hour < 7:
        return False
    
    # Analyze recent chat history for user sentiment
    recent_messages = chat_history[-5:]  # Look at last 5 messages
    context = "\n".join([
        f"{msg['role']}: {msg['content']}" 
        for msg in recent_messages
    ])
    
    prompt = SHOULD_SEND_MESSAGE_PROMPT.format(context=context)
    
    try:
        response = model.generate_content(prompt)
        should_message = response.text.strip().lower() == "yes"
        print(f"Should message: {response.text}")
        return should_message
    except Exception as e:
        logging.error(f"Error analyzing chat sentiment: {e}")
        return False

def generate_proactive_message(user_data: dict, chat_history: List[Dict]) -> Optional[str]:
    """
    Generates a contextual message based on user's data and chat history
    """
    health_data = user_data.get('health_data', {})
    user_name = user_data.get('name', 'User')
    
    prompt = SYSTEM_INSTRUCTIONS.format(
        user_name=user_name,
        health_data=health_data.get('sleep_data', 'No data'),
        chat_history=chat_history[-3:] if chat_history else 'No history',
        current_message=""
    )
    
    try:
        response = model.generate_content(prompt)
        return response.text if response.text else None
    except Exception as e:
        logging.error(f"Error generating proactive message: {e}")
        return None

def convert_markdown_to_html(text: str) -> str:
    """Convert markdown-style formatting to HTML tags."""
    # Handle bold text (**text**)
    parts = text.split('**')
    for i in range(1, len(parts), 2):
        if i < len(parts):
            # Wrap odd-indexed parts with <b> tags
            parts[i] = f'<b>{parts[i]}</b>'
    return ''.join(parts)

def create_oauth_state_for_user(telegram_id: str) -> str:
    """Generate a unique 'state' value and store it in Firestore."""
    state_value = str(uuid.uuid4())
    db.collection("oauth_states").document(state_value).set(
        {"telegram_id": telegram_id}
    )
    return state_value

def fetch_whoop_sleep_data(telegram_id: str, start_date: str = None) -> dict:
    """
    Fetch sleep data from WHOOP API, refreshing the access token if needed.
    """
    # 1) Get current user doc to retrieve access and refresh tokens
    user_doc_ref = db.collection("users").document(telegram_id).get()
    if not user_doc_ref.exists:
        logging.error(f"No user doc for {telegram_id}")
        return {}
    
    user_data = user_doc_ref.to_dict()
    access_token = user_data.get("whoop_access_token")
    refresh_token = user_data.get("whoop_refresh_token")
    
    if not access_token:
        logging.error("No access token found.")
        return {}

    # 2) Attempt the API request
    sleep_data_response = _call_whoop_sleep_api(access_token, start_date)

    # 3) If we got a 401 or 403, refresh the token
    if not sleep_data_response.get("success"):
        logging.info("Access token might be expired, attempting refresh...")
        refreshed_tokens = refresh_whoop_token(refresh_token)
        if refreshed_tokens:
            # 3a) Update Firestore with new tokens
            new_access_token = refreshed_tokens.get("access_token")
            new_refresh_token = refreshed_tokens.get("refresh_token")

            db.collection("users").document(telegram_id).set(
                {
                    "whoop_access_token": new_access_token,
                    "whoop_refresh_token": new_refresh_token,
                },
                merge=True,
            )
            # 3b) Retry the original request
            sleep_data_response = _call_whoop_sleep_api(new_access_token, start_date)

    return sleep_data_response

def _call_whoop_sleep_api(access_token: str, start_date: str = None) -> dict:
    """
    Makes the actual GET request to WHOOP for sleep data. 
    Returns a dict with a field 'success' = True/False to indicate any error states.
    """
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = "https://api.prod.whoop.com/developer/v1/activity/sleep"
        params = {"limit": 1}
        if start_date:
            params["start_date"] = start_date
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        # If unauthorized, you might check response.status_code == 401/403
        if response.status_code == 401 or response.status_code == 403:
            return {"success": False, "error": "Unauthorized or Token Expired"}

        response.raise_for_status()        
        return {"success": True, **response.json()}
    except Exception as e:
        logging.error(f"Error fetching Whoop sleep data: {e}")
        return {"success": False, "error": str(e)}

def refresh_whoop_token(refresh_token: str) -> Optional[dict]:
    """
    Calls WHOOP's refresh token endpoint to get a new access token.
    Returns a dict with new tokens on success, or None on failure.
    """
    token_url = "https://api.prod.whoop.com/oauth/oauth2/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
        "scope": "offline read:profile read:recovery read:sleep read:workout"
    }

    try:
        resp = requests.post(token_url, data=payload, timeout=10)
        resp.raise_for_status()
        token_data = resp.json()
        # Should contain "access_token" and "refresh_token"
        return token_data
    except Exception as e:
        logging.error(f"Error refreshing token: {e}")
        return None

def millis_to_hhmm(milliseconds):
    """Convert milliseconds to HH:MM format."""
    td = timedelta(milliseconds=milliseconds)
    total_minutes = td.total_seconds() / 60
    hours = int(total_minutes // 60)
    minutes = int(total_minutes % 60)
    return f"{hours:02d}:{minutes:02d}"


# (7) GUNICORN / UVICORN ENTRYPOINT

if __name__ == "__main__":
    if BOT_MODE == "polling":
        # First, explicitly remove any webhook
        bot.remove_webhook()
        logging.info("Removed webhook for polling mode")
        
        # Then start bot polling in a separate thread
        polling_thread = threading.Thread(target=bot.infinity_polling)
        polling_thread.start()
        logging.info("Started bot polling")

    # Start the FastAPI server
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8080)))