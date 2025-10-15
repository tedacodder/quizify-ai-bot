import os
import logging
import re
import random
import asyncio
import httpx
from threading import Thread
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ================= CONFIG =================
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_AI_KEY = os.getenv("GOOGLE_AI_KEY")
PORT = int(os.environ.get("PORT", 8080))  # Koyeb health check port

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ================= FLASK HEALTH CHECK =================
flask_app = Flask(__name__)

@flask_app.route("/")
def health_check():
    return "Bot is running ✅"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# Start Flask server in a separate thread
Thread(target=run_flask).start()

# ================= GOOGLE AI HELPER =================
async def ask_google_ai(prompt: str, retries: int = 2, timeout_seconds: int = 40) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GOOGLE_AI_KEY}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for attempt in range(1, retries + 2):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                if text:
                    return text.strip()
                else:
                    logger.warning("Google AI returned empty text.")
                    return ""
        except Exception as e:
            logger.warning(f"Google AI attempt {attempt} failed: {e}")
            if attempt <= retries:
                await asyncio.sleep(1 + attempt)
            else:
                logger.error("All Google AI attempts failed.")
                return "❌ AI service is unavailable right now. Please try again later."

# ================= QUIZ GENERATION =================
async def generate_quiz(topic: str):
    prompt = (
        f"Create one multiple-choice quiz question about {topic}. "
        "Return the question, four distinct options labeled A–D, and indicate the correct one.\n"
        "Format example:\n"
        "Question: <text>\nA: option A\nB: option B\nC: option C\nD: option D\nCorrect: B\n"
    )
    response = await ask_google_ai(prompt)
    lines = [ln.strip() for ln in response.splitlines() if ln.strip()]

    # Extract question
    question = next((re.sub(r'(?i)^question[:\-\s]*', '', line).strip() 
                     for line in lines if re.match(r"(?i)^question[:\-\s]", line)), None)
    if not question:
        question = next((line for line in lines if "?" in line), f"What is correct about {topic}?")

    # Extract options
    options = []
    opt_pattern = re.compile(r"^[A-D][\).:\-]\s*(.+)$", re.IGNORECASE)
    for line in lines:
        m = opt_pattern.match(line)
        if m:
            options.append(m.group(1).strip())
        if len(options) == 4:
            break
    # Looser matching if needed
    if len(options) < 4:
        for line in lines:
            m = re.match(r"^([A-D])\s+(.+)$", line, re.IGNORECASE)
            if m and m.group(2).strip() not in options:
                options.append(m.group(2).strip())
            if len(options) == 4:
                break
    while len(options) < 4:
        options.append(f"{topic} Option {chr(65 + len(options))}")

    # Correct answer
    correct_letter = None
    for line in lines:
        m = re.search(r"correct[:\s\-]*([A-D])", line, re.IGNORECASE)
        if m:
            correct_letter = m.group(1).upper()
            break
    if not correct_letter:
        for line in lines:
            m = re.search(r"answer[:\s\-]*([A-D])", line, re.IGNORECASE)
            if m:
                correct_letter = m.group(1).upper()
                break
    correct_index = ord(correct_letter)-65 if correct_letter and 'A' <= correct_letter <= 'D' else 0

    # Shuffle options
    indexed = list(enumerate(options))
    random.shuffle(indexed)
    shuffled_options = [text for (i, text) in indexed]
    new_correct = next((i for i, (orig, _) in enumerate(indexed) if orig == correct_index), 0)

    return question, shuffled_options, new_correct

# ================= QUIZ SENDER =================
async def send_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE, topic: str, count: int):
    total_sent = 0
    while total_sent < count:
        question, options, correct = await generate_quiz(topic)
        safe_question = question[:300]
        safe_options = [opt[:100] for opt in options]

        try:
            await context.bot.send_poll(
                chat_id=update.message.chat_id,
                question=safe_question,
                options=safe_options,
                type="quiz",
                correct_option_id=correct,
                explanation="Generated by Google Gemini 🤖"
            )
            total_sent += 1
        except Exception as e:
            logger.error(f"Quiz sending failed: {e}")
            await update.message.reply_text("⚠️ Quiz creation failed. Please try again.")

    await update.message.reply_text(f"🎉 All {total_sent} quiz{'es' if total_sent > 1 else ''} sent successfully!")

# ================= TELEGRAM HANDLERS =================
async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage:\n`/quiz Python` or `/quiz 3 JavaScript`", parse_mode="Markdown")
        return
    count = int(args[0]) if args[0].isdigit() else 1
    topic = " ".join(args[1:] if args[0].isdigit() else args).strip() or "General Knowledge"
    await send_quizzes(update, context, topic, count)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    lower = user_input.lower()
    m = re.search(r"(\d+)\s+quiz", lower)
    count = int(m.group(1)) if m else 1
    topic = re.sub(r"(create|make|quiz|quizzes|question|questions|\d+)", "", lower, flags=re.IGNORECASE).strip() or "General Knowledge"

    if "quiz" in lower or "question" in lower:
        await send_quizzes(update, context, topic, count)
    else:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        reply = await ask_google_ai(user_input)
        await update.message.reply_text(reply)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling update:", exc_info=context.error)

# ================= APP STARTUP =================
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not GOOGLE_AI_KEY:
        logger.error("❌ Missing API tokens! Put TELEGRAM_BOT_TOKEN and GOOGLE_AI_KEY into your .env")
        exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🤖 Starting bot on Koyeb free tier...")
    app.run_polling()
