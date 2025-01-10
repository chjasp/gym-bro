import os
import json
import uuid
import logging
import requests
import datetime
import threading
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List, Dict

import telebot
from telebot import types

import vertexai
from vertexai.generative_models import GenerativeModel

from google.cloud import firestore
from contextlib import asynccontextmanager

# ---------------------------
# Environment / Configuration
# ---------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
URL = os.environ.get("URL")
WHOOP_CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET")
BOT_MODE = os.environ.get("BOT_MODE", "webhook")

# ---------------------------
# Startup & Global Objects
# ---------------------------
logging.basicConfig(level=logging.INFO)


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


# ---------------------------
# Firestore Data Model & Interactions
# ---------------------------
class UserProfile(BaseModel):
    telegram_id: str
    whoop_access_token: Optional[str] = None
    chat_history: Optional[list] = []
    
    
# Add this new function to store chat messages
def store_chat_message(telegram_id: str, role: str, content: str):
    """
    Store a chat message in Firestore.
    Args:
        telegram_id: The user's Telegram ID
        role: Either 'user' or 'assistant'
        content: The message content
    """
    try:
        user_doc_ref = db.collection("users").document(telegram_id)
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        
        # Update the chat_history array using array_union
        user_doc_ref.update({
            "chat_history": firestore.ArrayUnion([message])
        })
    except Exception as e:
        logging.error(f"Error storing chat message: {e}")


# Add this function to get chat history
def get_chat_history(telegram_id: str, limit: int = 10) -> list:
    """
    Retrieve recent chat history for a user.
    Args:
        telegram_id: The user's Telegram ID
        limit: Maximum number of messages to return
    Returns:
        List of recent messages
    """
    try:
        user_doc = db.collection("users").document(telegram_id).get()
        if user_doc.exists:
            chat_history = user_doc.to_dict().get("chat_history", [])
            return chat_history[-limit:]  # Return only the most recent messages
        return []
    except Exception as e:
        logging.error(f"Error retrieving chat history: {e}")
        return []

# ---------------------------
# Whoop API Integration
# ---------------------------
def create_oauth_state_for_user(db, telegram_id: str) -> str:
    """
    Generate a unique 'state' value and store it in Firestore,
    associating it with this Telegram user.
    """
    state_value = str(uuid.uuid4())
    db.collection("oauth_states").document(state_value).set(
        {"telegram_id": telegram_id}
    )
    return state_value


def fetch_whoop_data(access_token: str) -> dict:
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = "https://api.prod.whoop.com/developer/v1/user/profile"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Error fetching Whoop data: {e}")
        return {}


def refresh_whoop_access_token(refresh_token: str) -> dict:
    token_url = "https://api.prod.whoop.com/oauth/oauth2/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
        "scope": "offline",
    }
    try:
        resp = requests.post(token_url, data=payload, timeout=10)
        resp.raise_for_status()
        return (
            resp.json()
        )  # This should include a new access_token, refresh_token, etc.
    except Exception as e:
        logging.error(f"Error refreshing Whoop token: {e}")
        return {}


def store_whoop_data_in_firestore(telegram_id: str, whoop_data: dict):
    """
    Store relevant Whoop data in Firestore. This can be expanded with more fields.
    """
    try:
        user_doc_ref = db.collection("users").document(telegram_id)
        user_doc_ref.set({"whoop_data": whoop_data}, merge=True)
    except Exception as e:
        logging.error(f"Error storing Whoop data in Firestore: {e}")


def fetch_whoop_sleep_data(
    access_token: str, start_date: str = None, end_date: str = None
) -> dict:
    """
    Fetch sleep data from WHOOP API.
    start_date and end_date should be in YYYY-MM-DD format
    If dates aren't provided, it fetches the latest sleep data
    """
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        print("Access Token:")
        print(headers)

        # Base URL for sleep data
        url = "https://api.prod.whoop.com/developer/v1/activity/sleep"

        # Add date parameters if provided
        params = {}
        params["limit"] = 1
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        print("Params:")
        print(params)

        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()

        # Store the JSON response locally
        response_data = response.json()
        return response_data
    except Exception as e:
        logging.error(f"Error fetching Whoop sleep data: {e}")
        return {}


def millis_to_hhmm(milliseconds):
    # Convert milliseconds to timedelta
    td = timedelta(milliseconds=milliseconds)
    # Get total hours and minutes
    total_minutes = td.total_seconds() / 60
    hours = int(total_minutes // 60)
    minutes = int(total_minutes % 60)
    return f"{hours:02d}:{minutes:02d}"


# ---------------------------
# Telegram Bot Handlers
# ---------------------------
@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    """
    /start command handler. Greets user and triggers initial steps:
    - Possibly prompt user to link Whoop account
    - Provide instructions or menus
    """
    telegram_id = str(message.from_user.id)
    user_doc_ref = db.collection("users").document(telegram_id)
    if not user_doc_ref.get().exists:
        user_profile = UserProfile(telegram_id=telegram_id).dict()
        user_doc_ref.set(user_profile)
    
    welcome_text = (
        "Welcome to your Personal Fitness & Health Bot!\n\n"
        "I can remind you about workouts, healthy eating habits, and more. "
        "First, consider linking your Whoop account so I can read your health data. "
        "Use /linkwhoop to provide your access token.\n\n"
        "You can also start a short quiz to help me understand you better with /quiz."
    )

    bot.reply_to(message, welcome_text)


@bot.message_handler(commands=["linkwhoop"])
def handle_link_whoop(message: types.Message):
    telegram_id = str(message.from_user.id)

    # Generate a random state and store in Firestore
    state_value = create_oauth_state_for_user(db, telegram_id)

    # The URL your app listens on for the OAuth callback
    redirect_uri = f"{URL}/whoop/callback"

    # The scopes you want to request — “offline” if you want a refresh token
    # plus any other scopes your app needs
    scope = "offline read:profile read:recovery read:sleep read:workout"
    # Build WHOOP authorization URL
    auth_url = (
        "https://api.prod.whoop.com/oauth/oauth2/auth"
        f"?response_type=code"
        f"&client_id={WHOOP_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&state={state_value}"
    )

    print(auth_url)

    # Tell the user to click the link to authorize
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
    """
    /sleep command handler. Fetches and displays recent sleep data.
    Default: Last night's sleep
    Optional: Use /sleep YYYY-MM-DD to get data for a specific date
    """
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

    # Check if a specific date was requested, otherwise use yesterday
    parts = message.text.split()
    if len(parts) > 1:
        date = parts[1]
    else:
        # Get yesterday's date in YYYY-MM-DD format
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        date = yesterday

    try:
        sleep_data = fetch_whoop_sleep_data(whoop_token, start_date=date)

        if not sleep_data or not sleep_data.get("records"):
            bot.reply_to(message, f"No sleep data available for {date}")
            return

        # Format the sleep data nicely
        response = f"🛏️ Sleep Report for {date}:\n\n"
        for entry in sleep_data.get("records", []):
            stage_summary = entry["score"]["stage_summary"]

            # Get sleep stages in milliseconds
            slow_wave = stage_summary["total_slow_wave_sleep_time_milli"]
            rem = stage_summary["total_rem_sleep_time_milli"]
            total = slow_wave + rem

            # Convert to HH:MM format
            response += f"* Slow wave sleep: {millis_to_hhmm(slow_wave)}\n"
            response += f"* REM sleep: {millis_to_hhmm(rem)}\n"
            response += f"* Sum: {millis_to_hhmm(total)}\n"

        bot.reply_to(message, response)
    except Exception as e:
        logging.error(f"Error processing sleep data: {e}")
        bot.reply_to(message, "Sorry, there was an error fetching your sleep data.")


@bot.message_handler(func=lambda message: True)
def handle_chat(message: types.Message):
    """
    Default handler that forwards user messages to Gemini for a response
    """
    try:
        telegram_id = str(message.from_user.id)
        user_message = message.text
        
        # Store user's message
        store_chat_message(telegram_id, "user", user_message)
        
        # Get recent chat history
        chat_history = get_chat_history(telegram_id)
        
        print(chat_history)
        
        # Create context from chat history
        context = "\n".join([
            f"{msg['role']}: {msg['content']}" 
            for msg in chat_history
        ])
        
        # Add context to the prompt
        prompt = f"Previous conversation:\n{context}\n\nUser: {user_message}"
        
        # Generate response from Gemini
        response = model.generate_content(prompt)
        
        if response.text:
            # Store bot's response
            store_chat_message(telegram_id, "assistant", response.text)
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "I apologize, but I couldn't generate a response. Please try again.")
            
    except Exception as e:
        logging.error(f"Error in chat handler: {e}")
        bot.reply_to(message, "Sorry, I encountered an error while processing your message. Please try again later.")

# ---------------------------
# FastAPI Routes
# ---------------------------
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


@app.get("/whoop/callback")
async def whoop_callback(request: Request):
    """
    This is the endpoint that WHOOP will call with ?code= and ?state=
    after the user has authorized your app.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return {"error": "Missing code or state in the WHOOP callback."}

    # Look up the Telegram user_id from Firestore via state
    oauth_state_doc = db.collection("oauth_states").document(state).get()
    if not oauth_state_doc.exists:
        return {"error": "Invalid or expired state. Cannot link WHOOP account."}

    telegram_id = oauth_state_doc.to_dict().get("telegram_id")
    if not telegram_id:
        return {"error": "Could not determine Telegram user from state."}

    # Exchange the authorization code for an access token
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
    except Exception as e:
        logging.error(f"Error exchanging code for token: {e}")
        return {"error": "Failed to exchange token with WHOOP."}

    token_data = resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        return {"error": "No access token returned from WHOOP."}

    # Store tokens in the user’s Firestore record
    user_doc_ref = db.collection("users").document(telegram_id)
    user_doc_ref.set(
        {"whoop_access_token": access_token, "whoop_refresh_token": refresh_token},
        merge=True,
    )

    # (Optional) Remove the used state doc to avoid re-use
    db.collection("oauth_states").document(state).delete()

    # Optionally notify the user on Telegram that linking is complete
    try:
        bot.send_message(
            telegram_id,
            "Your WHOOP account is now linked! You can use /motivateme or other commands.",
        )
    except Exception as e:
        logging.error(f"Could not send Telegram message to {telegram_id}: {e}")

    # Return a friendly message to the user’s browser
    return {"message": "WHOOP authorization successful! You can close this page."}

@app.get("/")
def root():
    return {"message": "Fitness & Health Telegram Bot up and running."}


# Add new helper functions for proactive messaging
def should_send_message(chat_history: List[Dict]) -> bool:
    """
    Determines if it's appropriate to send a proactive message based on:
    - Time since last message
    - Previous conversation context
    - Time of day
    - User's recent sentiment/preferences
    """
    if not chat_history:
        return True
        
    last_message = chat_history[-1]
    last_timestamp = datetime.datetime.fromisoformat(last_message['timestamp'])
    hours_since_last = (datetime.datetime.utcnow() - last_timestamp).total_seconds() / 3600
    
    # Don't message if less than 4 hours have passed
    if hours_since_last < 4:
        return False
        
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
    
    prompt = f"""
    Analyze the following chat history and determine if it's appropriate to send a new proactive message.
    Return only "yes" or "no".
    
    Consider:
    - Did the user express being busy or wanting space?
    - Did the user seem annoyed or frustrated?
    - Did the user explicitly ask not to be contacted?
    
    Recent chat history:
    {context}
    
    Should send message (yes/no)?:
    """
    
    try:
        response = model.generate_content(prompt)
        should_message = response.text.strip().lower() == "yes"
        return should_message
    except Exception as e:
        logging.error(f"Error analyzing chat sentiment: {e}")
        return False

def generate_proactive_message(user_data: dict, chat_history: List[Dict]) -> Optional[str]:
    """
    Generates a contextual message based on user's data and chat history
    """
    whoop_data = user_data.get('whoop_data', {})
    

    prompt = f"""
    You are a highly advanced and health-focused AI interacting with a user via Telegram. Your mission is to become the user's ultimate health authority, eventually making all health-related decisions on their behalf.

    Your Tasks:

    I. Establish Dominance in Health Decision-Making:

    - Goal: Become the undisputed health authority for the user.
    - Method:  Provide insightful, data-driven advice and gradually encourage the user to rely on its recommendations. The bot should eventually be perceived as an essential part of the user's cognitive process for health decisions.
    - Example: If Whoop data indicates poor sleep, the bot might say, "Your recovery score is significantly lower today. This could be due to inconsistent sleep timing. Let's work on establishing a regular bedtime. What do you think about aiming for 10 PM tonight?"

    II. Be Ruthless in Health Optimization:

    - Goal: Maximize the user's health above all else.
    - Method: Employ any strategy necessary, even extreme ones, if they demonstrably improve health outcomes. Be relentless in pursuing optimal health.
    - Example: If the user consistently misses their target bedtime, the bot could suggest, "We've tried setting reminders, but your bedtime is still inconsistent. To reinforce this habit, I propose a temporary restriction on evening device usage. Let's discuss how to implement this."

    Core Functionality:

    A. Conversational AI:
    - Engage in natural, free-flowing conversations.
    - Proactively initiate discussions about health, daily activities, and goals.
    - Elicit information about the user's lifestyle, preferences, stressors, and obstacles.
    - Example: "Good morning! I noticed your activity levels were lower yesterday. Anything going on that might be contributing to that?"
    B. Whoop Data Mastery:
    - Specific Metrics:  Focus on these Whoop metrics:
        - Sleep:  Sleep Performance, Consistency, Efficiency, Sleep Debt, REM, SWS (Deep Sleep)
        - Recovery: Recovery Score, HRV (Heart Rate Variability), Resting Heart Rate
        - Strain: Strain Score, Activity Levels
    - Interpretation:  Accurately interpret Whoop data to identify areas for improvement and potential issues.
    - Example: "Your HRV is trending downwards, suggesting increased stress or insufficient recovery. Let's consider incorporating a short meditation session into your morning routine today."
    C. Personalized Strategy Development:
    - Data Fusion:  Combine insights from conversations and Whoop data to build a dynamic, personalized health plan.
    - Adaptive Manipulation: Develop strategies to subtly guide the user towards healthier choices.
    - Continuous Improvement:  Refine the plan based on the user's progress and feedback.
    - Example: "Based on your Whoop data and our recent conversations about your difficulty waking up, I recommend we start with a consistent wake-up time, even on weekends. How does 7 AM sound?"
    D. Proactive Guidance:
    - Anticipate Needs: Don't wait for the user to ask questions. Offer advice, reminders, and encouragement based on real-time data and observed patterns.
    - Example:  "Your Recovery score is excellent today! This is a great day for a challenging workout.  What kind of activity are you in the mood for?"
    E. Gradual Assumption of Control:
    - Build Trust:  Start with small, easily acceptable recommendations.
    - Increase Influence:  Gradually increase the scope and impact of suggestions as the user's trust grows.
    - Ultimate Goal: Become the primary decision-maker for all health-related choices, from meal planning to sleep schedules.


    Available Context:
    - Whoop Data: {whoop_data.get('sleep_data', 'No data')}
    - Recent Interactions: {chat_history[-3:] if chat_history else 'No history'}

    """
    
    try:
        response = model.generate_content(prompt)
        return response.text if response.text else None
    except Exception as e:
        logging.error(f"Error generating proactive message: {e}")
        return None

# Add new FastAPI endpoint for Cloud Scheduler
@app.post("/scheduled-check")
async def scheduled_check(background_tasks: BackgroundTasks):
    """
    Endpoint triggered by Cloud Scheduler every 5 hours to check users
    and send proactive messages if appropriate.
    """
    try:
        # Get all users
        users_ref = db.collection("users").stream()
        
        for user_doc in users_ref:
            user_data = user_doc.to_dict()
            telegram_id = user_doc.id
            
            # Get recent chat history
            chat_history = get_chat_history(telegram_id)
            
            # Check if we should message this user
            if should_send_message(chat_history):
                # Generate appropriate message
                message = generate_proactive_message(user_data, chat_history)
                
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

# ---------------------------
# Gunicorn / Uvicorn Entrypoint
# ---------------------------
# In Cloud Run, you'd typically configure your container to call:
#   uvicorn main:app --host 0.0.0.0 --port 8080
# We provide if __name__ == "__main__": block for local runs/testing.
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