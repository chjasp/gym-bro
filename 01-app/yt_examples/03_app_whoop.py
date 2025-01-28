import os
import uuid
import requests
import telebot
import google.generativeai as genai
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import threading
import uvicorn
from dotenv import load_dotenv

load_dotenv()

# --- Configuration and Initialization ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
URL = os.getenv("URL")
WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
app = FastAPI()

USER_TOKENS = {}
OAUTH_STATES = {}

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-exp-1206")


# --- Telegram Bot Command Handlers ---
@bot.message_handler(commands=["linkwhoop"])
def handle_link_whoop(message):
    """
    Initiate WHOOP OAuth by sending the user an authorization URL.
    """
    telegram_id = str(message.from_user.id)

    # Generate a random "state" and map it to this user
    state_value = str(uuid.uuid4())
    OAUTH_STATES[state_value] = telegram_id

    # WHOOP OAuth URL
    redirect_uri = f"{URL}/whoop/callback"
    scope = "read:sleep"

    auth_url = (
        "https://api.prod.whoop.com/oauth/oauth2/auth"
        f"?response_type=code"
        f"&client_id={WHOOP_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&state={state_value}"
    )

    msg = (
        "Click the link below to authorize WHOOP:\n"
        f"<a href='{auth_url}'>Authorize WHOOP</a>\n\n"
        "You will be redirected, and I'll store your token."
    )
    bot.reply_to(message, msg)


@bot.message_handler(commands=["sleep"])
def handle_sleep(message):
    """
    Retrieve and analyze the user's most recent sleep data from WHOOP using Gemini.
    """
    telegram_id = str(message.from_user.id)

    # Check if we have a token for this user
    tokens = USER_TOKENS.get(telegram_id)
    if not tokens or not tokens.get("access_token"):
        bot.reply_to(message, "You have not linked WHOOP yet. Please use /linkwhoop.")
        return

    # Fetch sleep data from WHOOP
    data = fetch_whoop_sleep_data(tokens["access_token"])
    if not data.get("success"):
        bot.reply_to(message, f"Error retrieving sleep data: {data.get("error")}")
        return

    records = data.get("records", [])
    if not records:
        bot.reply_to(message, "No recent sleep records found.")
        return

    # Prepare the most recent sleep record for analysis
    first_record = records[0]

    # Extract fields from the WHOOP response
    in_bed_milli = first_record["score"]["stage_summary"]["total_in_bed_time_milli"]
    sleep_efficiency = first_record["score"]["sleep_efficiency_percentage"]
    sleep_cycles = first_record["score"]["stage_summary"]["sleep_cycle_count"]
    sw_sleep_milli = first_record["score"]["stage_summary"]["total_slow_wave_sleep_time_milli"]

    # Convert times from milliseconds
    in_bed_hours = in_bed_milli / 3600000
    sw_sleep_hours = sw_sleep_milli / 3600000

    # Build the prompt for Gemini
    prompt = f"""
    Please analyze this sleep data and provide insights:
    - Time in bed: {in_bed_hours:.2f} hours
    - Sleep efficiency: {sleep_efficiency:.2f}%
    - Sleep cycles: {sleep_cycles}
    - Slow wave sleep: {sw_sleep_hours:.2f} hours
    
    Include any notable patterns and recommendations for improvement.
    """

    print(prompt)

    try:
        # Get Gemini's analysis
        response = model.generate_content(prompt)
        if response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "Sorry, I couldn't analyze your sleep data. Please try again.")
    except Exception as e:
        print(f"Error getting Gemini analysis: {e}")
        bot.reply_to(message, "Sorry, an error occurred while analyzing your sleep data.")


@bot.message_handler(func=lambda message: True)
def handle_chat(message):
    """
    Any non-command text is passed to Gemini for an AI-generated response.
    """
    user_text = message.text
    try:
        response = model.generate_content(user_text)
        if response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "I'm not sure how to respond. Please try again.")
    except Exception as e:
        print(f"Error: {e}")
        bot.reply_to(message, "Sorry, an error occurred while generating a response.")


# --- FastAPI Endpoints ---
@app.get("/whoop/callback")
def whoop_callback(code: str, state: str):
    """
    WHOOP redirects here after the user grants permission.
    We exchange the code for an access_token and store it.
    """
    telegram_id = OAUTH_STATES.get(state)
    if not telegram_id:
        return JSONResponse(status_code=400, content={"error": "Invalid OAuth state."})

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

        # Store the tokens in memory (or DB in real app)
        USER_TOKENS[telegram_id] = {
            "access_token": token_data.get("access_token")
        }

        # Let the user know we're done
        bot.send_message(telegram_id, "Your WHOOP account is now linked! Try /sleep.")

        # Cleanup
        del OAUTH_STATES[state]
        return {"message": "WHOOP authorization successful. You can close this page."}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


def fetch_whoop_sleep_data(access_token: str):
    """Fetch the most recent WHOOP sleep record"""
    url = "https://api.prod.whoop.com/developer/v1/activity/sleep"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code in (401, 403):
            return {"success": False, "error": "unauthorized"}
            
        response.raise_for_status()
        return {"success": True, **response.json()}
        
    except requests.RequestException as e:
        return {"success": False, "error": str(e)}


# --- Main Entry Point ---
if __name__ == "__main__":
    bot.remove_webhook()
    
    # Start bot polling in a separate thread
    polling_thread = threading.Thread(target=bot.infinity_polling)
    polling_thread.start()

    # Start the FastAPI server
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8080)))
