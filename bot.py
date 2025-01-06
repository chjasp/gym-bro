import os
import json
import uuid
import logging
import requests
import datetime
import threading
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

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

        # Only set up webhook if we're in webhook mode
        if BOT_MODE == "webhook":
            webhook_url = f"{URL}/webhook"
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url)
            logging.info(f"Telegram webhook set to: {webhook_url}")

    except Exception as e:
        logging.error(f"Error during initialization: {e}")
        raise e

    yield
    logging.info("Shutting down...")


app = FastAPI(lifespan=lifespan)
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")


# ---------------------------
# Firestore Data Model
# ---------------------------
class UserProfile(BaseModel):
    telegram_id: str
    whoop_access_token: Optional[str] = None
    psychological_profile: Optional[dict] = None  # e.g., quiz results
    last_motivational_message: Optional[str] = None
    manifesto: Optional[str] = None
    # Additional fields that track user preferences, etc.


# ---------------------------
# Gemini  Helpers
# ---------------------------
def generate_motivational_message(user_profile: dict, context: str = "") -> str:
    """
    Generates a personalized motivational message based on user's manifesto and profile.
    """
    manifesto = user_profile.get("user_data", {}).get("manifesto", "")
    quiz_answers = user_profile.get("user_data", {}).get("quiz_answers", {})
    
    prompt = (
        "You are an intense, no-nonsense motivational coach who delivers powerful, "
        "concise messages that hit hard. Channel the energy of Jordan Peterson, "
        "Jocko Willink, and David Goggins.\n\n"
        f"User's personal manifesto: {manifesto}\n"
        f"User's quiz answers: {quiz_answers}\n"
        f"Context: {context}\n\n"
        "Create a short, high-impact motivational message (max 2-3 sentences) that:\n"
        "1. Uses powerful, decisive language\n"
        "2. Creates urgency and intensity\n"
        "3. Incorporates their manifesto themes if available\n"
        "4. Pushes them beyond their comfort zone\n\n"
        "The message should feel like a warrior's battle cry, not gentle encouragement."
    )
    
    try:
        generation_config = {"temperature": 2}
        response = model.generate_content(prompt, generation_config=generation_config)
        message = response.text.strip()
    except Exception as e:
        logging.error(f"Error generating motivational message: {e}")
        message = "WAKE UP WARRIOR! Your greatness awaits. NO EXCUSES! 💪"
    
    return message


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
# Quizzes (Psychological Profiling)
# ---------------------------
def store_quiz_answer(telegram_id: str, question_id: str, answer: str):
    """
    Stores the user's answer to a quiz question in Firestore.
    We can later build a psychological profile from their answers.
    """
    try:
        user_doc_ref = db.collection("users").document(telegram_id)
        user_doc_ref.set({f"quiz_answers.{question_id}": answer}, merge=True)
    except Exception as e:
        logging.error(f"Error storing quiz answer: {e}")


QUIZ_QUESTIONS = [
    {"id": "q1", "text": "What do you fear most? A) Illness or B) Not fitting in?"},
    {
        "id": "q2",
        "text": "What motivates you more? A) Achieving success or B) Avoiding failure?",
    },
    # Add more questions as needed
]


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
    # Ensure user doc in Firestore
    user_doc_ref = db.collection("users").document(telegram_id)
    if not user_doc_ref.get().exists:
        user_profile = UserProfile(telegram_id=telegram_id).dict()
        user_doc_ref.set(user_profile)

    welcome_text = (
        "Welcome to your Personal Fitness & Health Bot!\n\n"
        "I can remind you about workouts, healthy eating habits, and more. "
        "First, consider linking your Whoop account so I can read your health data. "
        "Use /linkwhoop to provide your access token.\n\n"
        "You can also start a short quiz to help me understand you better with /quiz.\n\n"
        "Need instant motivation? Just type /motivateme and I'll provide personalized encouragement!"
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


@bot.message_handler(commands=["manifesto"])
def handle_manifesto(message: types.Message):
    """
    /manifesto command handler. Expects format: /manifesto Your manifesto text here
    Stores the user's personal manifesto in their profile.
    """
    telegram_id = str(message.from_user.id)
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        bot.reply_to(
            message,
            "Please provide your manifesto text after the command. Example:\n/manifesto I want to become the strongest version of myself.",
        )
        return

    manifesto = parts[1].strip()

    try:
        # Store manifesto in user profile
        user_doc_ref = db.collection("users").document(telegram_id)
        user_doc_ref.set({"manifesto": manifesto}, merge=True)
        bot.reply_to(
            message,
            "Your manifesto has been saved! I'll use this to provide more personalized motivation.",
        )
    except Exception as e:
        logging.error(f"Error storing manifesto: {e}")
        bot.reply_to(message, "Sorry, there was an error saving your manifesto.")


@bot.message_handler(commands=["quiz"])
def handle_quiz(message: types.Message):
    """
    /quiz command handler. Sends out a quiz question.
    """
    telegram_id = str(message.from_user.id)

    # Find next question to ask (this is simplistic; you can expand logic).
    user_doc_ref = db.collection("users").document(telegram_id).get()
    user_data = user_doc_ref.to_dict() if user_doc_ref.exists else {}

    answered_questions = user_data.get("quiz_answers", {})
    next_question = None
    for q in QUIZ_QUESTIONS:
        if q["id"] not in answered_questions:
            next_question = q
            break

    if not next_question:
        bot.reply_to(message, "You've answered all quiz questions! Thank you.")
        return

    # Send next question
    bot.reply_to(
        message,
        f"Quiz question: {next_question['text']} "
        f"\nPlease answer using /answer {next_question['id']} <your_answer>",
    )


@bot.message_handler(commands=["answer"])
def handle_quiz_answer(message: types.Message):
    """
    /answer command handler. Expects format: /answer q1 A
    """
    telegram_id = str(message.from_user.id)
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(
            message, "Please answer in the format /answer <question_id> <answer>"
        )
        return

    question_id = parts[1].strip()
    answer = parts[2].strip()

    # Store the answer
    store_quiz_answer(telegram_id, question_id, answer)
    bot.reply_to(message, f"Saved your answer for {question_id}: {answer}")


@bot.message_handler(commands=["motivateme"])
def handle_motivate_me(message: types.Message):
    """
    /motivateme command handler. Fetches user's manifesto and generates
    a personalized motivational message.
    """
    telegram_id = str(message.from_user.id)
    user_doc_ref = db.collection("users").document(telegram_id).get()
    if not user_doc_ref.exists:
        bot.reply_to(message, "Please /start first.")
        return

    user_data = user_doc_ref.to_dict()
    
    # Only pass relevant data to the generation function
    profile = {
        "user_data": {
            "manifesto": user_data.get("manifesto"),
            "quiz_answers": user_data.get("quiz_answers", {})
        }
    }

    # Generate motivational message
    motivational_message = generate_motivational_message(
        profile, context="Immediate request from user."
    )
    bot.reply_to(message, motivational_message)


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


# ---------------------------
# Evening Overeating Prevention
# ---------------------------
def send_evening_prevention_message():
    """
    Send a message to each user who might be at risk of overeating in the evening.
    For Cloud Run, you may call this via a Cloud Scheduler job that hits the /prevent_overeat endpoint.
    """
    now = datetime.datetime.now()
    # Example criterion: if it's between 7pm and 10pm local time
    # (In production, you'd want to handle timezones properly.)
    if 19 <= now.hour < 22:
        # Fetch all users
        all_users = db.collection("users").stream()
        for user_snapshot in all_users:
            user_data = user_snapshot.to_dict()
            telegram_id = user_data.get("telegram_id")
            if not telegram_id:
                continue

            # You might refine the logic to check user_data or whoop_data
            # for cues of overeating or other triggers, but here we just send a reminder.
            combined_profile = {"user_data": user_data}
            message = generate_motivational_message(
                combined_profile,
                context="User is prone to evening overeating. Provide targeted prevention tips.",
            )
            try:
                bot.send_message(telegram_id, message)
            except Exception as e:
                logging.error(f"Error sending prevention message to {telegram_id}: {e}")


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
        bot.process_new_updates([telebot.types.Update.de_json(update)])
    except Exception as e:
        logging.error(f"Telegram webhook error: {e}")
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


@app.get("/prevent_overeat")
async def prevent_overeat_endpoint(background_tasks: BackgroundTasks):
    """
    Endpoint that can be invoked by Cloud Scheduler in the evening hours to push
    anti-overeating messages to all users. Example: schedule it for 7pm daily.
    """
    background_tasks.add_task(send_evening_prevention_message)
    return {"status": "scheduled"}


@app.get("/")
def root():
    return {"message": "Fitness & Health Telegram Bot up and running."}


# ---------------------------
# Gunicorn / Uvicorn Entrypoint
# ---------------------------
# In Cloud Run, you'd typically configure your container to call:
#   uvicorn main:app --host 0.0.0.0 --port 8080
# We provide if __name__ == "__main__": block for local runs/testing.
if __name__ == "__main__":
    if BOT_MODE == "polling":
        # Start bot polling in a separate thread
        polling_thread = threading.Thread(target=bot.infinity_polling)
        polling_thread.start()
        logging.info("Started bot polling")

    # Start the FastAPI server
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8080)))
