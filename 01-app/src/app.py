# Standard library imports
import os
import logging
import datetime
import threading
from datetime import timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
import uuid
import json
import requests

# Third-party imports
import uvicorn
import telebot
from telebot import types
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from dotenv import load_dotenv

# Google Cloud imports
from google.cloud import firestore
import google.generativeai as genai

from templates import (
    START_TEXT,
    SYSTEM_INSTRUCTIONS,
    HEALTH_REPORT_PROMPT,
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
genai.configure(api_key=os.environ["GEMINI_API_KEY"])


# (2) STARTUP & GLOBAL OBJECTS

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, model

    try:
        # Initialize Firestore and Vertex AI as before
        db = firestore.Client(project=GCP_PROJECT_ID)
        logging.info("Firestore client initialized")

        generation_config = {
            "max_output_tokens": 65536,
            "response_mime_type": "text/plain",
        }

        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash-thinking-exp-01-21",
            generation_config=generation_config,
        )
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

def get_daily_health_data_from_firestore(telegram_id: str, date_str: str) -> dict:
    """
    Fetch daily health_metrics doc for the given user & date 
    from Firestore and return it.
    """
    doc_ref = (
        db.collection("users")
          .document(telegram_id)
          .collection("health_metrics")
          .document(date_str)
    )
    print(f"DATE: {date_str}")
    doc_snapshot = doc_ref.get()

    if not doc_snapshot.exists:
        return {}

    return doc_snapshot.to_dict()


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

@bot.message_handler(commands=["report"])
def handle_report(message: types.Message):
    """
    Provides a combined health report (sleep + recovery + workout) for the user
    and includes an analysis by the Gemini model.
    """
    telegram_id = str(message.from_user.id)

    # Check if user exists
    user_doc_ref = db.collection("users").document(telegram_id).get()
    if not user_doc_ref.exists:
        bot.reply_to(message, "Please /start first.")
        return

    # Parse date or default to *today* (adjust if needed)
    parts = message.text.split()
    date_str = parts[1] if len(parts) > 1 else (
        datetime.datetime.now()
    ).strftime("%Y-%m-%d")

    # 1) Retrieve from Firestore
    daily_data = get_daily_health_data_from_firestore(telegram_id, date_str)
    sleep_records = daily_data.get("sleep_records", [])
    recovery_records = daily_data.get("recovery_records", [])
    workout_records = daily_data.get("workout_records", [])

    # If there's no data at all, notify and exit
    if not (sleep_records or recovery_records or workout_records):
        bot.reply_to(message, f"No data in Firestore for {date_str}.")
        return

    #
    # 2) Build the "user-friendly" text portions 
    #    (the concise stuff you want the user to see, not the full JSON)
    #

    # --- Sleep Summary ---
    # For demonstration, just show total slow wave & REM from the first sleep record
    sleep_summary_text = "No sleep data available."
    if sleep_records:
        entry = sleep_records[0]  # If multiple, decide how you want to handle them
        stage_summary = entry["score"]["stage_summary"]
        slow_wave = stage_summary["total_slow_wave_sleep_time_milli"]
        rem = stage_summary["total_rem_sleep_time_milli"]
        total = slow_wave + rem
        sleep_summary_text = (
            f"Slow Wave: {millis_to_hhmm(slow_wave)}\n"
            f"REM: {millis_to_hhmm(rem)}\n"
            f"Total (SWS + REM): {millis_to_hhmm(total)}\n"
        )

    # --- Recovery Summary ---
    # For demonstration, just show the first record's "recovery_score"
    recovery_summary_text = "No recovery data available."
    if recovery_records:
        entry = recovery_records[0]
        score = entry["score"]["recovery_score"]
        recovery_summary_text = f"Recovery Score: {score}"

    # --- Workout Summary ---
    # For demonstration, just show the first record's "strain" and "kilojoules"
    workout_summary_text = "No workout data available."
    if workout_records:
        entry = workout_records[0]
        strain = entry["score"]["strain"]
        kj = entry["score"].get("kilojoule")
        workout_summary_text = (
            f"Strain: {strain}\n"
            f"Kilojoules: {kj}"
        )

    # Combine into one consolidated "user-friendly" string
    report_text = (
        f"<b>Health Report for {date_str}</b>\n\n"
        f"<b>Sleep</b>\n{sleep_summary_text}\n"
        f"<b>Recovery</b>\n{recovery_summary_text}\n\n"
        f"<b>Workout</b>\n{workout_summary_text}\n"
    )

    #
    # 3) Generate a short LLM analysis based on **full** data (all raw JSON).
    #    That way, the analysis can see everything, not just the limited summary.
    #
    # We'll pass the entire raw records to Gemini so it can see details of each record.
    #

    # Convert each category's data to JSON strings
    sleep_json = json.dumps(sleep_records, indent=2)
    recovery_json = json.dumps(recovery_records, indent=2)
    workout_json = json.dumps(workout_records, indent=2)

    analysis_prompt = HEALTH_REPORT_PROMPT.format(
        date_str=date_str,
        sleep_json=sleep_json,
        recovery_json=recovery_json,
        workout_json=workout_json
    )

    # Use your VertexAI model to generate analysis
    try:
        analysis_response = model.generate_content(analysis_prompt)
        analysis_text = analysis_response.text.strip() if analysis_response.text else "No analysis available."
    except Exception as e:
        logging.error(f"Error generating analysis: {e}")
        analysis_text = "No analysis available (error)."

    # 4) Combine everything into a final message
    final_response = (
        f"{report_text}\n"
        f"<b>Analysis</b>\n{analysis_text}"
    )

    # 5) Store the user's request and your final response in Firestore chat
    user_message = message.text
    store_chat_message(telegram_id, "user", user_message)
    store_chat_message(telegram_id, "assistant", final_response)

    # 6) Reply to the user
    bot.reply_to(message, final_response, parse_mode="HTML")


@bot.message_handler(func=lambda message: True)
def handle_chat(message: types.Message):
    """Default handler that forwards user messages to Gemini for a response"""
    try:
        telegram_id = str(message.from_user.id)
        user_message = message.text
        
        # Get recent chat history
        chat_history = get_chat_history(telegram_id)
        
        # Get today's health data using the same function as /report
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        daily_data = get_daily_health_data_from_firestore(telegram_id, date_str)
        
        # Format health data similar to /report handler
        health_summary = []
        
        # Add sleep summary if available
        if sleep_records := daily_data.get("sleep_records", []):
            entry = sleep_records[0]
            stage_summary = entry["score"]["stage_summary"]
            slow_wave = stage_summary["total_slow_wave_sleep_time_milli"]
            rem = stage_summary["total_rem_sleep_time_milli"]
            health_summary.append(
                f"Sleep - SWS: {millis_to_hhmm(slow_wave)}, "
                f"REM: {millis_to_hhmm(rem)}"
            )
            
        # Add recovery if available
        if recovery_records := daily_data.get("recovery_records", []):
            score = recovery_records[0]["score"]["recovery_score"]
            health_summary.append(f"Recovery Score: {score}")
            
        # Add workout if available
        if workout_records := daily_data.get("workout_records", []):
            strain = workout_records[0]["score"]["strain"]
            health_summary.append(f"Daily Strain: {strain}")
            
        health_data_str = "\n".join(health_summary) if health_summary else "No health data available"
              
        # Add context and system instructions to the prompt
        prompt = SYSTEM_INSTRUCTIONS.format(
                user_name=message.from_user.first_name,
                health_data=health_data_str,
                chat_history=chat_history[-100:] if chat_history else 'No history',
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

@app.post("/scheduled/check-in")
async def scheduled_check_in(background_tasks: BackgroundTasks):
    """Endpoint triggered by Cloud Scheduler every x hours to check users and send proactive messages if appropriate."""
    try:
        # Get all users
        users_ref = db.collection("users")
        users_list = users_ref.stream()

        # Determine today's date string (or pick any date logic you like)
        today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")

        for user_doc in users_list:
            telegram_id = user_doc.id

            user_data = user_doc.to_dict()
            if not user_data:
                continue

            # Retrieve recent chat history
            chat_history = get_chat_history(telegram_id)
            
            # Get today's health data from Firestore subcollection
            daily_data = get_daily_health_data_from_firestore(telegram_id, today_str)
            
            # Convert that raw daily_data into a short summary
            health_summary = summarize_daily_health_data(daily_data)

            # Pass user_data, chat_history, AND the health_summary
            message = generate_proactive_message(user_data, chat_history, health_summary)
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


@app.post("/scheduled/update-health-data")
async def scheduled_update_health_data():
    """
    Endpoint triggered by Cloud Scheduler every hour to 
    keep current day's WHOOP data (sleep, recovery, workout) in Firestore.
    """
    try:
        # Determine today's date string
        today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")

        # Get all users
        users_ref = db.collection("users")
        users_list = users_ref.stream()

        for user_doc in users_list:
            telegram_id = user_doc.id
            user_data = user_doc.to_dict()

            # Skip if the user has no tokens
            if not user_data.get("whoop_access_token"):
                continue

            # Update today's data
            update_daily_health_data(telegram_id, date_str=today_str)

        return {"status": "success", "message": "Daily health data updates completed."}
    except Exception as e:
        logging.error(f"Error in scheduled update: {e}")
        return {"status": "error", "message": str(e)}

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
            "Your WHOOP account is now linked!",
        )

        return {"message": "WHOOP authorization successful! You can close this page."}
    except Exception as e:
        logging.error(f"Error exchanging code for token: {e}")
        return {"error": "Failed to exchange token with WHOOP."}

@app.get("/")
def root():
    return {"message": "Fitness & Health Telegram Bot up and running."}

# (6) HELPER FUNCTIONS

def summarize_daily_health_data(daily_data: dict) -> str:
    """
    Takes the daily_data dictionary (with sleep_records, recovery_records, workout_records)
    and returns a short string summary for prompting.
    """
    summary_parts = []

    # --- Sleep ---
    sleep_records = daily_data.get("sleep_records", [])
    if sleep_records:
        entry = sleep_records[0]  # or loop if you prefer
        stage_summary = entry["score"]["stage_summary"]
        slow_wave = stage_summary["total_slow_wave_sleep_time_milli"]
        rem = stage_summary["total_rem_sleep_time_milli"]
        summary_parts.append(
            f"SWS: {millis_to_hhmm(slow_wave)}, REM: {millis_to_hhmm(rem)}"
        )
    else:
        summary_parts.append("No sleep data")

    # --- Recovery ---
    recovery_records = daily_data.get("recovery_records", [])
    if recovery_records:
        recovery_score = recovery_records[0]["score"]["recovery_score"]
        summary_parts.append(f"Recovery: {recovery_score}")
    else:
        summary_parts.append("No recovery data")

    # --- Workout ---
    workout_records = daily_data.get("workout_records", [])
    if workout_records:
        strain = workout_records[0]["score"]["strain"]
        summary_parts.append(f"Strain: {strain}")
    else:
        summary_parts.append("No workout data")

    return " | ".join(summary_parts)

def generate_proactive_message(
    user_data: dict, 
    chat_history: List[Dict], 
    health_summary: str  # <-- now we receive a processed summary
) -> Optional[str]:
    """
    Generates a contextual message based on user's name, processed health data, and chat history
    """
    # Safely get the user's name
    user_name = user_data.get('name', 'User')

    # Build the system prompt using the summary we created
    prompt = SYSTEM_INSTRUCTIONS.format(
        user_name=user_name,
        health_data=health_summary or "No data",  # fallback
        chat_history=chat_history[-3:] if chat_history else "No history",
        current_message=""
    )

    try:
        response = model.generate_content(prompt)
        return response.text.strip() if response and response.text else None
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

def fetch_whoop_data(telegram_id: str, data_type: str, start_date: str = None) -> dict:
    """
    Fetch WHOOP data (sleep, recovery, workout) from the WHOOP API,
    refreshing the access token if needed.
    
    data_type: one of ["sleep", "recovery", "workout"]
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

    # 2) Determine which WHOOP endpoint to call based on data_type
    #    You can store these in a dict for cleanliness.
    endpoints = {
        "sleep": "activity/sleep",
        "recovery": "recovery",
        "workout": "activity/workout"
    }
    endpoint = endpoints.get(data_type)
    if not endpoint:
        return {"success": False, "error": f"Invalid data type: {data_type}"}

    # 3) Build params
    params = {"limit": 1}
    if start_date:
        params["start_date"] = start_date

    # 4) Attempt the API request
    whoop_data_response = _call_whoop_api(access_token, endpoint, params)

    # 5) If we got a 401 or 403, refresh the token and retry once
    if not whoop_data_response.get("success"):
        if "Unauthorized or Token Expired" in whoop_data_response.get("error", ""):
            logging.info("Access token might be expired, attempting refresh...")
            refreshed_tokens = refresh_whoop_token(refresh_token)
            if refreshed_tokens:
                new_access_token = refreshed_tokens.get("access_token")
                new_refresh_token = refreshed_tokens.get("refresh_token")

                # Store new tokens in Firestore
                db.collection("users").document(telegram_id).set({
                    "whoop_access_token": new_access_token,
                    "whoop_refresh_token": new_refresh_token,
                }, merge=True)

                # Retry the original request
                whoop_data_response = _call_whoop_api(new_access_token, endpoint, params)

    return whoop_data_response

def _call_whoop_api(access_token: str, endpoint: str, params: dict = None) -> dict:
    """
    Makes the actual GET request to WHOOP for a given endpoint.
    Returns a dict with 'success' = True/False to indicate any error states.
    """
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = f"https://api.prod.whoop.com/developer/v1/{endpoint}"
        params = params or {}
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code in (401, 403):
            return {"success": False, "error": "Unauthorized or Token Expired"}

        response.raise_for_status()
        return {"success": True, **response.json()}
    except Exception as e:
        logging.error(f"Error fetching Whoop {endpoint} data: {e}")
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

def update_daily_health_data(telegram_id: str, date_str: str) -> None:
    """
    Fetch WHOOP sleep, recovery, and workout data for the given user 
    (for a particular date), then store/update it in Firestore.
    """
    # 1) Fetch all data types
    sleep_data = fetch_whoop_data(telegram_id, data_type="sleep", start_date=date_str)
    recovery_data = fetch_whoop_data(telegram_id, data_type="recovery", start_date=date_str)
    workout_data = fetch_whoop_data(telegram_id, data_type="workout", start_date=date_str)

    # Check success, but don't bail completely if one fails
    sleep_records = sleep_data.get("records") if sleep_data.get("success") else []
    recovery_records = recovery_data.get("records") if recovery_data.get("success") else []
    workout_records = workout_data.get("records") if workout_data.get("success") else []

    # 2) Prepare data to store
    # You can store raw records or parse them. 
    # For an example, we'll just store them as-is plus a timestamp.
    data_to_store = {
        "sleep_records": sleep_records,
        "recovery_records": recovery_records,
        "workout_records": workout_records,
        "timestamp": datetime.datetime.utcnow()
    }

    # 3) Firestore doc reference
    metrics_doc_ref = (
        db.collection("users")
          .document(telegram_id)
          .collection("health_metrics")
          .document(date_str)  # e.g. "2025-01-10"
    )
    
    # 4) Set/merge data
    metrics_doc_ref.set(data_to_store, merge=True)

    logging.info(f"Updated daily health data for user={telegram_id}, date={date_str}")


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