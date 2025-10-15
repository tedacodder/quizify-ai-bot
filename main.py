import os
import logging
import re
import random
import asyncio
import httpx
from dotenv import load_dotenv
from telegram import Update,InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== CONFIGURATION ==========
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_AI_KEY = os.getenv("GOOGLE_AI_KEY")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🧠 Generate Quiz", callback_data="quiz_start")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help_info")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎓 Welcome to **Quizify AI** — your AI quiz generator powered by Google Gemini!\n\n"
        "Type `/quiz <topic>` or click below to get started 👇",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
# Safety / batching
MAX_BATCH_SIZE = 5  # how many quizzes per batch
DELAY_BETWEEN_BATCHES = 2  # seconds between batches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ========== GOOGLE AI HELPER ==========
async def ask_google_ai(prompt: str, retries: int = 2, timeout_seconds: int = 40) -> str:
    """
    Call Google Gemini with basic retry logic.
    Returns the generated text or an error message.
    """
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
                await asyncio.sleep(1 + attempt)  # backoff
            else:
                logger.error("All Google AI attempts failed.")
                return "❌ AI service is unavailable right now. Please try again later."

# ========== QUIZ GENERATION ==========
async def generate_quiz(topic: str):
    """
    Robust parsing:
    - handles "Question: ..." or plain lines containing "?"
    - extracts options A-D with flexible separators
    - extracts correct answer (A-D) if provided
    - returns (question, options_list, correct_index)
    """
    prompt = (
        f"Create one multiple-choice quiz question about {topic}. "
        "Return the question, four distinct options labeled A–D, and indicate the correct one.\n"
        "Preferred format examples:\n"
        "Question: <text>\nA: option A\nB: option B\nC: option C\nD: option D\nCorrect: B\n\n"
        "If you deviate, still include a clear question and four labeled options."
    )

    response = await ask_google_ai(prompt)
    lines = [ln.strip() for ln in response.splitlines() if ln.strip()]

    # --- Extract question robustly ---
    question = None
    for line in lines:
        # if line explicitly starts with "Question" or contains a '?', take it
        if re.match(r"(?i)^question[:\-\s]", line):
            question = re.sub(r'(?i)^question[:\-\s]*', '', line).strip()
            break
        if "?" in line:
            # prefer the first reasonable sentence with '?'
            question = line.strip()
            break
    if not question:
        question = f"What is the correct answer about {topic}?"

    # --- Extract options A-D ---
    options = []
    opt_pattern = re.compile(r"^[A-D][\).:\-]\s*(.+)$", re.IGNORECASE)
    for line in lines:
        m = opt_pattern.match(line)
        if m:
            options.append(m.group(1).strip())
        if len(options) == 4:
            break

    # If AI used other formats (like "- A) ..."), try looser matching
    if len(options) < 4:
        for line in lines:
            # find lines starting with letter + whitespace, e.g., "A Option text"
            m = re.match(r"^([A-D])\s+(.+)$", line, re.IGNORECASE)
            if m:
                if m.group(2).strip() not in options:
                    options.append(m.group(2).strip())
            if len(options) == 4:
                break

    # pad to 4 if needed
    while len(options) < 4:
        options.append(f"{topic} Option {chr(65 + len(options))}")

    # --- Extract correct letter ---
    correct_letter = None
    for line in lines:
        m = re.search(r"correct[:\s\-]*([A-D])", line, re.IGNORECASE)
        if m:
            correct_letter = m.group(1).upper()
            break
    if correct_letter is None:
        # try "Answer: B" style
        for line in lines:
            m = re.search(r"answer[:\s\-]*([A-D])", line, re.IGNORECASE)
            if m:
                correct_letter = m.group(1).upper()
                break

    if correct_letter is None:
        correct_index = 0
    else:
        correct_index = ord(correct_letter) - 65
        if not (0 <= correct_index < 4):
            correct_index = 0

    # --- Shuffle options while keeping correct index aligned ---
    indexed = list(enumerate(options))  # (original_index, text)
    random.shuffle(indexed)
    shuffled_options = [text for (orig_idx, text) in indexed]
    # find new index where original correct was moved
    if correct_letter is None:
        new_correct = 0
    else:
        orig_correct = ord(correct_letter) - 65
        new_correct = next((i for i, (o, _) in enumerate(indexed) if o == orig_correct), 0)

    return question, shuffled_options, new_correct

# ========== QUIZ SENDER ==========
async def send_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE, topic: str, count: int):
    """Send quizzes as Telegram polls only."""
    total_sent = 0
    while total_sent < count:
        question, options, correct = await generate_quiz(topic)

        # Telegram limits: question ~300 chars, options ~100 chars each
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

# ========== TELEGRAM HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I’m your AI Quiz Bot powered by Google Gemini.\n\n"
        "Use /quiz or plain messages like:\n"
        "• `/quiz Python`\n"
        "• `/quiz 3 JavaScript`\n"
        "• `create 3 quizzes about HTML`\nType /help for more."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Commands:\n"
        "/start - Welcome\n"
        "/help - This message\n"
        "/quiz <topic> - Generate 1 quiz\n"
        "/quiz <n> <topic> - Generate n quizzes\n"
        "Or use natural phrases: 'create 3 quizzes about Math'"
    )

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Usage:\n`/quiz Python` or `/quiz 3 JavaScript`", parse_mode="Markdown"
        )
        return

    if args[0].isdigit():
        count = int(args[0])
        topic = " ".join(args[1:]).strip()
    else:
        count = 1
        topic = " ".join(args).strip()

    if not topic:
        topic = "General Knowledge"

    await send_quizzes(update, context, topic, count)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    lower = user_input.lower()
    logger.info(f"User: {user_input}")

    # detect number of quizzes like "create 3 quizzes"
    m = re.search(r"(\d+)\s+quiz", lower)
    count = int(m.group(1)) if m else 1

    # extract topic
    topic = re.sub(r"(create|make|quiz|quizzes|question|questions|\d+)", "", lower, flags=re.IGNORECASE).strip()
    if not topic:
        topic = "General Knowledge"

    if "quiz" in lower or "question" in lower:
        await send_quizzes(update, context, topic, count)
    else:
        # chatting fallback to Gemini
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        reply = await ask_google_ai(user_input)
        await update.message.reply_text(reply)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling update:", exc_info=context.error)

# ========== APP STARTUP (async, stable) ==========
async def main():
    if not TELEGRAM_BOT_TOKEN or not GOOGLE_AI_KEY:
        logger.error("❌ Missing API tokens! Put TELEGRAM_BOT_TOKEN and GOOGLE_AI_KEY into your .env")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🤖 Starting bot...")
    await app.initialize()
    await app.start()
    # start polling
    await app.updater.start_polling()
    logger.info("🚀 Bot is running. Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()  # keep running
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Stopping bot...")
        await app.updater.stop_polling()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
