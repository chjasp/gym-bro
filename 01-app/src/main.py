# Standard library imports
import os
import logging
import datetime
import threading
from datetime import timedelta
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
import uuid

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

from .templates import (
    WELCOME_TEXT,
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
            "direction": "incoming" if role == "user" else "outgoing"
        })
    except Exception as e:
        logging.error(f"Error storing chat message for user {telegram_id}: {e}")
        raise

def get_chat_history(telegram_id: str, limit: int = 10) -> list:
    """Retrieve recent chat history from the new data structure."""
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
                "role": "user" if chat_data["direction"] == "incoming" else "assistant",
                "content": chat_data["content"],
                "timestamp": chat_data["timestamp"].isoformat()
            })
        
        # Reverse to get chronological order
        return list(reversed(messages))
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
        profile_ref = db.collection("users").document(telegram_id)\
                       .collection("profile").document("data")
        
        if not profile_ref.get().exists:
            profile_ref.set({
                "telegram_id": telegram_id,
                "name": message.from_user.first_name,
                "joined_at": datetime.datetime.utcnow()
            })
        
        bot.reply_to(message, WELCOME_TEXT)
        
    except Exception as e:
        logging.error(f"Error in start handler for user {telegram_id}: {e}")
        bot.reply_to(message, "Sorry, I encountered an error while setting up your profile. Please try again later.")

@bot.message_handler(func=lambda message: True)
def handle_chat(message: types.Message):
    """Default handler that forwards user messages to Gemini for a response"""
    try:
        telegram_id = str(message.from_user.id)
        user_message = message.text
        
        # Store user's message
        store_chat_message(telegram_id, "user", user_message)
        
        # Get recent chat history
        chat_history = get_chat_history(telegram_id)
        
        # Get health data
        health_data = get_health_data(telegram_id)
              
        # Add context and system instructions to the prompt
        prompt = SYSTEM_INSTRUCTIONS.format(
                health_data=health_data.get('sleep_data', 'No data'),
                chat_history=chat_history[-3:] if chat_history else 'No history'
            )
        
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

@app.get("/")
def root():
    return {"message": "Fitness & Health Telegram Bot up and running."}


# (6) HELPER FUNCTIONS

def should_send_message(chat_history: List[Dict]) -> bool:
    """Determines if it's appropriate to send a proactive message"""
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
    
    prompt = SHOULD_SEND_MESSAGE_PROMPT.format(context=context)
    
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
    health_data = user_data.get('health_data', {})
    
    prompt = SYSTEM_INSTRUCTIONS.format(
        health_data=health_data.get('sleep_data', 'No data'),
        chat_history=chat_history[-3:] if chat_history else 'No history'
    )
    
    try:
        response = model.generate_content(prompt)
        return response.text if response.text else None
    except Exception as e:
        logging.error(f"Error generating proactive message: {e}")
        return None


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