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
from sqlalchemy import create_engine, Column, BigInteger, String, DateTime, Boolean, Text, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker

# Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Railway environment variables
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

# Проверка обязательных переменных
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не указан!")
    raise ValueError("BOT_TOKEN is required")

logger.info(f"✅ Bot token: {BOT_TOKEN[:10]}...")
logger.info(f"✅ Admin ID: {ADMIN_ID}")

# Database URL
def get_database_url():
    public_url = os.getenv('DATABASE_PUBLIC_URL', '')
    if public_url:
        if public_url.startswith('postgres://'):
            public_url = public_url.replace('postgres://', 'postgresql://', 1)
        logger.info("Using DATABASE_PUBLIC_URL")
        return public_url.strip()
    
    db_url = os.getenv('DATABASE_URL', '')
    if db_url:
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        logger.info("Using DATABASE_URL")
        return db_url.strip()
    
    logger.info("Using SQLite")
    return 'sqlite:///bot.db'

DATABASE_URL = get_database_url()

# Create engine
try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("✅ Database connection successful!")
except Exception as e:
    logger.error(f"❌ Database error: {e}")
    DATABASE_URL = 'sqlite:///bot.db'
    engine = create_engine(DATABASE_URL)
    logger.info("Using SQLite fallback")

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# Models - используем BigInteger для user_id
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)  # Изменено на BigInteger
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
    used_by = Column(BigInteger, nullable=True)  # Изменено на BigInteger
    created_at = Column(DateTime, default=datetime.utcnow)

class BroadcastTask(Base):
    __tablename__ = 'broadcast_tasks'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)  # Изменено на BigInteger
    message_text = Column(Text, nullable=False)
    interval_minutes = Column(Integer, default=30)
    status = Column(String, default='active')
    groups_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Удаляем старые таблицы и создаем новые
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
logger.info("✅ Tables created")

# States
class UserStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_message = State()
    waiting_interval = State()
    waiting_license = State()
    admin_create_key = State()  # Добавлено состояние для админа

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
            logger.info(f"New user created: {user_id}")
        return user
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        db.rollback()
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
    logger.info(f"Start command from user {message.from_user.id}")
    create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"Я бот для рассылки сообщений в группы.\n\n"
        f"Выберите действие:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    logger.info(f"Admin command from user {message.from_user.id}")
    
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ У вас нет доступа к админ-панели.")
        return
    
    await message.answer(
        "🔐 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == 'activate_license')
async def process_activate_license(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, "🔑 Введите ваш лицензионный ключ:")
    await UserStates.waiting_license.set()

@dp.message_handler(state=UserStates.waiting_license)
async def process_license_key(message: types.Message, state: FSMContext):
    license_key = message.text.strip()
    
    db = SessionLocal()
    try:
        license_obj = db.query(LicenseKey).filter_by(key=license_key).first()
        
        if not license_obj:
            await message.answer("❌ Неверный ключ. Попробуйте снова.")
            return
        
        if license_obj.is_used:
            await message.answer("❌ Этот ключ уже использован.")
            return
        
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if not user:
            await message.answer("❌ Нажмите /start сначала.")
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
        
        await message.answer(
            f"✅ <b>Лицензия активирована!</b>\n\n"
            f"📅 Действует до: <b>{expiry.strftime('%d.%m.%Y')}</b>"
        )
        await state.finish()
    except Exception as e:
        logger.error(f"License activation error: {e}")
        await message.answer("❌ Ошибка активации.")
        await state.finish()
    finally:
        db.close()

@dp.callback_query_handler(lambda c: c.data == 'admin_create_key')
async def process_admin_create_key(callback_query: types.CallbackQuery):
    logger.info(f"Admin create key from user {callback_query.from_user.id}")
    
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "⛔️ Нет доступа", show_alert=True)
        return
    
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "🔑 <b>Создание ключа</b>\n\n"
        "Введите срок действия:\n"
        "• <code>30</code> - на месяц\n"
        "• <code>365</code> - на год\n"
        "• <code>-1</code> - навсегда"
    )
    await UserStates.admin_create_key.set()  # Используем состояние из UserStates

@dp.message_handler(state=UserStates.admin_create_key)
async def process_admin_key_duration(message: types.Message, state: FSMContext):
    logger.info(f"Processing key duration: {message.text}")
    
    try:
        duration = int(message.text)
        key = generate_license_key(duration)
        
        db = SessionLocal()
        try:
            license_obj = LicenseKey(key=key, duration_days=duration)
            db.add(license_obj)
            db.commit()
            logger.info(f"Key created: {key}")
        except Exception as e:
            logger.error(f"Error creating key: {e}")
            db.rollback()
            await message.answer("❌ Ошибка создания ключа.")
            await state.finish()
            return
        finally:
            db.close()
        
        duration_text = "Бессрочная" if duration == -1 else f"{duration} дней"
        
        await message.answer(
            f"✅ <b>Ключ создан!</b>\n\n"
            f"🔑 Ключ: <code>{key}</code>\n"
            f"📅 Срок: {duration_text}\n\n"
            f"Отправьте этот ключ пользователю."
        )
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите число.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.answer("❌ Произошла ошибка.")
        await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'admin_users')
async def process_admin_users(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "⛔️ Нет доступа", show_alert=True)
        return
    
    await bot.answer_callback_query(callback_query.id)
    
    db = SessionLocal()
    try:
        users = db.query(User).all()
        
        if not users:
            await bot.send_message(callback_query.from_user.id, "Нет пользователей.")
            return
        
        text = "👥 <b>Пользователи:</b>\n\n"
        for user in users:
            status = "🚫" if user.is_blocked else "✅"
            license_status = "✅" if user.license_expiry and user.license_expiry > datetime.utcnow() else "❌"
            text += f"{status} ID: <code>{user.user_id}</code>\n"
            text += f"👤 {user.first_name or 'Нет имени'}"
            if user.username:
                text += f" (@{user.username})"
            text += f"\n🔑 Лицензия: {license_status}\n\n"
        
        await bot.send_message(callback_query.from_user.id, text)
    except Exception as e:
        logger.error(f"Error listing users: {e}")
        await bot.send_message(callback_query.from_user.id, "❌ Ошибка.")
    finally:
        db.close()

@dp.callback_query_handler(lambda c: c.data == 'admin_stats')
async def process_admin_stats(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "⛔️ Нет доступа", show_alert=True)
        return
    
    await bot.answer_callback_query(callback_query.id)
    
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_keys = db.query(LicenseKey).count()
        used_keys = db.query(LicenseKey).filter_by(is_used=True).count()
        
        await bot.send_message(
            callback_query.from_user.id,
            f"📊 <b>Статистика:</b>\n\n"
            f"👥 Пользователей: <b>{total_users}</b>\n"
            f"🔑 Ключей: <b>{total_keys}</b>\n"
            f"📤 Использовано: <b>{used_keys}</b>"
        )
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await bot.send_message(callback_query.from_user.id, "❌ Ошибка.")
    finally:
        db.close()

# Startup
async def on_startup(dp):
    logger.info("✅ Bot started successfully!")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен!\n\nИспользуйте /admin для управления.")
        logger.info("Startup message sent to admin")
    except Exception as e:
        logger.error(f"Error sending startup message: {e}")

# Error handler
@dp.errors_handler()
async def errors_handler(update, error):
    logger.error(f"Update: {update} \nError: {error}")
    return True

# Main
if __name__ == '__main__':
    logger.info("🚀 Starting bot...")
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
