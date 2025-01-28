import os
import telebot
import google.generativeai as genai

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-exp-1206")

SYSTEM_PROMPT = "You are a helpful Telegram bot assistant. Provide clear and concise responses."

@bot.message_handler(func=lambda message: True)
def handle_chat(message):
    user_text = message.text
    
    # Combine system prompt with user message
    full_prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_text}"

    try:
        response = model.generate_content(full_prompt)

        if response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "Sorry, I couldn't generate a response.")

    except Exception as e:
        print(f"Error communicating with the model: {e}")
        bot.reply_to(message, "An error occurred while processing your request.")

if __name__ == "__main__":
    bot.infinity_polling()
