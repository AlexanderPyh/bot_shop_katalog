import asyncio
import os
import logging
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, ChatJoinRequestHandler, ConversationHandler
)
from datetime import datetime
from contextlib import contextmanager
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.error import Conflict, BadRequest, Forbidden
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG,
    handlers=[
        logging.FileHandler('user_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Environment variables
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '1839853176,409251957').split(',') if x}
TOKEN = os.getenv('USER_BOT_TOKEN', '7971140741:AAHt1cL1ljqQfUylHZ0JI_XWxjF1sA-e16w')
DB_PATH = os.getenv('DB_PATH', 'bot.db')
MEDIA_DIR = os.getenv('MEDIA_DIR', 'media')

# Ensure media directory exists
os.makedirs(MEDIA_DIR, exist_ok=True)

# Back button keyboard
BACK_BUTTON = [[KeyboardButton("🔙 Назад")]]

# States for ConversationHandler
PROMO_CODE_INPUT = 1

# --- Database Connection ---
@contextmanager
def get_conn():
    """Context manager for SQLite connections."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f"Database error: {e}")
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# Initialize database
def init_db():
    """Initialize database schema with all necessary tables."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS all_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL,
                    prod_name TEXT NOT NULL,
                    prod_desc TEXT,
                    price REAL NOT NULL,
                    size TEXT,
                    material TEXT,
                    photo_path TEXT,
                    FOREIGN KEY (category_id) REFERENCES categories(id)
                );
                CREATE TABLE IF NOT EXISTS buy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    promo_code_id INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES all_info(id),
                    FOREIGN KEY (promo_code_id) REFERENCES promo_codes(id)
                );
                CREATE TABLE IF NOT EXISTS promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL,
                    discount_percentage INTEGER NOT NULL,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    FOREIGN KEY (product_id) REFERENCES all_info(id)
                );
                CREATE TABLE IF NOT EXISTS telegram_profiles (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id INTEGER PRIMARY KEY,
                    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS support_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES telegram_profiles(telegram_id)
                );
                CREATE TABLE IF NOT EXISTS join_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    status TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO settings (key, value) VALUES ('restrict_keyboard_to_admins', '0');
            """)
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# --- Database Functions ---
def register_user(telegram_id: int, username: str = None) -> bool:
    """Register a user in the database."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO telegram_profiles (telegram_id, username) VALUES (?, ?)",
                (telegram_id, username)
            )
            return cursor.rowcount > 0 or cursor.execute(
                "SELECT 1 FROM telegram_profiles WHERE telegram_id = ?", (telegram_id,)
            ).fetchone() is not None
    except Exception as e:
        logger.error(f"Error registering user {telegram_id}: {e}")
        return False

def save_support_request(user_id: int, username: str, content: str) -> int:
    """Save a support request to the database."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO support_requests (user_id, username, content) VALUES (?, ?, ?)",
                (user_id, username, content)
            )
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Error saving support request for user {user_id}: {e}")
        raise

def log_join_request(user_id: int, username: str, status: str):
    """Log join request to the database."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO join_requests (user_id, username, status) VALUES (?, ?, ?)",
                (user_id, username, status)
            )
    except Exception as e:
        logger.error(f"Error logging join request for {user_id}: {e}")

def clear_user_cart(user_id: int) -> bool:
    """Clear user's cart."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM buy WHERE user_id = ?", (user_id,))
            return True
    except Exception as e:
        logger.error(f"Error clearing cart for user {user_id}: {e}")
        return False

def get_categories() -> list:
    """Retrieve all categories."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name FROM categories ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching categories: {e}")
        return []

def get_promotions() -> list:
    """Retrieve active promotions."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, description, image_url, start_date, end_date
                FROM promotions
                WHERE start_date <= date('now') AND end_date >= date('now')
                ORDER BY start_date
                """
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching promotions: {e}")
        return None

def get_products_by_category(category_id: int) -> list:
    """Retrieve products by category ID."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, prod_name, price, prod_desc
                FROM all_info
                WHERE category_id = ?
                """,
                (category_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching products for category {category_id}: {e}")
        return []

def get_product_by_id(product_id: int) -> dict:
    """Retrieve product details by ID."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT prod_name, price, prod_desc, size, material, photo_path
                FROM all_info
                WHERE id = ?
                """,
                (product_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching product {product_id}: {e}")
        return None

def add_product_to_cart(user_id: int, product_id: int, promo_code_id: int = None) -> bool:
    """Add a product to the user's cart with optional promo code."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO buy (user_id, product_id, promo_code_id) VALUES (?, ?, ?)",
                (user_id, product_id, promo_code_id)
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Error adding product {product_id} to cart for user {user_id}: {e}")
        return False

def get_user_cart(user_id: int) -> list:
    """Retrieve user's cart contents with promo code details."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT b.id, ai.id AS product_id, ai.prod_name, ai.price, pc.id AS promo_code_id,
                       pc.code, pc.discount_percentage
                FROM buy b
                JOIN all_info ai ON b.product_id = ai.id
                LEFT JOIN promo_codes pc ON b.promo_code_id = pc.id
                WHERE b.user_id = ?
                """,
                (user_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching cart for user {user_id}: {e}")
        return []

def is_blocked(user_id: int) -> bool:
    """Check if a user is blocked."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking if user {user_id} is blocked: {e}")
        return False

def validate_promo_code(code: str, product_id: int) -> dict:
    """Validate a promo code for a specific product."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, code, discount_percentage
                FROM promo_codes
                WHERE code = ? AND product_id = ? AND is_active = 1
                AND start_date <= date('now') AND end_date >= date('now')
                """,
                (code.upper(), product_id)
            )
            promo = cursor.fetchone()
            return dict(promo) if promo else None
    except Exception as e:
        logger.error(f"Error validating promo code {code} for product {product_id}: {e}")
        return None

def get_setting(key: str) -> str:
    """Retrieve a setting from the database."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row['value'] if row else None
    except Exception as e:
        logger.error(f"Error fetching setting {key}: {e}")
        return None

def update_setting(key: str, value: str) -> bool:
    """Update a setting in the database."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Error updating setting {key}: {e}")
        return False

# --- Async Wrappers ---
async def clear_user_cart_async(user_id: int) -> bool:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, clear_user_cart, user_id)
    except Exception as e:
        logger.error(f"Async error clearing cart for user {user_id}: {e}")
        return False

async def register_user_async(telegram_id: int, username: str = None) -> bool:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, register_user, telegram_id, username)
    except Exception as e:
        logger.error(f"Async error registering user {telegram_id}: {e}")
        return False

async def save_support_request_async(user_id: int, username: str, content: str) -> int:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, save_support_request, user_id, username, content)
    except Exception as e:
        logger.error(f"Async error saving support request for user {user_id}: {e}")
        raise

async def log_join_request_async(user_id: int, username: str, status: str):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, log_join_request, user_id, username, status)
    except Exception as e:
        logger.error(f"Async error logging join request for {user_id}: {e}")

async def get_categories_async() -> list:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_categories)
    except Exception as e:
        logger.error(f"Async error fetching categories: {e}")
        return []

async def get_promotions_async() -> list:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_promotions)
    except Exception as e:
        logger.error(f"Async error fetching promotions: {e}")
        return []

async def get_products_by_category_async(category_id: int) -> list:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_products_by_category, category_id)
    except Exception as e:
        logger.error(f"Async error fetching products for category {category_id}: {e}")
        return []

async def get_product_by_id_async(product_id: int) -> dict:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_product_by_id, product_id)
    except Exception as e:
        logger.error(f"Async error fetching product {product_id}: {e}")
        return None

async def add_product_to_cart_async(user_id: int, product_id: int, promo_code_id: int = None) -> bool:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, add_product_to_cart, user_id, product_id, promo_code_id)
    except Exception as e:
        logger.error(f"Async error adding product {product_id} to cart for user {user_id}: {e}")
        return False

async def get_user_cart_async(user_id: int) -> list:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_user_cart, user_id)
    except Exception as e:
        logger.error(f"Async error fetching cart for user {user_id}: {e}")
        return []

async def is_blocked_async(user_id: int) -> bool:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, is_blocked, user_id)
    except Exception as e:
        logger.error(f"Async error checking if user {user_id} is blocked: {e}")
        return False

async def validate_promo_code_async(code: str, product_id: int) -> dict:
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, validate_promo_code, code, product_id)
    except Exception as e:
        logger.error(f"Async error validating promo code {code} for product {product_id}: {e}")
        return None

async def get_setting_async(key: str) -> str:
    """Async wrapper for get_setting."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_setting, key)
    except Exception as e:
        logger.error(f"Async error fetching setting {key}: {e}")
        return None

async def update_setting_async(key: str, value: str) -> bool:
    """Async wrapper for update_setting."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, update_setting, key, value)
    except Exception as e:
        logger.error(f"Async error updating setting {key}: {e}")
        return False

# --- Helper Functions ---
async def check_bot_permissions(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """Check if the bot has admin permissions to manage join requests."""
    try:
        chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=context.bot.id)
        if chat_member.status in ('administrator', 'creator') and chat_member.can_invite_users:
            logger.info(f"Bot has required permissions in chat {chat_id}")
            return True
        logger.warning(f"Bot lacks admin or can_invite_users permission in chat {chat_id}")
        return False
    except Exception as e:
        logger.error(f"Error checking bot permissions in chat {chat_id}: {e}")
        return False

# --- Bot Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = update.effective_user.id
    username = update.effective_user.username
    logger.info(f"User {user_id} started bot")

    if await is_blocked_async(user_id):
        logger.warning(f"Blocked user {user_id} attempted to start bot")
        await update.message.reply_text("❌ Вы заблокированы и не можете использовать бота.")
        return

    if await register_user_async(user_id, username):
        logger.info(f"User {user_id} registered successfully")
    else:
        logger.warning(f"Failed to register user {user_id}")

    is_admin = user_id in ADMIN_IDS
    restrict_keyboard = await get_setting_async('restrict_keyboard_to_admins') == '1'

    inline_keyboard = [
        [InlineKeyboardButton("📦 Каталог", callback_data='catalog_main')],
        [InlineKeyboardButton("🛒 Корзина", callback_data='cart'),
         InlineKeyboardButton("🎁 Акции", callback_data='promotions')],
        [InlineKeyboardButton("🆘 Поддержка", callback_data='support_request'),
         InlineKeyboardButton("🎟 Промокод", callback_data='apply_promo')]
    ]

    if is_admin:
        status_text = "Скрыть клавиатуру для не-админов" if not restrict_keyboard else "Показать клавиатуру всем"
        inline_keyboard.append([InlineKeyboardButton(f"🔐 {status_text}", callback_data='toggle_keyboard')])

    inline_markup = InlineKeyboardMarkup(inline_keyboard)
    reply_markup = ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True)
    start_message = '🏠 Добро пожаловать! Выберите действие:'

    if restrict_keyboard and not is_admin:
        if update.message:
            await update.message.reply_text(
        f"Добро пожаловать в студию штор «Еврокаскад», @{username}!\n"
        "Уже более 25 лет мы создаем уют и стиль в вашем доме с помощью качественного текстиля.\n"
        "От элегантных штор до современных жалюзи и рулонных штор с электроуправлением — мы знаем, "
        "как подчеркнуть индивидуальность вашего интерьера!\n"
        "Оставайтесь с нами и следите за новостями!")
        elif update.callback_query:
            await update.callback_query.message.edit_text(
        f"Добро пожаловать в студию штор «Еврокаскад», @{username}!\n"
        "Уже более 25 лет мы создаем уют и стиль в вашем доме с помощью качественного текстиля.\n"
        "От элегантных штор до современных жалюзи и рулонных штор с электроуправлением — мы знаем, "
        "как подчеркнуть индивидуальность вашего интерьера!\n"
        "Оставайтесь с нами и следите за новостями!")
        else:
            logger.error("Invalid update type in start handler")
        return

    if update.message:
        await update.message.reply_text(start_message, reply_markup=inline_markup)
        await update.message.reply_text("Кнопка '🔙 Назад' снова вызывает это меню", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(start_message, reply_markup=inline_markup)
        await update.callback_query.message.reply_text("Кнопка '🔙 Назад' снова вызывает это меню", reply_markup=reply_markup)
    else:
        logger.error("Invalid update type in start handler")

async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display available categories."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if await is_blocked_async(user_id):
        await query.message.reply_text("❌ Вы заблокированы и не можете просматривать каталог.")
        return

    categories = await get_categories_async()
    logger.info(f"Fetched {len(categories)} categories for user {user_id}")

    if not categories:
        await query.edit_message_text(
            text="📂 Каталог пуст. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]])
        )
        return

    keyboard = [
        [InlineKeyboardButton(category['name'], callback_data=f'category_{category["id"]}')]
        for category in categories
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text="📂 Выберите категорию:",
        reply_markup=reply_markup
    )

async def show_category_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display products in a selected category."""
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    logger.info(f"Showing products for category {category_id} for user {user_id}")

    if await is_blocked_async(user_id):
        await query.message.reply_text("❌ Вы заблокированы и не можете просматривать товары.")
        return

    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
            category = cursor.fetchone()
            category_name = category['name'] if category else "Неизвестная категория"
    except Exception as e:
        logger.error(f"Error fetching category name {category_id}: {e}")
        category_name = "Неизвестная категория"

    products = await get_products_by_category_async(category_id)
    if not products:
        await query.edit_message_text(
            text=f"В категории '{category_name}' пока нет товаров.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='catalog_main')]])
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"{p['prod_name']} - {int(p['price'])}₽", callback_data=f'product_{p["id"]}')]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='catalog_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=f"Товары в категории '{category_name}':",
        reply_markup=reply_markup
    )

async def show_product_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display detailed product information with robust image handling."""
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    logger.info(f"Showing product {product_id} for user {user_id}")

    if await is_blocked_async(user_id):
        await query.message.reply_text("❌ Вы заблокированы и не можете просматривать товары.")
        return

    product = await get_product_by_id_async(product_id)
    if not product:
        await query.edit_message_text(text="❌ Товар не найден.")
        return

    message = (
        f"📌 <b>{product['prod_name']}</b>\n\n"
        f"💰 Цена: <b>{int(product['price'])}₽</b>\n"
        f"📝 Описание: {product['prod_desc'] or 'Нет описания'}\n"
        f"📏 Размер: {product['size'] or 'Не указан'}\n"
        f"🔍 Материал: {product['material'] or 'Не указан'}"
    )
    keyboard = [
        [InlineKeyboardButton("➕ В корзину", callback_data=f'add_to_cart_{product_id}')],
        [InlineKeyboardButton("🔙 К каталогу", callback_data='catalog_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if product['photo_path'] and os.path.exists(os.path.join(MEDIA_DIR, product['photo_path'])):
            with open(os.path.join(MEDIA_DIR, product['photo_path']), 'rb') as photo:
                await query.message.reply_photo(
                    photo=photo,
                    caption=message,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            await query.delete_message()
        else:
            logger.warning(f"Missing or invalid photo_path for product {product_id}: {product['photo_path']}")
            await query.edit_message_text(text=message, parse_mode='HTML', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error sending photo for product {product_id}: {e}, photo_path: {product['photo_path']}")
        await query.edit_message_text(text=message, parse_mode='HTML', reply_markup=reply_markup)

async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display user's cart with promo code discounts."""
    user_id = update.effective_user.id
    logger.info(f"Showing cart for user {user_id}")

    if await is_blocked_async(user_id):
        await update.message.reply_text("❌ Вы заблокированы и не можете просматривать корзину.")
        return

    cart_items = await get_user_cart_async(user_id)
    if not cart_items:
        text = "🛒 Ваша корзина пуста."
    else:
        message_lines = []
        total_price = 0
        for item in cart_items:
            price = float(item['price'])
            if item['promo_code_id'] and item['discount_percentage']:
                discount = item['discount_percentage']
                discounted_price = price * (1 - discount / 100)
                message_lines.append(
                    f"{item['prod_name']} - {int(price)}₽ (-{discount}%: {int(discounted_price)}₽, промокод: {item['code']})"
                )
                total_price += discounted_price
            else:
                message_lines.append(f"{item['prod_name']} - {int(price)}₽")
                total_price += price
        text = "🛒 Ваша корзина:\n\n" + "\n".join(message_lines) + f"\n\n💰 Итого: {int(total_price)}₽"

    keyboard = []
    if cart_items:
        keyboard.append([InlineKeyboardButton("🗑 Очистить корзину", callback_data='clear_cart')])
    keyboard.append([InlineKeyboardButton("🎟 Применить промокод", callback_data='apply_promo')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
        await update.callback_query.delete_message()

async def apply_promo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start applying a promo code."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    logger.info(f"User {user_id} started applying promo code")

    if await is_blocked_async(user_id):
        await query.message.reply_text("❌ Вы заблокированы и не можете использовать промокоды.")
        return ConversationHandler.END

    cart_items = await get_user_cart_async(user_id)
    if not cart_items:
        await query.message.reply_text("🛒 Ваша корзина пуста. Добавьте товары, чтобы применить промокод.")
        return ConversationHandler.END

    await query.message.reply_text(
        "🎟 Введите промокод:",
        reply_markup=ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True)
    )
    return PROMO_CODE_INPUT

async def apply_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle promo code input."""
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()
    logger.info(f"User {user_id} entered promo code: {code}")

    if code == "🔙 Назад":
        await update.message.reply_text("❌ Применение промокода отменено.", reply_markup=ReplyKeyboardRemove())
        await start_command(update, context)
        return ConversationHandler.END

    cart_items = await get_user_cart_async(user_id)
    if not cart_items:
        await update.message.reply_text("🛒 Ваша корзина пуста.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    applied = False
    for item in cart_items:
        product_id = item['product_id']
        promo = await validate_promo_code_async(code, product_id)
        if promo:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE buy SET promo_code_id = ? WHERE user_id = ? AND product_id = ?",
                    (promo['id'], user_id, product_id)
                )
                if cursor.rowcount > 0:
                    applied = True
                    logger.info(f"Promo code {code} applied to product {product_id} for user {user_id}")
                    break

    if applied:
        await update.message.reply_text(
            f"✅ Промокод {code} успешно применён!",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            "❗ Промокод недействителен или не применим к товарам в корзине.",
            reply_markup=ReplyKeyboardRemove()
        )

    await show_cart(update, context)
    return ConversationHandler.END

async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages to check for support context."""
    user = update.effective_user
    user_id = user.id
    message_text = update.message.text.strip()
    logger.debug(f"Received message from user {user_id}: '{message_text}'")

    if await is_blocked_async(user_id):
        await update.message.reply_text("❌ Вы заблокированы и не можете отправлять запросы поддержки.")
        return

    previous_message = context.user_data.get('last_bot_message', '')
    is_support_context = previous_message == "📩 Напишите ваш вопрос в поддержку:"

    if message_text == "🔙 Назад":
        logger.info(f"User {user_id} canceled operation via back button")
        await update.message.reply_text("❌ Операция отменена.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        await start_command(update, context)
        return

    if is_support_context:
        if not message_text:
            logger.warning(f"Empty support message from user {user_id}")
            await update.message.reply_text(
                "❗ Пожалуйста, введите текст вашего вопроса или проблемы:",
                reply_markup=ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True)
            )
            context.user_data['last_bot_message'] = "❗ Пожалуйста, введите текст вашего вопроса или проблемы:"
            return

        try:
            username = f"@{user.username}" if user.username else "Не указан"
            request_id = await save_support_request_async(
                user_id=user_id,
                username=username,
                content=message_text
            )
            await update.message.reply_text(
                "✅ Ваш запрос успешно отправлен в поддержку. Мы свяжемся с вами вскоре.",
                reply_markup=ReplyKeyboardRemove()
            )
            logger.info(f"Support request #{request_id} from user {user_id} saved successfully")
            context.user_data.clear()
            await start_command(update, context)
        except Exception as e:
            logger.error(f"Error saving support request for user {user_id}: {e}")
            await update.message.reply_text(
                "❗ Ошибка при отправке запроса. Пожалуйста, попробуйте позже или свяжитесь напрямую с @support_username.",
                reply_markup=ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True)
            )
            context.user_data['last_bot_message'] = "❗ Ошибка при отправке запроса. Пожалуйста, попробуйте позже или свяжитесь напрямую с @support_username."
    else:
        logger.debug(f"Message from user {user_id} not in support context, ignoring")

async def handle_new_channel_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle join requests in the channel."""
    logger.debug("Received chat_join_request update")
    if not update.chat_join_request:
        logger.warning("No chat_join_request in update")
        return

    join_request = update.chat_join_request
    chat = join_request.chat
    user = join_request.from_user
    user_id = user.id
    user_chat_id = join_request.user_chat_id
    username = user.username or "Пользователь"
    logger.info(f"Join request from {user_id} (@{username}) in chat {chat.id}, user_chat_id: {user_chat_id}")

    # Проверяем, что событие произошло в канале
    if chat.type != "channel":
        logger.debug("Ignoring non-channel join request")
        return

    # Проверяем права бота
    if not await check_bot_permissions(context, chat.id):
        logger.error(f"Bot lacks permissions to process join requests in chat {chat.id}")
        return

    # Проверяем, не заблокирован ли пользователь
    if await is_blocked_async(user_id):
        logger.warning(f"Blocked user {user_id} attempted to join, rejecting request")
        try:
            await context.bot.decline_chat_join_request(chat_id=chat.id, user_id=user_id)
            logger.info(f"Declined join request for blocked user {user_id}")
        except Exception as e:
            logger.error(f"Error declining join request for {user_id}: {e}")
        return

    # Логируем заявку
    await log_join_request_async(user_id, f"@{username}", "pending")

    # Сначала одобряем заявку
    try:
        await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=user_id)
        logger.info(f"Approved join request for user {user_id}")
        await log_join_request_async(user_id, f"@{username}", "member")
    except Exception as e:
        logger.error(f"Error approving join request for {user_id}: {e}")
        return

    # Приветственное сообщение
    welcome_message = (
        f"Добро пожаловать в студию штор «Еврокаскад», @{username}!\n"
        "Уже более 25 лет мы создаем уют и стиль в вашем доме с помощью качественного текстиля.\n"
        "От элегантных штор до современных жалюзи и рулонных штор с электроуправлением — мы знаем, "
        "как подчеркнуть индивидуальность вашего интерьера!\n"
        "Оставайтесь с нами и следите за новостями!"
    )



    # Отправляем приветственное сообщение через user_chat_id
    try:
        if user_chat_id:
            await context.bot.send_message(
                chat_id=user_chat_id,
                text=welcome_message,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            logger.info(f"Welcome message sent to user {user_id} via user_chat_id {user_chat_id}")
        else:
            logger.warning(f"No user_chat_id provided for user {user_id}")
            raise BadRequest("No user_chat_id available")
    except (BadRequest, Forbidden) as e:
        logger.warning(f"Failed to send welcome message to user {user_id}: {e}")
        channel_message = (
            f"Привет, @{username}! 🎉\n"
            f"Спасибо за вступление в канал! Чтобы начать, отправь /start боту: t.me/{context.bot.username}"
        )
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=channel_message,
                parse_mode='HTML'
            )
            logger.info(f"Sent fallback channel message for user {user_id}")
        except Exception as channel_e:
            logger.error(f"Failed to send channel message for user {user_id}: {channel_e}")
    except Exception as e:
        logger.error(f"Unexpected error sending welcome message to user {user_id}: {e}")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries."""
    query = update.callback_query
    data = query.data
    await query.answer()
    user_id = update.effective_user.id
    logger.info(f"Callback {data} from user {user_id}")

    if await is_blocked_async(user_id):
        await query.message.reply_text("❌ Вы заблокированы и не можете использовать бота.")
        return

    match data:
        case 'catalog_main':
            await show_catalog(update, context)
        case data if data.startswith('category_'):
            await show_category_products(update, context)
        case data if data.startswith('product_'):
            await show_product_details(update, context)
        case 'back_to_main':
            await start_command(update, context)
        case 'cart':
            await show_cart(update, context)
        case 'clear_cart':
            success = await clear_user_cart_async(user_id)
            if success:
                await query.edit_message_text("✅ Корзина очищена.")
            else:
                await query.edit_message_text("❌ Не удалось очистить корзину.")
            await show_cart(update, context)
        case 'promotions':
            promos = await get_promotions_async()
            if not promos:
                await query.edit_message_text("🎁 На данный момент акций нет.")
                return
            text = "🎁 Текущие акции:\n\n" + "\n\n".join(
                f"📌 {p['name']}\n{p['description'] or 'Без описания'}\n🕒 {p['start_date']}–{p['end_date']}"
                for p in promos
            )
            await query.edit_message_text(text)
            for promo in promos:
                if promo.get('image_url'):
                    try:
                        await query.message.reply_photo(
                            photo=promo['image_url'],
                            caption=f"{promo['name']}\n{promo['description'] or ''}"
                        )
                    except BadRequest as e:
                        logger.error(f"Error sending promo image: {e}")
        case 'support_request':
            await query.message.reply_text(
                "📩 Напишите ваш вопрос в поддержку:",
                reply_markup=ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True)
            )
            context.user_data['last_bot_message'] = "📩 Напишите ваш вопрос в поддержку:"
            logger.debug(f"User {user_id} prompted for support message")
        case 'apply_promo':
            return await apply_promo_start(update, context)
        case data if data.startswith('add_to_cart_'):
            product_id = int(data.split('_')[-1])
            if await add_product_to_cart_async(user_id, product_id):
                await query.answer("✅ Добавлено в корзину")
            else:
                await query.answer("❌ Не удалось добавить в корзину")
        case 'toggle_keyboard':
            if user_id not in ADMIN_IDS:
                await query.message.reply_text("Спасибо за вступление в канал! 🎉")
                return
            current_state = await get_setting_async('restrict_keyboard_to_admins')
            new_state = '0' if current_state == '1' else '1'
            if await update_setting_async('restrict_keyboard_to_admins', new_state):
                status_text = "доступна всем" if new_state == '0' else "ограничена для админов"
                await query.message.reply_text(f"✅ Клавиатура теперь {status_text}.")
            else:
                await query.message.reply_text("❌ Ошибка при изменении настроек.")
            await start_command(update, context)

async def cancel_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel promo code input."""
    logger.info(f"User {update.effective_user.id} canceled promo code input")
    await update.message.reply_text("❌ Применение промокода отменено.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    await start_command(update, context)
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bot errors."""
    error = context.error
    logger.error(f"Bot error: {error}", exc_info=True)
    if isinstance(error, Conflict):
        logger.error("Conflict detected. Ensure only one bot instance is running.")
        await context.application.stop_running()
    elif update and (update.message or update.callback_query):
        msg = update.message or update.callback_query.message
        await msg.reply_text("❗ Произошла ошибка. Попробуйте снова.")

# --- Main ---
def main():
    """Run the bot."""
    try:
        init_db()
        application = Application.builder().token(TOKEN).build()

        # Promo code conversation handler
        promo_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(apply_promo_start, pattern='^apply_promo$')],
            states={
                PROMO_CODE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_promo_code)],
            },
            fallbacks=[
                CommandHandler('cancel', cancel_promo),
                MessageHandler(filters.Regex('^🔙 Назад$'), cancel_promo)
            ],
            per_chat=True,
            per_user=True,
            per_message=True,
            name='promo_conversation'
        )

        # Handlers
        application.add_handler(CommandHandler('start', start_command))
        application.add_handler(CommandHandler('cart', show_cart))
        application.add_handler(CallbackQueryHandler(button))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support_message))
        application.add_handler(MessageHandler(filters.Regex('^🔙 Назад$'), start_command))
        application.add_handler(ChatJoinRequestHandler(handle_new_channel_member))
        application.add_handler(promo_handler)
        application.add_error_handler(error_handler)

        logger.info("Starting bot polling")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise

if __name__ == '__main__':
    main()