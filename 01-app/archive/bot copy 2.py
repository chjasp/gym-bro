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