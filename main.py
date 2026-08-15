import os
import asyncio
import logging
from datetime import datetime, timedelta
import secrets

# Telegram imports
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import Client
from pyrogram.errors import FloodWait

# Database imports
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Railway environment variables
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

# Database URL - try multiple options
def get_database_url():
    """Try different database URLs in order"""
    
    # 1. Try DATABASE_PUBLIC_URL first (for external connections)
    public_url = os.getenv('DATABASE_PUBLIC_URL', '')
    if public_url:
        if public_url.startswith('postgres://'):
            public_url = public_url.replace('postgres://', 'postgresql://', 1)
        logger.info("Using DATABASE_PUBLIC_URL")
        return public_url.strip()
    
    # 2. Try DATABASE_URL
    db_url = os.getenv('DATABASE_URL', '')
    if db_url:
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        
        # Check if it's internal URL
        if 'railway.internal' in db_url:
            # Try to use public URL instead
            logger.info("Internal URL detected, trying public URL")
            # Try to construct public URL from parts
            pg_host = os.getenv('PGHOST', '')
            if pg_host and 'railway.internal' in pg_host:
                # Use proxy host
                public_host = pg_host.replace('postgres.railway.internal', 'altaria.proxy.rlwy.net')
                pg_port = os.getenv('PGPORT', '5432')
                pg_user = os.getenv('PGUSER', 'postgres')
                pg_password = os.getenv('PGPASSWORD', '')
                pg_database = os.getenv('PGDATABASE', 'railway')
                
                # Use port 50439 for public access
                public_url = f'postgresql://{pg_user}:{pg_password}@{public_host}:50439/{pg_database}'
                logger.info("Constructed public URL")
                return public_url
        
        logger.info("Using DATABASE_URL")
        return db_url.strip()
    
    # 3. Try individual variables
    pg_host = os.getenv('PGHOST', '')
    pg_port = os.getenv('PGPORT', '5432')
    pg_user = os.getenv('PGUSER', '')
    pg_password = os.getenv('PGPASSWORD', '')
    pg_database = os.getenv('PGDATABASE', '')
    
    if pg_host and pg_user and pg_database:
        # Use public proxy
        if 'railway.internal' in pg_host:
            pg_host = 'altaria.proxy.rlwy.net'
            pg_port = '50439'
        
        db_url = f'postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}'
        logger.info("Built URL from individual variables")
        return db_url
    
    # 4. Fallback to SQLite
    logger.warning("No database URL found, using SQLite")
    return 'sqlite:///bot.db'

DATABASE_URL = get_database_url()
logger.info(f"Database URL: {DATABASE_URL[:50]}...")

# Create engine
try:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        connect_args={'sslmode': 'disable'} if 'postgresql' in DATABASE_URL else {}
    )
    # Test connection
    with engine.connect() as conn:
        conn.execute("SELECT 1")
    logger.info("Database connection successful!")
except Exception as e:
    logger.error(f"Database error: {e}")
    logger.info("Using SQLite fallback")
    DATABASE_URL = 'sqlite:///bot.db'
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# Models
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    license_key = Column(String, unique=True, nullable=True)
    license_expiry = Column(DateTime, nullable=True)
    is_blocked = Column(Boolean, default=False)
    phone_number = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    session_string = Column(Text, nullable=True)

class LicenseKey(Base):
    __tablename__ = 'license_keys'
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    duration_days = Column(Integer, default=30)
    is_used = Column(Boolean, default=False)
    used_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BroadcastTask(Base):
    __tablename__ = 'broadcast_tasks'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    message_text = Column(Text, nullable=False)
    interval_minutes = Column(Integer, default=30)
    status = Column(String, default='active')
    groups_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# States
class UserStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_message = State()
    waiting_interval = State()
    waiting_license = State()

# Initialize bot
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

active_clients = {}

# Helper functions
def generate_license_key(duration_days: int) -> str:
    db = SessionLocal()
    try:
        while True:
            key = f"LIC-{secrets.token_urlsafe(16).upper()}"
            if not db.query(LicenseKey).filter_by(key=key).first():
                break
        return key
    finally:
        db.close()

def is_valid_license(user_id: int) -> bool:
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=user_id).first()
        if not user or user.is_blocked:
            return False
        if not user.license_expiry:
            return False
        if user.license_expiry < datetime.utcnow():
            return False
        return True
    finally:
        db.close()

def create_user_if_not_exists(user_id: int, username: str = None, first_name: str = None):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=user_id).first()
        if not user:
            user = User(user_id=user_id, username=username, first_name=first_name)
            db.add(user)
            db.commit()
        return user
    finally:
        db.close()

# Keyboards
def get_main_keyboard(user_id: int):
    keyboard = InlineKeyboardMarkup(row_width=2)
    if is_valid_license(user_id):
        keyboard.add(
            InlineKeyboardButton("📱 Подключить аккаунт", callback_data="connect_account"),
            InlineKeyboardButton("📨 Создать рассылку", callback_data="create_broadcast"),
            InlineKeyboardButton("⏹ Остановить", callback_data="stop_broadcasts"),
            InlineKeyboardButton("📊 Мои рассылки", callback_data="my_broadcasts")
        )
    else:
        keyboard.add(InlineKeyboardButton("🔑 Активировать лицензию", callback_data="activate_license"))
    return keyboard

def get_admin_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_create_key"),
        InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
        InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton("🚫 Заблокировать", callback_data="admin_block_user")
    )
    return keyboard

# User handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я бот для рассылки сообщений.\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    await message.answer("🔐 Админ-панель:", reply_markup=get_admin_keyboard())

@dp.callback_query_handler(lambda c: c.data == 'activate_license')
async def process_activate_license(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, "Введите лицензионный ключ:")
    await UserStates.waiting_license.set()

@dp.message_handler(state=UserStates.waiting_license)
async def process_license_key(message: types.Message, state: FSMContext):
    license_key = message.text.strip()
    db = SessionLocal()
    try:
        license_obj = db.query(LicenseKey).filter_by(key=license_key).first()
        if not license_obj:
            await message.answer("❌ Неверный ключ")
            return
        if license_obj.is_used:
            await message.answer("❌ Ключ уже использован")
            return
        
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if not user:
            await message.answer("❌ Нажмите /start")
            return
        
        if license_obj.duration_days == -1:
            expiry = datetime.utcnow() + timedelta(days=36500)
        else:
            expiry = datetime.utcnow() + timedelta(days=license_obj.duration_days)
        
        user.license_key = license_key
        user.license_expiry = expiry
        license_obj.is_used = True
        license_obj.used_by = user.user_id
        db.commit()
        
        await message.answer(f"✅ Лицензия активирована до {expiry.strftime('%d.%m.%Y')}")
        await state.finish()
    finally:
        db.close()

@dp.callback_query_handler(lambda c: c.data == 'admin_create_key')
async def process_admin_create_key(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "⛔️ Нет доступа")
        return
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, "Введите срок в днях (-1 для бессрочного):")
    await dp.current_state(user=callback_query.from_user.id).set_state('admin_create_key')

@dp.message_handler(lambda message: message.from_user.id == ADMIN_ID and dp.current_state(user=message.from_user.id).get_state() == 'admin_create_key')
async def process_admin_key_duration(message: types.Message, state: FSMContext):
    try:
        duration = int(message.text)
        key = generate_license_key(duration)
        db = SessionLocal()
        try:
            license_obj = LicenseKey(key=key, duration_days=duration)
            db.add(license_obj)
            db.commit()
        finally:
            db.close()
        
        duration_text = "Бессрочная" if duration == -1 else f"{duration} дней"
        await message.answer(f"✅ Ключ: <code>{key}</code>\nСрок: {duration_text}")
        await state.finish()
    except ValueError:
        await message.answer("❌ Введите число")

@dp.callback_query_handler(lambda c: c.data == 'admin_users')
async def process_admin_users(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return
    db = SessionLocal()
    try:
        users = db.query(User).all()
    finally:
        db.close()
    
    if not users:
        await bot.answer_callback_query(callback_query.id, "Нет пользователей")
        return
    
    text = "👥 Пользователи:\n\n"
    for user in users:
        status = "🚫" if user.is_blocked else "✅"
        text += f"{status} {user.user_id} - {user.first_name or 'Нет имени'}\n"
    
    await bot.send_message(callback_query.from_user.id, text)

@dp.callback_query_handler(lambda c: c.data == 'admin_stats')
async def process_admin_stats(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_keys = db.query(LicenseKey).count()
        used_keys = db.query(LicenseKey).filter_by(is_used=True).count()
    finally:
        db.close()
    
    await bot.send_message(
        callback_query.from_user.id,
        f"📊 Статистика:\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"🔑 Ключей: {total_keys}\n"
        f"📤 Использовано: {used_keys}"
    )

# Startup
async def on_startup(dp):
    logger.info("Bot started!")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен!")
    except:
        pass

# Main
if __name__ == '__main__':
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
