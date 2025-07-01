import os
import logging
import random
import asyncio
from datetime import datetime
import html

from pymongo import MongoClient
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import TimedOut, BadRequest

# Optional dotenv support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("[⚠️] python-dotenv not installed; ensure env vars are set externally.")

# Environment variables
TOKEN = os.getenv("TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
MONGO_URI = os.getenv("MONGO_URI")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client.ctfbot
users = db.users
flags = db.flags
submissions = db.submissions
admins = db.admins

# Categories and Levels
CATEGORIES = ["Crypto", "Web", "Forensics", "Pwn", "Reverse"]
LEVELS = ["Easy", "Medium", "Hard"]

# Conversation states
SELECT_CHALLENGE, WAIT_FLAG, AF_CATEGORY, AF_NAME, AF_POINTS, AF_LINK, AF_LEVEL, AF_FLAG = range(8)

# Pagination settings
ITEMS_PER_PAGE = 10
SUBMISSIONS_PER_PAGE = 20  # Added for submissions pagination

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# GIF URLs
GIF_CORRECT = ["https://tenor.com/bCCX9.gif"]
GIF_WRONG = ["https://tenor.com/Agkx.gif"]

# Helper functions
def is_admin(username: str) -> bool:
    return username == ADMIN_USERNAME or bool(admins.find_one({"username": username}))

async def add_user_if_not_exists(user_id: int, username: str):
    if username is None:
        username = "Unknown"
    users.update_one(
        {"_id": user_id},
        {"$set": {"username": username}, "$setOnInsert": {"points": 0}},
        upsert=True,
    )

async def get_unsolved_challenges(user_id: int) -> list[str]:
    all_chals = [c["_id"] for c in flags.find()]
    solved = [s["challenge"] for s in submissions.find({"user_id": user_id, "correct": True})]
    return [ch for ch in all_chals if ch not in solved]

# Build paginated keyboard (used for leaderboard, viewusers)
def build_menu(items, page, prefix, items_per_page=ITEMS_PER_PAGE):
    start = page * items_per_page
    end = start + items_per_page
    page_items = items[start:end]
    keyboard = []
    for item in page_items:
        keyboard.append([InlineKeyboardButton(item, callback_data=f"{prefix}:noop")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page-1}:nav"))
    if end < len(items):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page+1}:nav"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    logger.info(f"Generated keyboard for {prefix}, page {page}, callback data: {nav_buttons}")
    return keyboard

# Build submissions message with pagination
def build_submissions_message(submissions_list, page):
    start = page * SUBMISSIONS_PER_PAGE
    end = start + SUBMISSIONS_PER_PAGE
    page_submissions = submissions_list[start:end]
    lines = []
    for r in page_submissions:
        ts = r.get("timestamp", r["_id"].generation_time).strftime("%Y-%m-%d %H:%M:%S")
        user_doc = users.find_one({"_id": r["user_id"]})
        uname = user_doc.get("username", "Unknown") if user_doc else "Unknown"
        status = "Correct" if r["correct"] else "Wrong"
        lines.append(f"{ts} - @{uname} - {r['challenge']} - {r['submitted_flag']} - {status}")
    text = "📝 Submissions:\n" + "\n".join(lines)
    
    keyboard = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"submissions:{page-1}:nav"))
    if end < len(submissions_list):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"submissions:{page+1}:nav"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    return text, keyboard

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Welcome to InfoSec CTF flag Bot 👾\n"
        "🦾This bot is designed to Submit flags for CTF challenges from InfoSec Cyber_CTF learning Gp\n"
        "🎟Features\n"
        "🎗 Flag submission\n"
        "🎗View Challenges\n"
        "🎗Earn points\n"
        "🎗Leaderboard\n"
        "If you want to share CTF challenges or need help in solving one, you can create a challenge for everyone to think about and try to solve.\n"
        "Feel free to say something in the InfoSec Cyber_CTF Training Group to request if you really want to share challenges.\n"
        
        "Commands for managing challenges\n"
        "You can typically type just / for the bot to show you the commands.\n"
        "/help – View all the commands\n"
        "/submit – Start flag submission\n"
        "/myviewpoints – View your points\n"
        "/viewchallenges – List all challenges\n"
        "/leaderboard – View top users\n"
        "/bloods – View all challenges and their solvers\n"
        "/cancel – Fix command not working errors"
    )
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/submit – Start flag submission\n"
        "/myviewpoints – View your points\n"
        "/viewchallenges – List all challenges\n"
        "/leaderboard – View top users\n"
        "/bloods – View all challenges and their solvers\n"
        "/addflag – (Admin) Add/update a challenge\n"
        "/addnewadmins <username> – (Admin) Grant admin rights\n"
        "/delete <challenge> – (Admin) Delete a challenge\n"
        "/viewusers – (Admin) View registered users\n"
        "/viewsubmissions – (Admin) View submissions log\n"
        "/cancel – Fix command not working errors"
    )

# View challenges → categories -> challenges -> details
async def view_challenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"viewcat:{cat}")] for cat in CATEGORIES]
    await update.message.reply_text("📂 Select a category:", reply_markup=InlineKeyboardMarkup(keyboard))

async def view_category_challenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 1)[1]
    challenges = [c["_id"] for c in flags.find({"category": category})]
    if not challenges:
        await query.edit_message_text(f"No challenges in category {category}.")
        return
    keyboard = [[InlineKeyboardButton(ch, callback_data=f"detail:{ch}")] for ch in challenges]
    await query.edit_message_text(f"📋 Challenges in {category}:", reply_markup=InlineKeyboardMarkup(keyboard))

async def details_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    doc = flags.find_one({"_id": name})
    if not doc:
        await query.edit_message_text("❗ Challenge not found.")
        return
    category = doc.get("category", "Unknown")
    level = doc.get("level", "Unknown")
    pts = doc.get("points", 0)
    link = doc.get("post_link", "")
    text = f"<b>{html.escape(name)}</b>\nCategory: {html.escape(category)}\nLevel: {html.escape(level)}\nPoints: {pts}\n<a href=\"{link}\">Post Link</a>"
    await query.edit_message_text(text, parse_mode="HTML")

# Submission flow
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user_if_not_exists(user.id, user.username)
    unsolved = await get_unsolved_challenges(user.id)
    if not unsolved:
        await update.message.reply_text("🎉 All challenges solved!")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(ch, callback_data=f"submit:{ch}")] for ch in unsolved]
    await update.message.reply_text(
        "📋 Select a challenge to submit:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_CHALLENGE

async def select_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chal = query.data.split(":", 1)[1]
    context.user_data["challenge"] = chal
    await query.edit_message_text(
        f"🚩 Submit flag for <b>{html.escape(chal)}</b>:\n<i>Please send only the flag.</i>",
        parse_mode="HTML",
    )
    return WAIT_FLAG

async def receive_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chal = context.user_data.get("challenge")
    flag_text = update.message.text.strip()
    doc = flags.find_one({"_id": chal})
    if not doc:
        await update.message.reply_text("❗ Challenge not found.")
        return ConversationHandler.END
    correct = flag_text == doc["flag"]
    pts = doc.get("points", 0)
    submissions.insert_one({
        "user_id": user.id,
        "challenge": chal,
        "submitted_flag": flag_text,
        "correct": correct,
        "timestamp": datetime.utcnow(),
    })
    if correct:
        users.update_one(
            {"_id": user.id},
            {
                "$inc": {"points": pts},
                "$set": {"last_correct_submission": datetime.utcnow()}
            }
        )
        await update.message.reply_text(
            f"✅ Correct! You earned {pts} points for {chal}!"
        )
        await update.message.reply_animation(random.choice(GIF_CORRECT))
    else:
        await update.message.reply_text(
            f"❌ Incorrect for {chal}. Try again with /submit"
        )
        await update.message.reply_animation(random.choice(GIF_WRONG))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Operation cancelled.")
    return ConversationHandler.END

# Other view commands
async def my_viewpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = users.find_one({"_id": user.id}) or {}
    pts = doc.get("points", 0)
    if user.username:
        name = f"@{user.username}"
    else:
        name = user.first_name or "User"
    await update.message.reply_text(f"👤 {name}, you have {pts} points.")

# Leaderboard with pagination
async def leaderboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = list(users.find().sort([("points", -1), ("last_correct_submission", 1)]))
    if not all_users:
        await update.message.reply_text("No users on the leaderboard yet.")
        return
    context.user_data['leaderboard_list'] = all_users
    logger.info(f"Stored leaderboard_list with {len(all_users)} users in user_data")
    items = [f"{rank+1}. @{html.escape(u.get('username', 'Unknown') or 'Unknown')} — {u['points']} pts" for rank, u in enumerate(all_users)]
    keyboard = build_menu(items, 0, 'lead')
    await update.message.reply_text(
        "<b>🏅 Leaderboard 🏅</b>\n\n" + "\n".join(items[0:ITEMS_PER_PAGE]),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def leaderboard_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(':', 2)
    logger.info(f"Leaderboard page callback triggered with data: {query.data}")
    if len(data) == 3 and data[2] == 'nav':
        page = int(data[1])
        all_users = context.user_data.get('leaderboard_list', [])
        logger.info(f"Retrieved {len(all_users)} users from leaderboard_list for page {page}")
        if not all_users:
            logger.warning("leaderboard_list is empty in context.user_data")
            await query.edit_message_text("Error: Leaderboard data not found. Please run /leaderboard again.")
            return
        items = [f"{rank+1}. @{html.escape(u.get('username', 'Unknown') or 'Unknown')} — {u['points']} pts" for rank, u in enumerate(all_users)]
        start = page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        keyboard = build_menu(items, page, 'lead')
        for attempt in range(3):
            try:
                await query.edit_message_text(
                    "<b>🏅 Leaderboard 🏅</b>\n\n" + "\n".join(page_items),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                logger.info(f"Successfully updated leaderboard to page {page}")
                return
            except TimedOut:
                logger.warning(f"edit_message_text timed out, retry {attempt+1}/3")
                await asyncio.sleep(5)  # Increased delay to 5 seconds
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    logger.info("Message content is the same, no need to edit.")
                    return
                elif "Message to edit not found" in str(e) or "MESSAGE_ID_INVALID" in str(e):
                    logger.error("Cannot edit message: message not found or invalid.")
                    await query.message.reply_text("Error: The leaderboard message is no longer available. Please run /leaderboard again.")
                    return
                else:
                    logger.error(f"BadRequest error: {e}")
            except Exception as e:
                logger.error(f"Unexpected error editing leaderboard message: {e}")
                break
        logger.error("Failed to edit leaderboard message after 3 attempts")
        await query.message.reply_text("Error: Could not update leaderboard. Please try again later.")

# Registered users with pagination
async def viewusers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    all_users = list(users.find())
    context.user_data['users_list'] = all_users
    logger.info(f"Stored users_list with {len(all_users)} users in user_data")
    items = [f"{u['_id']}: {u.get('username', 'No username')}" for u in all_users]
    keyboard = build_menu(items, 0, 'users')
    await update.message.reply_text("👥 Registered Users:", reply_markup=InlineKeyboardMarkup(keyboard))

async def viewusers_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, page_str, _ = query.data.split(':', 2)
    page = int(page_str)
    logger.info(f"Viewusers page callback triggered with data: {query.data}")
    all_users = context.user_data.get('users_list', [])
    logger.info(f"Retrieved {len(all_users)} users from users_list for page {page}")
    if not all_users:
        logger.warning("users_list is empty in context.user_data")
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
        await query.message.reply_text("Error: Users data not found. Please run /viewusers again.")
        return
    items = [f"{u['_id']}: {u.get('username', 'No username')}" for u in all_users]
    keyboard = build_menu(items, page, 'users')
    for attempt in range(3):
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            logger.info(f"Successfully updated viewusers to page {page}")
            return
        except TimedOut:
            logger.warning(f"edit_message_reply_markup timed out, retry {attempt+1}/3")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error editing viewusers message: {e}")
            break
    logger.error("Failed to edit viewusers message after 3 attempts")
    await query.message.reply_text("Error: Could not update users list. Please try again.")

# Admin commands (addnewadmins, addflag, delete, viewsubmissions)
async def addnewadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addnewadmins <username>")
        return
    new_admin = context.args[0].lstrip("@")
    admins.update_one({"username": new_admin}, {"$set": {"username": new_admin}}, upsert=True)
    await update.message.reply_text(f"✅ @{new_admin} is now an admin.")

async def addflag_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"category:{cat}")] for cat in CATEGORIES]
    await update.message.reply_text("📂 Select a category:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AF_CATEGORY

async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 1)[1]
    context.user_data["af_category"] = category
    await query.edit_message_text(f"📝 Enter challenge name for category {category}:")
    return AF_NAME

async def af_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["af_name"] = update.message.text.strip()
    await update.message.reply_text("🎯 Enter points value:")
    return AF_POINTS

async def af_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["af_points"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid integer for points.")
        return AF_POINTS
    await update.message.reply_text("🔗 Enter Telegram post link:")
    return AF_LINK

async def af_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["af_link"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton(lvl, callback_data=f"level:{lvl}")] for lvl in LEVELS]
    await update.message.reply_text("📊 Select difficulty level:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AF_LEVEL

async def select_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    level = query.data.split(":", 1)[1]
    context.user_data["af_level"] = level
    await query.edit_message_text(f"🚩 Enter the correct flag string for {context.user_data['af_name']}:")
    return AF_FLAG

async def af_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data["af_name"]
    pts = context.user_data["af_points"]
    link = context.user_data["af_link"]
    category = context.user_data["af_category"]
    level = context.user_data["af_level"]
    flag_str = update.message.text.strip()
    flags.update_one(
        {"_id": name},
        {"$set": {
            "flag": flag_str,
            "points": pts,
            "post_link": link,
            "category": category,
            "level": level
        }},
        upsert=True,
    )
    await update.message.reply_text(f"✅ Challenge '{name}' in category '{category}' with level '{level}' added/updated with {pts} points.")
    return ConversationHandler.END

async def delete_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /delete <challenge>")
        return
    name = " ".join(context.args).strip()
    doc = flags.find_one({"_id": name})
    if not doc:
        await update.message.reply_text(f"❗ Challenge '{name}' does not exist.")
        return
    pts = doc.get("points", 0)
    for s in submissions.find({"challenge": name, "correct": True}):
        users.update_one({"_id": s["user_id"]}, {"$inc": {"points": -pts}})
    submissions.delete_many({"challenge": name})
    flags.delete_one({"_id": name})
    await update.message.reply_text(f"✅ Challenge '{name}' and all related data deleted.")

# Updated viewsubmissions with pagination
async def viewsubmissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    
    all_submissions = list(submissions.find().sort("timestamp", -1))
    context.user_data['submissions_list'] = all_submissions
    
    if not all_submissions:
        await update.message.reply_text("No submissions yet.")
        return
    
    text, keyboard = build_submissions_message(all_submissions, 0)
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Callback handler for submissions pagination
async def submissions_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split(':', 2)
    if len(data) == 3 and data[0] == 'submissions' and data[2] == 'nav':
        page = int(data[1])
        all_submissions = context.user_data.get('submissions_list', [])
        
        if not all_submissions:
            await query.edit_message_text("Error: Submissions data not found. Please run /viewsubmissions again.")
            return
        
        text, keyboard = build_submissions_message(all_submissions, page)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# New /bloods command handlers
async def bloods_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = [
        {"$lookup": {
            "from": "submissions",
            "let": {"chal": "$_id"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$and": [
                        {"$eq": ["$challenge", "$$chal"]},
                        {"$eq": ["$correct", True]}
                    ]}
                }},
                {"$group": {"_id": None, "solvers": {"$addToSet": "$user_id"}}},
                {"$project": {"_id": 0, "solver_count": {"$size": "$solvers"}}}
            ],
            "as": "solver_info"
        }},
        {"$addFields": {
            "solver_count": {"$ifNull": [{"$arrayElemAt": ["$solver_info.solver_count", 0]}, 0]}
        }},
        {"$project": {"_id": 1, "solver_count": 1}}
    ]
    all_challenges = list(flags.aggregate(pipeline))
    all_challenges.sort(key=lambda x: x['_id'].lower())  # Sort alphabetically, case-insensitive
    context.user_data['bloods_challenges'] = all_challenges
    await show_challenges_page(update, context, 0)

async def show_challenges_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    all_challenges = context.user_data.get('bloods_challenges', [])
    if not all_challenges:
        if update.callback_query:
            await update.callback_query.edit_message_text("No challenges available.")
        else:
            await update.message.reply_text("No challenges available.")
        return
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_challenges = all_challenges[start:end]
    keyboard = []
    for chal in page_challenges:
        name = chal['_id']
        count = chal['solver_count']
        button_text = f"{name} ({count} solvers)"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"bloods_show:{name}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"bloods_page:{page-1}"))
    if end < len(all_challenges):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"bloods_page:{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    text = "📋 All Challenges:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def bloods_show_solvers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, challenge = query.data.split(':', 1)
    solved_submissions = list(submissions.find({"challenge": challenge, "correct": True}).sort("timestamp", 1))
    if not solved_submissions:
        await query.edit_message_text(f"No solvers yet for {challenge}.")
        return
    solvers = []
    for sub in solved_submissions:
        user_doc = users.find_one({"_id": sub["user_id"]})
        username = user_doc.get("username", "Unknown") if user_doc else "Unknown"
        solvers.append(username)
    seen = set()
    unique_solvers = [s for s in solvers if not (s in seen or seen.add(s))]
    if not unique_solvers:
        await query.edit_message_text(f"No solvers yet for {challenge}.")
        return
    firstblood = unique_solvers[0]
    other_solvers = unique_solvers[1:]
    text = f"Solvers for {challenge}\n"
    text += f"@{html.escape(firstblood)} firstblood\n"
    for solver in other_solvers:
        text += f"@{html.escape(solver)}\n"
    await query.edit_message_text(text)

async def bloods_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, page_str = query.data.split(':', 1)
    page = int(page_str)
    await show_challenges_page(update, context, page)

# Startup: retry setting commands
def init_commands(app):
    async def on_startup(application):
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show help"),
            BotCommand("submit", "Submit a flag"),
            BotCommand("myviewpoints", "View your points"),
            BotCommand("viewchallenges", "List all challenges"),
            BotCommand("leaderboard", "View top users"),
            BotCommand("bloods", "View all challenges and their solvers"),
            BotCommand("addflag", "Add/update a challenge"),
            BotCommand("addnewadmins", "Grant admin rights"),
            BotCommand("delete", "Delete a challenge"),
            BotCommand("viewusers", "View registered users"),
            BotCommand("viewsubmissions", "View submissions log"),
            BotCommand("cancel", "Cancel current operation"),
        ]
        for attempt in range(3):
            try:
                await application.bot.set_my_commands(commands)
                return
            except TimedOut:
                logger.warning(f"set_my_commands timed out, retry {attempt+1}/3")
                await asyncio.sleep(2)
        logger.error("Failed to set bot commands after 3 attempts")

    return on_startup

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(init_commands(None))
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myviewpoints", my_viewpoints))
    app.add_handler(CommandHandler("viewchallenges", view_challenges))
    app.add_handler(CallbackQueryHandler(view_category_challenges, pattern=r"^viewcat:.+"))
    app.add_handler(CallbackQueryHandler(details_challenge, pattern=r"^detail:.+"))
    app.add_handler(CommandHandler("leaderboard", leaderboard_start))
    app.add_handler(CallbackQueryHandler(leaderboard_page, pattern=r"^lead:\d+:nav$"))
    app.add_handler(CommandHandler("bloods", bloods_start))
    app.add_handler(CallbackQueryHandler(bloods_show_solvers, pattern=r"^bloods_show:.+"))
    app.add_handler(CallbackQueryHandler(bloods_page, pattern=r"^bloods_page:\d+$"))
    app.add_handler(CommandHandler("addnewadmins", addnewadmins))
    app.add_handler(CommandHandler("delete", delete_challenge))
    app.add_handler(CommandHandler("viewusers", viewusers_start))
    app.add_handler(CallbackQueryHandler(viewusers_page, pattern=r"^users:\d+:(nav|.+)"))
    app.add_handler(CommandHandler("viewsubmissions", viewsubmissions))
    app.add_handler(CallbackQueryHandler(submissions_page, pattern=r"^submissions:\d+:nav$"))

    # Conversations
    submit_conv = ConversationHandler(
        entry_points=[CommandHandler("submit", submit_start)],
        states={
            SELECT_CHALLENGE: [
                CallbackQueryHandler(select_challenge, pattern=r"^submit:.+"),
                CommandHandler("submit", submit_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: u.message.reply_text("Please select a challenge from the buttons above."))
            ],
            WAIT_FLAG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_flag),
                CommandHandler("submit", submit_start)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        allow_reentry=True,
    )
    addflag_conv = ConversationHandler(
        entry_points=[CommandHandler("addflag", addflag_start)],
        states={
            AF_CATEGORY: [CallbackQueryHandler(select_category, pattern=r"^category:.+")],
            AF_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_name)],
            AF_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_points)],
            AF_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_link)],
            AF_LEVEL: [CallbackQueryHandler(select_level, pattern=r"^level:.+")],
            AF_FLAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_flag)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )
    app.add_handler(submit_conv)
    app.add_handler(addflag_conv)

    # Error handler
    async def error_handler(update, context):
        logger.error("❌ Exception in handler:", exc_info=context.error)

    app.add_error_handler(error_handler)

    # Start webhook or polling
    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 5000)),
            url_path="webhook",
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
