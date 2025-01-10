import os
import logging
from fastapi import FastAPI, Request, BackgroundTasks
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import telebot
import vertexai

from database import Database
from ai_service import AIService
from bot_handlers import BotHandlers

load_dotenv()

# Environment variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
# ... other env vars ...

# FastAPI setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, ai_service, bot_handlers

    try:
        # Initialize services
        db = Database(GCP_PROJECT_ID)
        
        vertexai.init(project=GCP_PROJECT_ID, location="us-central1")
        ai_service = AIService("gemini-exp-1206")
        
        bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
        bot_handlers = BotHandlers(bot, db, ai_service)
        bot_handlers.register_handlers()

        if BOT_MODE == "webhook":
            webhook_url = f"{URL}/webhook"
            bot.set_webhook(url=webhook_url)

    except Exception as e:
        logging.error(f"Error during initialization: {e}")
        raise e

    yield

    logging.info("Shutting down...")

app = FastAPI(lifespan=lifespan)

# FastAPI routes
@app.post("/webhook")
async def telegram_webhook(request: Request):
    # ... existing webhook logic ...
    pass

@app.post("/scheduled-check")
async def scheduled_check(background_tasks: BackgroundTasks):
    # ... existing scheduled check logic ...
    pass