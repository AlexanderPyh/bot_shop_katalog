
import os
import logging
import shutil
from datetime import datetime, timedelta
from contextlib import contextmanager
import sqlite3
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.error import BadRequest, Conflict, TelegramError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv
import uuid
import asyncio

# Load environment variables
load_dotenv()

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG,
    handlers=[
        logging.FileHandler('admin_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Enable debug logging for ConversationHandler
logging.getLogger('telegram.ext.ConversationHandler').setLevel(logging.DEBUG)

# --- States ---
CATEGORY_CHOICE, NEW_CATEGORY, PROD_NAME, PROD_DESC, PROD_SIZE, PROD_MATERIAL, PROD_PRICE, PROD_PHOTO = range(8)
PROMO_NAME, PROMO_DESC, PROMO_IMAGE, PROMO_START, PROMO_END = range(5)
PROMO_CODE, PROMO_PRODUCT, PROMO_DISCOUNT, PROMO_CODE_START, PROMO_CODE_END = range(10, 15)
MAIL_CONTENT, MAIL_TIMER = range(2)

# --- Environment configuration ---
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '1839853176, 409251957').split(',') if x}
CREDENTIALS_PATH = os.getenv('GOOGLE_CREDENTIALS_JSON', 'credentials.json')
SPREADSHEET_ID = os.getenv('GSHEET_ANALYTICS_ID', '1okEbfK969YCaioL_ZiqNkQynGXWhAHFKjLEVthqs48')
ADMIN_BOT_TOKEN = os.getenv('ADMIN_BOT_TOKEN', '8089242630:AAE_6189OdZD1i-Sh_cvFgSy6T8GD49gYt4')
USER_BOT_TOKEN = os.getenv('USER_BOT_TOKEN', '7971140741:AAHt1cL1ljqQfUylHZ0JI_XWxjF1sA-e16w')
DB_PATH = os.getenv('DB_PATH', 'bot.db')
MEDIA_DIR = os.getenv('MEDIA_DIR', 'media')

# Ensure media directory exists
os.makedirs(MEDIA_DIR, exist_ok=True)

# --- Back Button Keyboard ---
BACK_BUTTON = [[KeyboardButton("🔙 Назад")]]

def get_back_keyboard():
    """Return a ReplyKeyboardMarkup with a Back button."""
    return ReplyKeyboardMarkup(BACK_BUTTON, resize_keyboard=True, one_time_keyboard=True)

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

# --- Database Initialization ---
def init_db():
    """Initialize database schema."""
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
                CREATE TABLE IF NOT EXISTS support_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES telegram_profiles(telegram_id)
                );
                CREATE TABLE IF NOT EXISTS buy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES all_info(id)
                );
                CREATE TABLE IF NOT EXISTS promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL,
                    discount_percentage INTEGER NOT NULL,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (product_id) REFERENCES all_info(id)
                );
                CREATE TABLE IF NOT EXISTS telegram_profiles (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS mailings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    send_at TIMESTAMP NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled'
                );
            """)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='categories'")
            if not cursor.fetchone():
                logger.error("Failed to create categories table")
                raise RuntimeError("Categories table not created")
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# --- Admin Check Decorator ---
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            logger.warning(f"Unauthorized access attempt by user {user_id}")
            if update.message:
                await update.message.reply_text('❌ Доступ запрещён.')
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

# --- Message Reply Helper ---
def get_reply_target(update: Update):
    return update.callback_query.message if update.callback_query else update.message

# --- Database Functions ---
def get_categories():
    """Retrieve all categories."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name FROM categories ORDER BY name")
            categories = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Fetched {len(categories)} categories")
            return categories
    except Exception as e:
        logger.error(f"Error fetching categories: {e}")
        return []

def create_category(name: str) -> int:
    """Create a new category."""
    try:
        logger.info(f"Attempting to create category: '{name}'")
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO categories (name) VALUES (?)", (name,))
            category_id = cursor.lastrowid
            logger.info(f"Category '{name}' created with ID: {category_id}")
            return category_id
    except sqlite3.IntegrityError:
        logger.error(f"Category '{name}' already exists")
        raise ValueError(f"Категория '{name}' уже существует")
    except Exception as e:
        logger.error(f"Error creating category '{name}': {e}")
        raise

def delete_category(category_id: int):
    """Delete a category and all associated products and media."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, photo_path FROM all_info WHERE category_id = ?", (category_id,))
            products = cursor.fetchall()
            for product in products:
                product_id = product['id']
                photo_path = product['photo_path']
                cursor.execute("DELETE FROM all_info WHERE id = ?", (product_id,))
                cursor.execute("DELETE FROM buy WHERE product_id = ?", (product_id,))
                cursor.execute("DELETE FROM promo_codes WHERE product_id = ?", (product_id,))
                if photo_path:
                    product_dir = os.path.join(MEDIA_DIR, str(product_id))
                    if os.path.exists(product_dir):
                        shutil.rmtree(product_dir)
                        logger.info(f"Deleted media directory for product #{product_id}: {product_dir}")
                logger.info(f"Product #{product_id} deleted as part of category #{category_id} deletion")
            cursor.execute("DELETE FROM categories WHERE id = ?", (category_id,))
            if cursor.rowcount == 0:
                logger.warning(f"No category found with ID {category_id}")
                raise ValueError(f"Категория #{category_id} не найдена")
            logger.info(f"Category #{category_id} deleted")
    except Exception as e:
        logger.error(f"Error deleting category #{category_id}: {e}")
        raise

def get_category_by_id(category_id: int):
    """Retrieve a category by ID."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name FROM categories WHERE id = ?", (category_id,))
            category = cursor.fetchone()
            return dict(category) if category else None
    except Exception as e:
        logger.error(f"Error fetching category #{category_id}: {e}")
        return None

def create_product(category_id: int, name: str, price: float, desc: str, photo_path: str, size: str, material: str) -> int:
    """Create a new product."""
    try:
        logger.info(f"Creating product: '{name}' in category {category_id}")
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO all_info (category_id, prod_name, prod_desc, price, photo_path, size, material)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (category_id, name, desc, price, photo_path, size, material)
            )
            product_id = cursor.lastrowid
            logger.info(f"Product '{name}' created with ID: {product_id}")
            return product_id
    except Exception as e:
        logger.error(f"Error creating product '{name}': {e}")
        raise

def delete_product(product_id: int):
    """Delete a product and its associated media and promo codes."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT photo_path FROM all_info WHERE id = ?", (product_id,))
            product = cursor.fetchone()
            if not product:
                logger.warning(f"No product found with ID {product_id}")
                raise ValueError(f"Товар #{product_id} не найден")
            photo_path = product['photo_path']
            cursor.execute("DELETE FROM all_info WHERE id = ?", (product_id,))
            cursor.execute("DELETE FROM buy WHERE product_id = ?", (product_id,))
            cursor.execute("DELETE FROM promo_codes WHERE product_id = ?", (product_id,))
            if photo_path:
                product_dir = os.path.join(MEDIA_DIR, str(product_id))
                if os.path.exists(product_dir):
                    shutil.rmtree(product_dir)
                    logger.info(f"Deleted media directory for product #{product_id}: {product_dir}")
            logger.info(f"Product #{product_id} deleted")
    except Exception as e:
        logger.error(f"Error deleting product #{product_id}: {e}")
        raise

def fetch_promotions():
    """Retrieve all promotions."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, description, image_url, start_date, end_date
                FROM promotions
                ORDER BY start_date
                """
            )
            promotions = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Fetched {len(promotions)} promotions")
            return promotions
    except Exception as e:
        logger.error(f"Error fetching promotions: {e}")
        return []

def create_promotion(name: str, description: str, image_url: str, start: str, end: str) -> int:
    """Create a new promotion."""
    try:
        logger.info(f"Creating promotion: '{name}'")
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO promotions (name, description, image_url, start_date, end_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, description, image_url, start, end)
            )
            promotion_id = cursor.lastrowid
            logger.info(f"Promotion '{name}' created with ID: {promotion_id}")
            return promotion_id
    except Exception as e:
        logger.error(f"Error creating promotion '{name}': {e}")
        raise

def delete_promotion(promo_id: int):
    """Delete a promotion."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM promotions WHERE id = ?", (promo_id,))
            if cursor.rowcount == 0:
                logger.warning(f"No promotion found with ID {promo_id}")
                raise ValueError(f"Акция #{promo_id} не найдена")
            logger.info(f"Promotion #{promo_id} deleted")
    except Exception as e:
        logger.error(f"Error deleting promotion #{promo_id}: {e}")
        raise

def create_promo_code(code: str, product_id: int, discount_percentage: int, start_date: str, end_date: str) -> int:
    """Создать новый промокод."""
    try:
        logger.info(f"Creating promo code: '{code}' for product {product_id}")
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO promo_codes (code, product_id, discount_percentage, start_date, end_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                (code, product_id, discount_percentage, start_date, end_date)
            )
            promo_id = cursor.lastrowid
            logger.info(f"Promo code '{code}' created with ID: {promo_id}")
            return promo_id
    except sqlite3.IntegrityError:
        logger.error(f"Promo code '{code}' already exists")
        raise ValueError(f"Промокод '{code}' уже существует")
    except Exception as e:
        logger.error(f"Error creating promo code '{code}': {e}")
        raise

def fetch_promo_codes():
    """Получить все промокоды."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT pc.id, pc.code, pc.product_id, p.prod_name, pc.discount_percentage, pc.start_date, pc.end_date, pc.is_active
                FROM promo_codes pc
                JOIN all_info p ON pc.product_id = p.id
                ORDER BY pc.created_at
                """
            )
            promo_codes = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Fetched {len(promo_codes)} promo codes")
            return promo_codes
    except Exception as e:
        logger.error(f"Error fetching promo codes: {e}")
        return []

def deactivate_promo_code(promo_id: int):
    """Деактивировать промокод."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE promo_codes SET is_active = 0 WHERE id = ?", (promo_id,))
            if cursor.rowcount == 0:
                logger.warning(f"No promo code found with ID {promo_id}")
                raise ValueError(f"Промокод #{promo_id} не найден")
            logger.info(f"Promo code #{promo_id} deactivated")
    except Exception as e:
        logger.error(f"Error deactivating promo code #{promo_id}: {e}")
        raise

def fetch_products():
    """Retrieve all products."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM all_info ORDER BY id")
            products = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Fetched {len(products)} products")
            return products
    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        return []

def get_product_by_id(product_id: int):
    """Retrieve a product by ID."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM all_info WHERE id = ?", (product_id,))
            product = cursor.fetchone()
            return dict(product) if product else None
    except Exception as e:
        logger.error(f"Error fetching product #{product_id}: {e}")
        return None

def fetch_users():
    """Retrieve all telegram profiles."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id FROM telegram_profiles")
            users = [row['telegram_id'] for row in cursor.fetchall()]
            logger.info(f"Fetched {len(users)} users")
            return users
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return []

def fetch_support_requests():
    """Retrieve all pending support requests."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_id, username, content, created_at
                FROM support_requests
                ORDER BY created_at
                """
            )
            requests = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Fetched {len(requests)} support requests")
            return requests
    except Exception as e:
        logger.error(f"Error fetching support requests: {e}")
        return []

def delete_support_request(request_id: int):
    """Delete a support request."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM support_requests WHERE id = ?", (request_id,))
            if cursor.rowcount == 0:
                logger.warning(f"No support request found with ID {request_id}")
                raise ValueError(f"Запрос поддержки #{request_id} не найден")
            logger.info(f"Support request #{request_id} deleted")
    except Exception as e:
        logger.error(f"Error deleting support request #{request_id}: {e}")
        raise

def delete_mailing(mailing_id: int):
    """Delete a mailing."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM mailings WHERE id = ?", (mailing_id,))
            if cursor.rowcount == 0:
                logger.warning(f"No mailing found with ID {mailing_id}")
                raise ValueError(f"Рассылка #{mailing_id} не найдена")
            logger.info(f"Mailing #{mailing_id} deleted")
    except Exception as e:
        logger.error(f"Error deleting mailing #{mailing_id}: {e}")
        raise

# --- Analytics Functions ---
def fetch_sales_by_date(start_date: str, end_date: str):
    """Fetch sales data by date."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT date(added_at) as order_date, COUNT(*) as total_sales
                FROM buy
                WHERE date(added_at) BETWEEN ? AND ?
                GROUP BY date(added_at)
                ORDER BY order_date
                """,
                (start_date, end_date)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching sales data: {e}")
        return []

def fetch_top_products(start_date: str, end_date: str, limit: int = 10):
    """Fetch top products by sales."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT b.product_id, p.prod_name AS product_name, COUNT(*) AS total_sold
                FROM buy b
                JOIN all_info p ON b.product_id = p.id
                WHERE date(b.added_at) BETWEEN ? AND ?
                GROUP BY b.product_id, p.prod_name
                ORDER BY total_sold DESC
                LIMIT ?
                """,
                (start_date, end_date, limit)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching top products: {e}")
        return []

def fetch_user_activity(start_date: str, end_date: str, limit: int = 10):
    """Fetch user activity data."""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT b.user_id, COUNT(*) AS orders_count
                FROM buy b
                WHERE date(b.added_at) BETWEEN ? AND ?
                GROUP BY b.user_id
                ORDER BY orders_count DESC
                LIMIT ?
                """,
                (start_date, end_date, limit)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching user activity: {e}")
        return []

def fetch_metrics():
    """Fetch analytics metrics."""
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    end_date = datetime.now().strftime('%Y-%m-%d')
    sales = fetch_sales_by_date(start_date, end_date)
    top_products = fetch_top_products(start_date, end_date, limit=5)
    users = fetch_user_activity(start_date, end_date, limit=10)
    return {'sales': sales, 'top_products': top_products, 'users': users}

def export_to_sheets(metrics: dict):
    """Export metrics to Google Sheets."""
    try:
        creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        service = build('sheets', 'v4', credentials=creds)
        sheet = service.spreadsheets()
        sales_values = [[r['order_date'], r['total_sales']] for r in metrics['sales']]
        sheet.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range='Sales!A2:B',
            valueInputOption='RAW',
            body={'values': sales_values},
        ).execute()
        product_values = [[r['product_name'], r['total_sold']] for r in metrics['top_products']]
        sheet.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range='TopProducts!A2:B',
            valueInputOption='RAW',
            body={'values': product_values},
        ).execute()
        logger.info("Metrics exported to Google Sheets")
    except Exception as e:
        logger.error(f"Error exporting to Google Sheets: {e}")
        raise

# --- Mailing Functions ---
async def send_mailing_directly(mailing_id: int, mail_content: str):
    """Send mailing directly to users."""
    logger.info(f"Starting mailing #{mailing_id}")
    try:
        users = fetch_users()
        if not users:
            logger.warning(f"No users found for mailing #{mailing_id}")
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE mailings SET status = 'failed' WHERE id = ?", (mailing_id,))
            return
        bot = Bot(USER_BOT_TOKEN)
        bot_info = await bot.get_me()
        logger.info(f"User bot authenticated: {bot_info.username}")
        success_count = 0
        failed_users = []
        for uid in users:
            try:
                await bot.send_message(chat_id=uid, text=mail_content, parse_mode='HTML')
                success_count += 1
                logger.info(f"Mailing #{mailing_id} sent to user {uid}")
                await asyncio.sleep(0.1)  # Avoid Telegram rate limits
            except TelegramError as e:
                logger.error(f"Telegram error sending mailing #{mailing_id} to user {uid}: {e}")
                failed_users.append({'user_id': uid, 'error': str(e)})
            except Exception as e:
                logger.error(f"Unexpected error sending mailing #{mailing_id} to user {uid}: {e}")
                failed_users.append({'user_id': uid, 'error': str(e)})
        status = 'completed' if success_count > 0 else 'failed'
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE mailings SET status = ? WHERE id = ?", (status, mailing_id))
        logger.info(f"Mailing #{mailing_id} {status}, sent to {success_count}/{len(users)} users")
        if failed_users:
            logger.warning(f"Failed to send to users: {failed_users}")
    except Exception as e:
        logger.error(f"Error executing mailing #{mailing_id}: {e}", exc_info=True)
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE mailings SET status = 'failed' WHERE id = ?", (mailing_id,))

async def check_support_requests(context: ContextTypes.DEFAULT_TYPE):
    """Periodically check for new support requests and notify admins."""
    while True:
        try:
            requests = fetch_support_requests()
            if not requests:
                await asyncio.sleep(60)
                continue
            bot = Bot(ADMIN_BOT_TOKEN)
            for req in requests:
                username = req['username'] or 'Не указан'
                text = (
                    f"📩 Новый запрос поддержки #{req['id']}\n"
                    f"Пользователь: @{username} (ID: {req['user_id']})\n"
                    f"Время: {req['created_at']}\n"
                    f"Сообщение: {req['content']}"
                )
                keyboard = [
                    [InlineKeyboardButton("🚫 Заблокировать пользователя", callback_data=f"block_user_{req['user_id']}_{req['id']}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(
                            chat_id=admin_id,
                            text=text,
                            parse_mode='HTML',
                            reply_markup=reply_markup
                        )
                        logger.info(f"Support request #{req['id']} sent to admin {admin_id}")
                    except TelegramError as e:
                        logger.error(f"Error sending support request #{req['id']} to admin {admin_id}: {e}")
                delete_support_request(req['id'])
        except Exception as e:
            logger.error(f"Error checking support requests: {e}")
        await asyncio.sleep(60)  # Check every 60 seconds

async def check_scheduled_mailings(context: ContextTypes.DEFAULT_TYPE):
    """Periodically check for scheduled mailings."""
    while True:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, content, send_at
                    FROM mailings
                    WHERE status = 'scheduled' AND send_at <= ?
                    """,
                    (datetime.now(),)
                )
                mailings = [dict(row) for row in cursor.fetchall()]
            for mailing in mailings:
                logger.info(f"Processing scheduled mailing #{mailing['id']} at {mailing['send_at']}")
                await send_mailing_directly(mailing['id'], mailing['content'])
        except Exception as e:
            logger.error(f"Error checking scheduled mailings: {e}")
        await asyncio.sleep(60)  # Check every minute

# --- Handlers ---
@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    logger.info(f"User {update.effective_user.id} started admin bot")
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📊 Аналитика", callback_data="analytics")],
        [InlineKeyboardButton("🎁 Акции", callback_data="promos"),
         InlineKeyboardButton("🎟 Промокоды", callback_data="promo_codes")],
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("✉️ Рассылка", callback_data="mailing"),
         InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("📋 Категории", callback_data="categories"),
         InlineKeyboardButton("📬 Просмотр рассылок", callback_data="view_mailings")],
        [InlineKeyboardButton("📩 Запросы поддержки", callback_data="support_requests")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text("🏠 Админ-консоль. Выберите действие:", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text("🏠 Админ-консоль. Выберите действие:", reply_markup=reply_markup)
    return ConversationHandler.END

@admin_only
async def analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /analytics command."""
    msg = get_reply_target(update)
    logger.info(f"Analytics requested by user {update.effective_user.id}")
    metrics = fetch_metrics()
    if not metrics['sales']:
        await msg.reply_text("⚠️ Данные о продажах за последние 30 дней отсутствуют.")
        return
    top_products = metrics['top_products']
    preview = "📊 Топ-5 товаров:\n" + "\n".join(
        f"#{i + 1}: {p['product_name']} ({p['total_sold']} продаж)"
        for i, p in enumerate(top_products)
    ) if top_products else "Нет данных о продажах."
    try:
        export_to_sheets(metrics)
        await msg.reply_text(f"{preview}\n\n✅ Данные экспортированы в Google Sheets.")
    except Exception as e:
        await msg.reply_text(f"{preview}\n\n❗ Ошибка экспорта в Google Sheets: {str(e)}")

@admin_only
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display catalog with inline buttons for each product."""
    msg = get_reply_target(update)
    logger.info(f"Catalog requested by user {update.effective_user.id}")
    prods = fetch_products()
    if not prods:
        await msg.reply_text("📂 Каталог пуст.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]))
        return
    keyboard = [
        [InlineKeyboardButton(f"#{p['id']} {p['prod_name']} - {int(p['price'])}₽", callback_data=f"product_{p['id']}")]
        for p in prods
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.reply_text("🛍 Выберите товар для просмотра:", reply_markup=reply_markup)

@admin_only
async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display categories with inline buttons."""
    msg = get_reply_target(update)
    logger.info(f"Categories menu requested by user {update.effective_user.id}")
    categories = get_categories()
    if not categories:
        await msg.reply_text("📋 Категорий нет.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]))
        return
    keyboard = [
        [InlineKeyboardButton(f"#{c['id']} {c['name']}", callback_data=f"category_{c['id']}")]
        for c in categories
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.reply_text("📋 Выберите категорию для просмотра:", reply_markup=reply_markup)

@admin_only
async def show_category_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display category information with delete option."""
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split('_')[1])
    logger.info(f"Showing category #{category_id} for user {update.effective_user.id}")
    category = get_category_by_id(category_id)
    if not category:
        await query.message.reply_text("❌ Категория не найдена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К категориям", callback_data='categories')]]))
        return
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as product_count FROM all_info WHERE category_id = ?", (category_id,))
        product_count = cursor.fetchone()['product_count']
    text = (
        f"<b>#{category['id']} {category['name']}</b>\n"
        f"Товаров в категории: {product_count}\n"
        f"<i>При удалении категории все связанные товары и их медиафайлы будут удалены.</i>"
    )
    keyboard = [
        [InlineKeyboardButton("🗑 Удалить категорию", callback_data=f"delete_category_{category_id}")],
        [InlineKeyboardButton("🔙 К категориям", callback_data='categories')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error sending category #{category_id}: {e}")
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)

@admin_only
async def delete_category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category deletion."""
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split('_')[2])
    logger.info(f"Deleting category #{category_id} by user {update.effective_user.id}")
    try:
        delete_category(category_id)
        await query.message.reply_text(
            f"✅ Категория #{category_id} удалена.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К категориям", callback_data='categories')]])
        )
        await query.delete_message()
    except ValueError as e:
        await query.message.reply_text(
            f"❗ {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К категориям", callback_data='categories')]])
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error in delete_category_handler #{category_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка при удалении: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К категориям", callback_data='categories')]])
        )
        await query.delete_message()

@admin_only
async def show_product_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display detailed product information with delete option."""
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split('_')[1])
    logger.info(f"Showing product #{product_id} for user {update.effective_user.id}")
    product = get_product_by_id(product_id)
    if not product:
        await query.message.reply_text("❌ Товар не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К каталогу", callback_data='catalog')]]))
        return
    text = (
        f"<b>#{product['id']} {product['prod_name']}</b>\n"
        f"{product['prod_desc'] or 'Без описания'}\n"
        f"Размер: <i>{product['size'] or 'N/A'}</i>\n"
        f"Материал: <i>{product['material'] or 'N/A'}</i>\n"
        f"<b>Цена:</b> {int(product['price'])}₽"
    )
    keyboard = [
        [InlineKeyboardButton("🗑 Удалить товар", callback_data=f"delete_product_{product_id}")],
        [InlineKeyboardButton("🔙 К каталогу", callback_data='catalog')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if product['photo_path'] and os.path.exists(os.path.join(MEDIA_DIR, product['photo_path'])):
            with open(os.path.join(MEDIA_DIR, product['photo_path']), 'rb') as photo:
                await query.message.reply_photo(
                    photo=photo,
                    caption=text,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
        else:
            await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error sending product #{product_id}: {e}")
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        await query.delete_message()

@admin_only
async def delete_product_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product deletion."""
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split('_')[2])
    logger.info(f"Deleting product #{product_id} by user {update.effective_user.id}")
    try:
        delete_product(product_id)
        await query.message.reply_text(
            f"✅ Товар #{product_id} удалён.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К каталогу", callback_data='catalog')]])
        )
        await query.delete_message()
    except ValueError as e:
        await query.message.reply_text(
            f"❗ {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К каталогу", callback_data='catalog')]])
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error in delete_product_handler #{product_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка при удалении: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К каталогу", callback_data='catalog')]])
        )
        await query.delete_message()

@admin_only
async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding a product."""
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message
    logger.info(f"Add product started by user {update.effective_user.id}")
    categories = get_categories()
    if not categories:
        await msg.reply_text("📂 Категорий пока нет. Введите название новой категории:", reply_markup=get_back_keyboard())
        logger.info(f"No categories found, transitioning to NEW_CATEGORY for user {update.effective_user.id}")
        return NEW_CATEGORY
    keyboard = [[InlineKeyboardButton(cat['name'], callback_data=f"cat_{cat['id']}")] for cat in categories]
    keyboard.append([InlineKeyboardButton("➕ Новая категория", callback_data="new_category")])
    await msg.reply_text(
        "📦 Выберите категорию или создайте новую:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CATEGORY_CHOICE

@admin_only
async def category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection."""
    query = update.callback_query
    await query.answer()
    logger.info(f"Category choice by user {update.effective_user.id}: {query.data}")
    if query.data == "new_category":
        await query.message.reply_text("📂 Введите название новой категории:", reply_markup=get_back_keyboard())
        return NEW_CATEGORY
    category_id = int(query.data.split("_")[1])
    context.user_data['category_id'] = category_id
    await query.message.reply_text("🆕 Введите название товара:", reply_markup=get_back_keyboard())
    return PROD_NAME

@admin_only
async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new category creation."""
    category_name = update.message.text.strip()
    user_id = update.effective_user.id
    if category_name == "🔙 Назад":
        return await start_command(update, context)
    logger.info(f"New category input: '{category_name}' by user {user_id}")
    if not category_name:
        logger.info(f"Empty category name by user {user_id}")
        await update.message.reply_text("❗ Название категории не может быть пустым. Введите название:", reply_markup=get_back_keyboard())
        return NEW_CATEGORY
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
            existing = cursor.fetchone()
            if existing:
                logger.info(f"Category '{category_name}' already exists with ID: {existing['id']}")
                await update.message.reply_text(
                    f"❗ Категория '{category_name}' уже существует. Введите другое название:", reply_markup=get_back_keyboard()
                )
                return NEW_CATEGORY
        category_id = create_category(category_name)
        context.user_data['category_id'] = category_id
        await update.message.reply_text(
            f"✅ Категория '{category_name}' создана (ID: {category_id}).\n🆕 Введите название товара:", reply_markup=get_back_keyboard()
        )
        return PROD_NAME
    except ValueError as e:
        logger.warning(f"Value error in new_category: {e}")
        await update.message.reply_text(f"❗ {str(e)} Введите другое название:", reply_markup=get_back_keyboard())
        return NEW_CATEGORY
    except Exception as e:
        logger.error(f"Error creating category '{category_name}' for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(f"❗ Ошибка: {str(e)}. Попробуйте снова:", reply_markup=get_back_keyboard())
        return NEW_CATEGORY

@admin_only
async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product name input."""
    prod_name = update.message.text.strip()
    if prod_name == "🔙 Назад":
        await start_command(update, context)
        return ConversationHandler.END
    context.user_data['prod_name'] = prod_name
    await update.message.reply_text("📝 Введите описание товара:", reply_markup=get_back_keyboard())
    return PROD_DESC

@admin_only
async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product description input."""
    prod_desc = update.message.text.strip()
    if prod_desc == "🔙 Назад":
        await start_command(update, context)
        return ConversationHandler.END
    context.user_data['prod_desc'] = prod_desc
    await update.message.reply_text("📏 Укажите размеры (например, 33×45 / 50×50):", reply_markup=get_back_keyboard())
    return PROD_SIZE

@admin_only
async def add_product_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product size input."""
    prod_size = update.message.text.strip()
    if prod_size == "🔙 Назад":
        return await start_command(update, context)
    context.user_data['prod_size'] = prod_size
    await update.message.reply_text("🔍 Укажите материал (например, хлопок, полиэстер):", reply_markup=get_back_keyboard())
    return PROD_MATERIAL

@admin_only
async def add_product_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product material input."""
    prod_material = update.message.text.strip()
    if prod_material == "🔙 Назад":
        return await start_command(update, context)
    context.user_data['prod_material'] = prod_material
    await update.message.reply_text("💰 Введите цену в рублях (число, например, 1000):", reply_markup=get_back_keyboard())
    return PROD_PRICE

@admin_only
async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product price input."""
    txt = update.message.text.strip()
    if txt == "🔙 Назад":
        return await start_command(update, context)
    try:
        price = float(txt)
        if price <= 0:
            raise ValueError("Цена должна быть положительной")
        context.user_data['prod_price'] = price
        await update.message.reply_text("📷 Пришлите фото товара:", reply_markup=get_back_keyboard())
        return PROD_PHOTO
    except ValueError:
        logger.warning(f"Invalid price input: '{txt}' by user {update.effective_user.id}")
        await update.message.reply_text("❗ Введите число, например: 1000", reply_markup=get_back_keyboard())
        return PROD_PRICE

@admin_only
async def add_product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product photo input."""
    user_id = update.effective_user.id
    if update.message.text and update.message.text == "🔙 Назад":
        return await start_command(update, context)
    if not update.message.photo:
        await update.message.reply_text("❗ Ожидается фотография. Пришлите фото товара.", reply_markup=get_back_keyboard())
        return PROD_PHOTO
    photo_file = await update.message.photo[-1].get_file()
    file_extension = '.jpg'
    data = context.user_data
    required_fields = ['category_id', 'prod_name', 'prod_price', 'prod_desc', 'prod_size', 'prod_material']
    if not all(k in data for k in required_fields):
        logger.warning(f"Incomplete product data: {data}")
        await update.message.reply_text("❗ Недостаточно данных для создания товара. Попробуйте заново.", reply_markup=get_back_keyboard())
        return ConversationHandler.END
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO all_info (category_id, prod_name, prod_desc, price, size, material)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (data['category_id'], data['prod_name'], data['prod_desc'], data['prod_price'], data['prod_size'], data['prod_material'])
            )
            product_id = cursor.lastrowid
        product_dir = os.path.join(MEDIA_DIR, str(product_id))
        os.makedirs(product_dir, exist_ok=True)
        photo_filename = f"product_{product_id}{file_extension}"
        photo_path = os.path.join(str(product_id), photo_filename)
        full_photo_path = os.path.join(MEDIA_DIR, photo_path)
        await photo_file.download_to_drive(full_photo_path)
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE all_info SET photo_path = ? WHERE id = ?",
                (photo_path, product_id)
            )
        logger.info(f"Product #{product_id} added by user {user_id} with photo at {photo_path}")
        await update.message.reply_text(f"✅ Товар #{product_id} добавлен успешно.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error adding product: {e}")
        await update.message.reply_text(f"❗ Ошибка при добавлении товара: {str(e)}. Попробуйте снова.", reply_markup=get_back_keyboard())
        return PROD_PHOTO

@admin_only
async def add_promo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding a promotion."""
    msg = get_reply_target(update)
    await msg.reply_text("🎁 Введите название акции:", reply_markup=get_back_keyboard())
    return PROMO_NAME

@admin_only
async def add_promo_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle promotion name input."""
    promo_name = update.message.text.strip()
    if promo_name == "🔙 Назад":
        return await start_command(update, context)
    context.user_data['promo_name'] = promo_name
    await update.message.reply_text("📝 Введите описание акции:", reply_markup=get_back_keyboard())
    return PROMO_DESC

@admin_only
async def add_promo_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle promotion description input."""
    promo_desc = update.message.text.strip()
    if promo_desc == "🔙 Назад":
        return await start_command(update, context)
    context.user_data['promo_desc'] = promo_desc
    await update.message.reply_text("📷 Пришлите изображение акции или введите 'none':", reply_markup=get_back_keyboard())
    return PROMO_IMAGE

@admin_only
async def add_promo_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle promotion image input."""
    if update.message.text and update.message.text == "🔙 Назад":
        return await start_command(update, context)
    if update.message.photo:
        context.user_data['promo_image'] = update.message.photo[-1].file_id
    else:
        txt = update.message.text.strip().lower()
        context.user_data['promo_image'] = None if txt == 'none' else txt
    await update.message.reply_text("🕒 Введите дату начала (YYYY-MM-DD):", reply_markup=get_back_keyboard())
    return PROMO_START

@admin_only
async def add_promo_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle promotion start date input."""
    txt = update.message.text.strip()
    if txt == "🔙 Назад":
        return await start_command(update, context)
    try:
        start_date = datetime.strptime(txt, '%Y-%m-%d').date()
        if start_date < datetime.now().date():
            await update.message.reply_text("❗ Дата начала не может быть в прошлом. Введите дату (YYYY-MM-DD):", reply_markup=get_back_keyboard())
            return PROMO_START
        context.user_data['promo_start'] = txt
        await update.message.reply_text("🕒 Введите дату окончания (YYYY-MM-DD):", reply_markup=get_back_keyboard())
        return PROMO_END
    except ValueError:
        await update.message.reply_text("❗ Неверный формат. Введите дату в формате YYYY-MM-DD:", reply_markup=get_back_keyboard())
        return PROMO_START

@admin_only
async def add_promo_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle promotion end date input."""
    txt = update.message.text.strip()
    if txt == "🔙 Назад":
        return await start_command(update, context)
    try:
        end_date = datetime.strptime(txt, '%Y-%m-%d').date()
        start_date = datetime.strptime(context.user_data['promo_start'], '%Y-%m-%d').date()
        if end_date < start_date:
            await update.message.reply_text(
                "❗ Дата окончания не может быть раньше даты начала. Введите дату (YYYY-MM-DD):", reply_markup=get_back_keyboard())
            return PROMO_END
        data = context.user_data
        pid = create_promotion(
            data['promo_name'],
            data['promo_desc'],
            data.get('promo_image'),
            data['promo_start'],
            txt
        )
        await update.message.reply_text(f"✅ Акция #{pid} добавлена.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❗ Неверный формат. Введите дату в формате YYYY-MM-DD:", reply_markup=get_back_keyboard())
        return PROMO_END

@admin_only
async def list_promos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all promotions."""
    msg = get_reply_target(update)
    promos = fetch_promotions()
    if not promos:
        await msg.reply_text("🎁 Акций нет.")
        return
    text = "\n".join(
        f"#{p['id']}: {p['name']} ({p['start_date']}–{p['end_date']})\n{p['description'] or 'Без описания'}"
        for p in promos
    )
    await msg.reply_text(f"📋 Список акций:\n\n{text}")

@admin_only
async def remove_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a promotion."""
    msg = get_reply_target(update)
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("Использование: /remove_promo <id>")
        return
    promo_id = int(context.args[0])
    try:
        delete_promotion(promo_id)
        await msg.reply_text(f"✅ Акция #{promo_id} удалена.")
    except ValueError as e:
        await msg.reply_text(f"❗ {str(e)}")
    except Exception as e:
        logger.error(f"Error removing promotion #{promo_id}: {e}")
        await msg.reply_text(f"❗ Ошибка: {str(e)}")

@admin_only
async def add_promo_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать создание промокода."""
    msg = get_reply_target(update)
    await msg.reply_text("🎟 Введите промокод (латинскими буквами):", reply_markup=get_back_keyboard())
    return PROMO_CODE

@admin_only
async def add_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать ввод промокода."""
    promo_code = update.message.text.strip().upper()
    if promo_code == "🔙 Назад":
        return await start_command(update, context)
    if not promo_code.isalnum():
        await update.message.reply_text("❗ Промокод должен содержать только латинские буквы и цифры. Попробуйте снова:", reply_markup=get_back_keyboard())
        return PROMO_CODE
    context.user_data['promo_code'] = promo_code
    products = fetch_products()
    if not products:
        await update.message.reply_text("❗ Нет товаров для привязки. Добавьте товары в каталог.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton(f"#{p['id']} {p['prod_name']}", callback_data=f"promo_product_{p['id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    await update.message.reply_text("🛍 Выберите товар для акции:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PROMO_PRODUCT

@admin_only
async def add_promo_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать выбор товара для промокода."""
    query = update.callback_query
    await query.answer()
    if query.data == "back_to_main":
        return await start_command(update, context)
    product_id = int(query.data.split('_')[2])
    context.user_data['promo_product_id'] = product_id
    product = get_product_by_id(product_id)
    await query.message.reply_text(
        f"Выбран товар: {product['prod_name']}\n💸 Введите процент скидки (число, например, 10):",
        reply_markup=get_back_keyboard()
    )
    return PROMO_DISCOUNT

@admin_only
async def add_promo_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать ввод процента скидки."""
    txt = update.message.text.strip()
    if txt == "🔙 Назад":
        return await start_command(update, context)
    try:
        discount = int(txt)
        if not 0 < discount <= 100:
            raise ValueError("Скидка должна быть от 1 до 100%")
        context.user_data['promo_discount'] = discount
        await update.message.reply_text("🕒 Введите дату начала акции (YYYY-MM-DD):", reply_markup=get_back_keyboard())
        return PROMO_CODE_START
    except ValueError:
        await update.message.reply_text("❗ Введите число от 1 до 100:", reply_markup=get_back_keyboard())
        return PROMO_DISCOUNT

@admin_only
async def add_promo_code_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать дату начала акции."""
    txt = update.message.text.strip()
    if txt == "🔙 Назад":
        return await start_command(update, context)
    try:
        start_date = datetime.strptime(txt, '%Y-%m-%d').date()
        if start_date < datetime.now().date():
            await update.message.reply_text("❗ Дата начала не может быть в прошлом. Введите дату (YYYY-MM-DD):", reply_markup=get_back_keyboard())
            return PROMO_CODE_START
        context.user_data['promo_start_date'] = txt
        await update.message.reply_text("🕒 Введите дату окончания акции (YYYY-MM-DD):", reply_markup=get_back_keyboard())
        return PROMO_CODE_END
    except ValueError:
        await update.message.reply_text("❗ Неверный формат. Введите дату в формате YYYY-MM-DD:", reply_markup=get_back_keyboard())
        return PROMO_CODE_START

@admin_only
async def add_promo_code_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработать дату окончания акции и сохранить промокод."""
    txt = update.message.text.strip()
    if txt == "🔙 Назад":
        return await start_command(update, context)
    try:
        end_date = datetime.strptime(txt, '%Y-%m-%d').date()
        start_date = datetime.strptime(context.user_data['promo_start_date'], '%Y-%m-%d').date()
        if end_date < start_date:
            await update.message.reply_text(
                "❗ Дата окончания не может быть раньше даты начала. Введите дату (YYYY-MM-DD):", reply_markup=get_back_keyboard())
            return PROMO_CODE_END
        data = context.user_data
        promo_id = create_promo_code(
            data['promo_code'],
            data['promo_product_id'],
            data['promo_discount'],
            data['promo_start_date'],
            txt
        )
        await update.message.reply_text(f"✅ Промокод #{promo_id} ({data['promo_code']}) добавлен.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END
    except ValueError as ve:
        await update.message.reply_text(f"❗ {str(ve)} Введите другое название:", reply_markup=get_back_keyboard())
        return PROMO_CODE
    except Exception as e:
        logger.error(f"Error creating promo code: {e}")
        await update.message.reply_text("❗ Неверный формат. Введите дату в формате YYYY-MM-DD:", reply_markup=get_back_keyboard())
        return PROMO_CODE_END

@admin_only
async def list_promo_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех промокодов."""
    msg = get_reply_target(update)
    promo_codes = fetch_promo_codes()
    if not promo_codes:
        await msg.reply_text("🎟 Нет активных промокодов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]))
        return
    keyboard = [
        [
            InlineKeyboardButton(f"#{p['id']} {p['code']} ({p['prod_name']}, {p['discount_percentage']}%)", callback_data=f"promo_code_{p['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"deactivate_promo_{p['id']}")
        ]
        for p in promo_codes if p['is_active']
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.reply_text("🎟 Список промокодов:", reply_markup=reply_markup)

@admin_only
async def show_promo_code_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детали промокода."""
    query = update.callback_query
    await query.answer()
    promo_id = int(query.data.split('_')[2])
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT pc.id, pc.code, pc.product_id, p.prod_name, pc.discount_percentage, pc.start_date, pc.end_date, pc.is_active
                FROM promo_codes pc
                JOIN all_info p ON pc.product_id = p.id
                WHERE pc.id = ?
                """,
                (promo_id,)
            )
            promo = cursor.fetchone()
        if not promo:
            await query.message.reply_text(
                "❌ Промокод не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К промокодам", callback_data='list_promo_codes')]])
            )
            return
        text = (
            f"<b>Промокод #{promo['id']}</b>\n"
            f"Код: {promo['code']}\n"
            f"Товар: {promo['prod_name']}\n"
            f"Скидка: {promo['discount_percentage']}%\n"
            f"Дата начала: {promo['start_date']}\n"
            f"Дата окончания: {promo['end_date']}\n"
            f"Статус: {'Активен' if promo['is_active'] else 'Неактивен'}"
        )
        keyboard = [
            [InlineKeyboardButton("🗑 Деактивировать", callback_data=f"deactivate_promo_{promo_id}")],
            [InlineKeyboardButton("🔙 К промокодам", callback_data='list_promo_codes')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error showing promo code #{promo_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К промокодам", callback_data='list_promo_codes')]])
        )

@admin_only
async def deactivate_promo_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Деактивировать промокод."""
    query = update.callback_query
    await query.answer()
    promo_id = int(query.data.split('_')[2])
    logger.info(f"Deactivating promo code #{promo_id} by user {update.effective_user.id}")
    try:
        deactivate_promo_code(promo_id)
        await query.message.reply_text(
            f"✅ Промокод #{promo_id} деактивирован.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К промокодам", callback_data='list_promo_codes')]])
        )
        await query.delete_message()
    except ValueError as e:
        await query.message.reply_text(
            f"❗ {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К промокодам", callback_data='list_promo_codes')]])
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error deactivating promo code #{promo_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка при деактивации: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К промокодам", callback_data='list_promo_codes')]])
        )
        await query.delete_message()

@admin_only
async def mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start creating a mailing."""
    msg = get_reply_target(update)
    users = fetch_users()
    logger.info(f"Starting mailing for user {update.effective_user.id}, found {len(users)} users")
    if not users:
        await msg.reply_text("❗ Нет пользователей для рассылки. Добавьте пользователей в telegram_profiles.")
        return ConversationHandler.END
    await msg.reply_text(f"✉️ Введите текст рассылки (будет отправлено {len(users)} пользователям):", reply_markup=get_back_keyboard())
    return MAIL_CONTENT

@admin_only
async def mailing_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mailing content input."""
    logger.debug(f"Processing mailing content for user {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.warning(f"No text message received for mailing content from user {update.effective_user.id}")
        await update.message.reply_text("❗ Пожалуйста, введите текст рассылки.", reply_markup=get_back_keyboard())
        return MAIL_CONTENT
    mail_content = update.message.text.strip()
    logger.info(f"Mailing content received: '{mail_content}' from user {update.effective_user.id}")
    if mail_content == "🔙 Назад":
        logger.info(f"User {update.effective_user.id} canceled mailing with back button")
        await start_command(update, context)
        return ConversationHandler.END
    if not mail_content:
        logger.warning(f"Empty mailing content from user {update.effective_user.id}")
        await update.message.reply_text("❗ Текст рассылки не может быть пустым. Введите текст:", reply_markup=get_back_keyboard())
        return MAIL_CONTENT
    context.user_data['mail_content'] = mail_content
    await update.message.reply_text("⏱ Введите, через сколько минут отправить рассылку (число, например, 5):", reply_markup=get_back_keyboard())
    logger.debug(f"Transitioning to MAIL_TIMER for user {update.effective_user.id}")
    return MAIL_TIMER

@admin_only
async def mailing_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mailing timer input."""
    logger.debug(f"Processing mailing timer for user {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.warning(f"No text message received for mailing timer from user {update.effective_user.id}")
        await update.message.reply_text("❗ Пожалуйста, введите число минут.", reply_markup=get_back_keyboard())
        return MAIL_TIMER
    txt = update.message.text.strip()
    logger.info(f"Mailing timer input: '{txt}' from user {update.effective_user.id}")
    if txt == "🔙 Назад":
        logger.info(f"User {update.effective_user.id} canceled mailing timer with back button")
        await start_command(update, context)
        return ConversationHandler.END
    try:
        minutes = int(txt)
        if minutes < 1:
            logger.warning(f"Invalid timer input: {minutes} minutes from user {update.effective_user.id}")
            await update.message.reply_text(
                "❗ Время должно быть больше 0 минут. Введите число:", reply_markup=get_back_keyboard())
            return MAIL_TIMER
        send_dt = datetime.now() + timedelta(minutes=minutes)
        mail_content = context.user_data.get('mail_content')
        if not mail_content:
            logger.error(f"No mail_content found in user_data for user {update.effective_user.id}")
            await update.message.reply_text(
                "❗ Ошибка: текст рассылки не найден. Начните заново.", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO mailings (content, send_at, status)
                VALUES (?, ?, 'scheduled')
                """,
                (mail_content, send_dt)
            )
            mid = cursor.lastrowid
        await update.message.reply_text(
            f"✅ Рассылка #{mid} запланирована на {send_dt.strftime('%Y-%m-%d %H:%M')} через юзер-бота.",
            reply_markup=ReplyKeyboardRemove())
        logger.info(f"Mailing #{mid} scheduled successfully for user {update.effective_user.id}")
        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        logger.warning(f"Invalid timer input: '{txt}' from user {update.effective_user.id}")
        await update.message.reply_text(
            "❗ Введите целое число минут, например, 5:", reply_markup=get_back_keyboard())
        return MAIL_TIMER
    except Exception as e:
        logger.error(f"Error scheduling mailing for user {update.effective_user.id}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❗ Ошибка при планировании рассылки: {str(e)}.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

@admin_only
async def view_mailings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display all mailings with delete buttons."""
    msg = get_reply_target(update)
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, content, send_at, status
                FROM mailings
                ORDER BY send_at
                """
            )
            mailings = [dict(row) for row in cursor.fetchall()]
        if not mailings:
            await msg.reply_text("✉️ Нет рассылок.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]))
            return
        keyboard = [
            [
                InlineKeyboardButton(f"#{m['id']} {m['content'][:20]}... ({m['send_at']})", callback_data=f"mailing_{m['id']}"),
                InlineKeyboardButton("🗑", callback_data=f"delete_mailing_{m['id']}")
            ]
            for m in mailings
        ]
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.reply_text("📬 Список рассылок:", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error listing mailings: {e}")
        await msg.reply_text(f"❗ Ошибка: {str(e)}")

@admin_only
async def show_mailing_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display mailing details."""
    query = update.callback_query
    await query.answer()
    mailing_id = int(query.data.split('_')[1])
    logger.info(f"Showing mailing #{mailing_id} for user {update.effective_user.id}")
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, content, send_at, status
                FROM mailings
                WHERE id = ?
                """,
                (mailing_id,)
            )
            mailing = cursor.fetchone()
        if not mailing:
            await query.message.reply_text(
                "❌ Рассылка не найдена.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К рассылкам", callback_data='view_mailings')]])
            )
            return
        text = (
            f"<b>Рассылка #{mailing['id']}</b>\n"
            f"Текст: {mailing['content']}\n"
            f"Время отправки: {mailing['send_at']}\n"
            f"Статус: {mailing['status']}"
        )
        keyboard = [
            [InlineKeyboardButton("🗑 Удалить рассылку", callback_data=f"delete_mailing_{mailing_id}")],
            [InlineKeyboardButton("🔙 К рассылкам", callback_data='view_mailings')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error showing mailing #{mailing_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К рассылкам", callback_data='view_mailings')]])
        )

@admin_only
async def delete_mailing_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mailing deletion."""
    query = update.callback_query
    await query.answer()
    mailing_id = int(query.data.split('_')[2])
    logger.info(f"Deleting mailing #{mailing_id} by user {update.effective_user.id}")
    try:
        delete_mailing(mailing_id)
        await query.message.reply_text(
            f"✅ Рассылка #{mailing_id} удалена.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К рассылкам", callback_data='view_mailings')]])
        )
        await query.delete_message()
    except ValueError as e:
        await query.message.reply_text(
            f"❗ {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К рассылкам", callback_data='view_mailings')]])
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error in delete_mailing_handler #{mailing_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка при удалении: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К рассылкам", callback_data='view_mailings')]])
        )
        await query.delete_message()

@admin_only
async def support_requests_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display current support requests."""
    msg = get_reply_target(update)
    logger.info(f"Support requests menu requested by user {update.effective_user.id}")
    requests = fetch_support_requests()
    if not requests:
        await msg.reply_text("📩 Нет активных запросов поддержки.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')]]))
        return
    keyboard = [
        [InlineKeyboardButton(f"#{r['id']} @{r['username'] or 'N/A'} ({r['created_at']})", callback_data=f"support_{r['id']}")]
        for r in requests
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_main')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.reply_text("📩 Выберите запрос поддержки для просмотра:", reply_markup=reply_markup)

@admin_only
async def show_support_request_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display support request details with block option."""
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split('_')[1])
    logger.info(f"Showing support request #{request_id} for user {update.effective_user.id}")
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_id, username, content, created_at
                FROM support_requests
                WHERE id = ?
                """,
                (request_id,)
            )
            request = cursor.fetchone()
        if not request:
            await query.message.reply_text(
                "❌ Запрос поддержки не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
            )
            return
        text = (
            f"<b>Запрос поддержки #{request['id']}</b>\n"
            f"Пользователь: @{request['username'] or 'Не указан'} (ID: {request['user_id']})\n"
            f"Время: {request['created_at']}\n"
            f"Сообщение: {request['content']}"
        )
        keyboard = [
            [InlineKeyboardButton("🚫 Заблокировать пользователя", callback_data=f"block_user_{request['user_id']}_{request['id']}")],
            [InlineKeyboardButton("🗑 Удалить запрос", callback_data=f"delete_support_{request['id']}")],
            [InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error showing support request #{request_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
        )

@admin_only
async def delete_support_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support request deletion."""
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split('_')[2])
    logger.info(f"Deleting support request #{request_id} by user {update.effective_user.id}")
    try:
        delete_support_request(request_id)
        await query.message.reply_text(
            f"✅ Запрос поддержки #{request_id} удалён.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
        )
        await query.delete_message()
    except ValueError as e:
        await query.message.reply_text(
            f"❗ {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error in delete_support_request_handler #{request_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка при удалении: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
        )
        await query.delete_message()

@admin_only
async def block_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user blocking."""
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    user_id = int(data[2])
    request_id = int(data[3])
    logger.info(f"Blocking user {user_id} from support request #{request_id} by admin {update.effective_user.id}")
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM telegram_profiles WHERE telegram_id = ?", (user_id,))
            cursor.execute("DELETE FROM buy WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM support_requests WHERE user_id = ?", (user_id,))
        await query.message.reply_text(
            f"✅ Пользователь {user_id} заблокирован и все его данные удалены.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Error blocking user {user_id}: {e}")
        await query.message.reply_text(
            f"❗ Ошибка при блокировке пользователя: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К запросам", callback_data='support_requests')]])
        )
        await query.delete_message()

@admin_only
async def mailing_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any conversation."""
    logger.info(f"Conversation cancelled by user {update.effective_user.id}")
    context.user_data.clear()
    await update.message.reply_text("🚫 Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return await start_command(update, context)

@admin_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries."""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    logger.info(f"Callback '{data}' from user {user_id}")
    try:
        await query.answer()
    except BadRequest as e:
        if "query is too old" not in str(e):
            logger.warning(f"BadRequest on callback answer: {e}")
    if user_id not in ADMIN_IDS:
        await query.message.reply_text("❌ Доступ запрещён.")
        return
    match data:
        case "analytics":
            await analytics_command(update, context)
        case "promos":
            await add_promo_start(update, context)
        case "promo_codes":
            await list_promo_codes(update, context)
        case "catalog":
            await catalog_menu(update, context)
        case "categories":
            await categories_menu(update, context)
        case "mailing":
            await mailing_start(update, context)
        case "add_product":
            await add_product_start(update, context)
        case "view_mailings":
            await view_mailings(update, context)
        case "support_requests":
            await support_requests_menu(update, context)
        case data if data.startswith("support_"):
            await show_support_request_details(update, context)
        case data if data.startswith("delete_support_"):
            await delete_support_request_handler(update, context)
        case data if data.startswith("block_user_"):
            await block_user_handler(update, context)
        case data if data.startswith("product_"):
            await show_product_details(update, context)
        case data if data.startswith("delete_product_"):
            await delete_product_handler(update, context)
        case data if data.startswith("category_"):
            await show_category_details(update, context)
        case data if data.startswith("delete_category_"):
            await delete_category_handler(update, context)
        case data if data.startswith("mailing_"):
            await show_mailing_details(update, context)
        case data if data.startswith("delete_mailing_"):
            await delete_mailing_handler(update, context)
        case data if data.startswith("promo_code_"):
            await show_promo_code_details(update, context)
        case data if data.startswith("deactivate_promo_"):
            await deactivate_promo_code_handler(update, context)
        case data if data.startswith("list_promo_codes"):
            await list_promo_codes(update, context)
        case data if data.startswith("back_to_main"):
            await start_command(update, context)
        case _:
            await category_choice(update, context)

# --- Main ---
def main():
    """Start the bot."""
    try:
        logger.info("Starting admin bot")
        init_db()
        app = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()

        # Product addition conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(add_product_start, pattern="^add_product$")],
            states={
                CATEGORY_CHOICE: [CallbackQueryHandler(category_choice)],
                NEW_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_category)],
                PROD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
                PROD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_desc)],
                PROD_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_size)],
                PROD_MATERIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_material)],
                PROD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_price)],
                PROD_PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, add_product_photo)],
            },
            fallbacks=[
                CommandHandler('cancel', mailing_cancel),
                MessageHandler(filters.Regex('^🔙 Назад$'), mailing_cancel)
            ],
            per_chat=True,
            per_user=True,
            name='product_conversation'
        )

        # Promotion conversation handler
        promo_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(add_promo_start, pattern="^promos$")],
            states={
                PROMO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_name)],
                PROMO_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_desc)],
                PROMO_IMAGE: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, add_promo_image)],
                PROMO_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_start_date)],
                PROMO_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_end_date)],
            },
            fallbacks=[
                CommandHandler('cancel', mailing_cancel),
                MessageHandler(filters.Regex('^🔙 Назад$'), mailing_cancel)
            ],
            per_chat=True,
            per_user=True,
            name='promo_conversation'
        )

        # Promo code conversation handler
        promo_code_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(add_promo_code_start, pattern="^promo_codes$")],
            states={
                PROMO_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_code)],
                PROMO_PRODUCT: [CallbackQueryHandler(add_promo_product)],
                PROMO_DISCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_discount)],
                PROMO_CODE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_code_start_date)],
                PROMO_CODE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_promo_code_end_date)],
            },
            fallbacks=[
                CommandHandler('cancel', mailing_cancel),
                MessageHandler(filters.Regex('^🔙 Назад$'), mailing_cancel)
            ],
            per_chat=True,
            per_user=True,
            name='promo_code_conversation'
        )

        # Mailing conversation handler
        mailing_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(mailing_start, pattern="^mailing$")],
            states={
                MAIL_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, mailing_content)],
                MAIL_TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, mailing_timer)],
            },
            fallbacks=[
                CommandHandler('cancel', mailing_cancel),
                MessageHandler(filters.Regex('^🔙 Назад$'), mailing_cancel)
            ],
            per_chat=True,
            per_user=True,
            name='mailing_conversation'
        )

        # Add handlers
        app.add_handler(CommandHandler('start', start_command))
        app.add_handler(CommandHandler('analytics', analytics_command))
        app.add_handler(CommandHandler('remove_promo', remove_promo))
        app.add_handler(conv_handler)
        app.add_handler(promo_conv)
        app.add_handler(promo_code_conv)
        app.add_handler(mailing_conv)
        app.add_handler(CallbackQueryHandler(on_callback))

        # Start periodic jobs
        app.job_queue.run_repeating(check_support_requests, interval=60, first=10)
        app.job_queue.run_repeating(check_scheduled_mailings, interval=60, first=10)

        logger.info("Bot is running...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        raise

if __name__ == '__main__':
    main()
