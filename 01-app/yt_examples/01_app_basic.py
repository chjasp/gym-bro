import os
import telebot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")


@bot.message_handler(func=lambda message: True)
def greet_user(message):
    bot.reply_to(message, "Hi, how are you?")


if __name__ == "__main__":
    bot.infinity_polling()
