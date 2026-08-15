import os
import asyncio
import logging
from datetime import datetime, timedelta
import secrets
import re

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
from sqlalchemy.pool import NullPool

# Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Railway environment variables
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

# Database setup for Railway
def get_database_url():
    """Get and fix database URL from environment"""
    db_url = os.getenv('DATABASE_URL', '')
    
    if not db_url:
        # Fallback to SQLite
        return 'sqlite:///bot_database.db'
    
    # Fix PostgreSQL URL format
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)
    elif db_url.startswith('postgresql://'):
        pass  # Already correct
    else:
        # Try to construct from individual variables
        pg_host = os.getenv('PGHOST', '')
        pg_port = os.getenv('PGPORT', '5432')
        pg_user = os.getenv('PGUSER', '')
        pg_password = os.getenv('PGPASSWORD', '')
        pg_database = os.getenv('PGDATABASE', '')
        
        if pg_host and pg_user and pg_database:
            db_url = f'postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}'
        else:
            return 'sqlite:///bot_database.db'
    
    # Remove any special characters that might cause issues
    db_url = db_url.strip()
    
    # Add SSL mode if not present
    if 'sslmode' not in db_url:
        if '?' in db_url:
            db_url += '&sslmode=disable'
        else:
            db_url += '?sslmode=disable'
    
    logger.info(f"Using database URL: {db_url[:50]}...")  # Log only beginning for security
    
    return db_url

# Create engine with proper settings
DATABASE_URL = get_database_url()

try:
    engine = create_engine(
        DATABASE_URL,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False
    )
    logger.info("Database engine created successfully")
except Exception as e:
    logger.error(f"Error creating engine: {e}")
    # Fallback to SQLite
    engine = create_engine('sqlite:///bot_database.db')

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
    duration_days = Column(Integer, default=30)  # -1 для бессрочной
    is_used = Column(Boolean, default=False)
    used_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BroadcastTask(Base):
    __tablename__ = 'broadcast_tasks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    message_text = Column(Text, nullable=False)
    interval_minutes = Column(Integer, default=30)
    status = Column(String, default='active')  # active, paused, completed
    groups_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
try:
    Base.metadata.create_all(engine)
    logger.info("Database tables created successfully")
except Exception as e:
    logger.error(f"Error creating tables: {e}")

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

# Store active clients and tasks
active_clients = {}

# Helper functions
def generate_license_key(duration_days: int) -> str:
    """Generate unique license key"""
    db = SessionLocal()
    try:
        while True:
            key = f"LIC-{secrets.token_urlsafe(16).upper()}"
            if not db.query(LicenseKey).filter_by(key=key).first():
                break
    finally:
        db.close()
    return key

def is_valid_license(user_id: int) -> bool:
    """Check if user has valid license"""
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
            user = User(
                user_id=user_id,
                username=username,
                first_name=first_name
            )
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
            InlineKeyboardButton("⏹ Остановить рассылки", callback_data="stop_broadcasts"),
            InlineKeyboardButton("📊 Мои рассылки", callback_data="my_broadcasts")
        )
    else:
        keyboard.add(
            InlineKeyboardButton("🔑 Активировать лицензию", callback_data="activate_license")
        )
    
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
    create_user_if_not_exists(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    
    welcome_text = f"""
👋 <b>Привет, {message.from_user.first_name}!</b>

Я бот для автоматической рассылки сообщений в Telegram группы.

<b>Мои возможности:</b>
✅ Подключение вашего аккаунта Telegram
✅ Автоматическая рассылка в группы
✅ Настройка интервала между сообщениями

<b>Как начать:</b>
1️⃣ Активируйте лицензию
2️⃣ Подключите свой аккаунт
3️⃣ Создайте задачу для рассылки

Для получения лицензии обратитесь к администратору.
"""
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard(message.from_user.id))

@dp.callback_query_handler(lambda c: c.data == 'activate_license')
async def process_activate_license(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "🔑 <b>Активация лицензии</b>\n\n"
        "Введите ваш лицензионный ключ:"
    )
    await UserStates.waiting_license.set()

@dp.message_handler(state=UserStates.waiting_license)
async def process_license_key(message: types.Message, state: FSMContext):
    license_key = message.text.strip()
    db = SessionLocal()
    
    try:
        license_obj = db.query(LicenseKey).filter_by(key=license_key).first()
        
        if not license_obj:
            await message.answer("❌ Неверный лицензионный ключ. Попробуйте снова.")
            return
        
        if license_obj.is_used:
            await message.answer("❌ Этот ключ уже был использован.")
            return
        
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        
        if not user:
            await message.answer("❌ Пользователь не найден. Нажмите /start")
            return
        
        if license_obj.duration_days == -1:
            expiry = datetime.utcnow() + timedelta(days=36500)  # ~100 лет
        else:
            expiry = datetime.utcnow() + timedelta(days=license_obj.duration_days)
        
        user.license_key = license_key
        user.license_expiry = expiry
        
        license_obj.is_used = True
        license_obj.used_by = user.user_id
        
        db.commit()
        
        await message.answer(
            f"✅ <b>Лицензия активирована!</b>\n\n"
            f"📅 Действует до: <b>{expiry.strftime('%d.%m.%Y')}</b>\n"
            f"🔑 Ключ: <code>{license_key}</code>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        
        await state.finish()
    finally:
        db.close()

@dp.callback_query_handler(lambda c: c.data == 'connect_account')
async def process_connect_account(callback_query: types.CallbackQuery):
    if not is_valid_license(callback_query.from_user.id):
        await bot.answer_callback_query(callback_query.id, "❌ У вас нет активной лицензии!", show_alert=True)
        return
    
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "📱 <b>Подключение аккаунта Telegram</b>\n\n"
        "Введите номер телефона в международном формате.\n"
        "Например: <code>+79123456789</code>"
    )
    await UserStates.waiting_phone.set()

@dp.message_handler(state=UserStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с '+'. Попробуйте снова.")
        return
    
    await state.update_data(phone=phone)
    
    client = Client(
        f"session_{message.from_user.id}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        await state.update_data(phone_code_hash=sent_code.phone_code_hash)
        await client.disconnect()
        
        await message.answer(
            "📨 <b>Код подтверждения отправлен!</b>\n\n"
            "Введите код, который вы получили:"
        )
        await UserStates.waiting_code.set()
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке кода: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    phone_code_hash = data.get('phone_code_hash')
    
    client = Client(
        f"session_{message.from_user.id}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    
    try:
        await client.connect()
        await client.sign_in(phone, phone_code_hash, code)
        
        session_string = await client.export_session_string()
        
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=message.from_user.id).first()
            if user:
                user.phone_number = phone
                user.session_string = session_string
                db.commit()
        finally:
            db.close()
        
        active_clients[message.from_user.id] = client
        
        await message.answer(
            "✅ <b>Аккаунт успешно подключен!</b>\n\n"
            "Теперь вы можете создавать рассылки."
        )
        await state.finish()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка при входе: {str(e)}")
        await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'create_broadcast')
async def process_create_broadcast(callback_query: types.CallbackQuery):
    if not is_valid_license(callback_query.from_user.id):
        await bot.answer_callback_query(callback_query.id, "❌ У вас нет активной лицензии!", show_alert=True)
        return
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=callback_query.from_user.id).first()
        if not user or not user.session_string:
            await bot.answer_callback_query(callback_query.id, "❌ Сначала подключите аккаунт!", show_alert=True)
            return
    finally:
        db.close()
    
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "📝 <b>Создание рассылки</b>\n\n"
        "Введите текст сообщения для рассылки:"
    )
    await UserStates.waiting_message.set()

@dp.message_handler(state=UserStates.waiting_message)
async def process_message_text(message: types.Message, state: FSMContext):
    message_text = message.text
    await state.update_data(message_text=message_text)
    
    await message.answer(
        "⏱ <b>Интервал рассылки</b>\n\n"
        "Введите интервал в минутах между отправкой сообщений.\n"
        "Рекомендуется не менее 30 минут для безопасности."
    )
    await UserStates.waiting_interval.set()

@dp.message_handler(state=UserStates.waiting_interval)
async def process_interval(message: types.Message, state: FSMContext):
    try:
        interval = int(message.text)
        if interval < 5:
            await message.answer("❌ Минимальный интервал - 5 минут. Введите снова:")
            return
        
        data = await state.get_data()
        message_text = data.get('message_text')
        
        db = SessionLocal()
        try:
            task = BroadcastTask(
                user_id=message.from_user.id,
                message_text=message_text,
                interval_minutes=interval,
                status='active'
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            task_id = task.id
        finally:
            db.close()
        
        asyncio.create_task(start_broadcast(message.from_user.id, task_id))
        
        await message.answer(
            f"✅ <b>Рассылка создана!</b>\n\n"
            f"📝 Сообщение: <code>{message_text[:100]}</code>\n"
            f"⏱ Интервал: {interval} минут\n"
            f"🔄 Статус: Активна"
        )
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите число в минутах:")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

async def start_broadcast(user_id: int, task_id: int):
    """Start broadcast task"""
    db = SessionLocal()
    try:
        task = db.query(BroadcastTask).filter_by(id=task_id).first()
        user = db.query(User).filter_by(user_id=user_id).first()
    finally:
        db.close()
    
    if not task or not user or not user.session_string:
        return
    
    client = Client(
        f"broadcast_{user_id}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=user.session_string
    )
    
    try:
        await client.start()
        
        dialogs = []
        async for dialog in client.get_dialogs():
            if dialog.chat.type in ['group', 'supergroup']:
                dialogs.append(dialog.chat.id)
        
        db = SessionLocal()
        try:
            db.query(BroadcastTask).filter_by(id=task_id).update({'groups_count': len(dialogs)})
            db.commit()
        finally:
            db.close()
        
        for group_id in dialogs:
            db = SessionLocal()
            try:
                current_task = db.query(BroadcastTask).filter_by(id=task_id).first()
            finally:
                db.close()
            
            if not current_task or current_task.status != 'active':
                break
            
            try:
                await client.send_message(group_id, task.message_text)
                logger.info(f"Message sent to group {group_id}")
                
                await asyncio.sleep(task.interval_minutes * 60)
                
            except FloodWait as e:
                logger.warning(f"FloodWait: {e.value} seconds")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Error: {str(e)}")
                continue
        
        db = SessionLocal()
        try:
            db.query(BroadcastTask).filter_by(id=task_id).update({'status': 'completed'})
            db.commit()
        finally:
            db.close()
        
        await bot.send_message(user_id, f"✅ Рассылка завершена! Отправлено в {len(dialogs)} групп")
        
    except Exception as e:
        logger.error(f"Broadcast error: {str(e)}")
        await bot.send_message(user_id, f"❌ Ошибка: {str(e)}")
    finally:
        await client.stop()

@dp.callback_query_handler(lambda c: c.data == 'stop_broadcasts')
async def process_stop_broadcasts(callback_query: types.CallbackQuery):
    db = SessionLocal()
    try:
        db.query(BroadcastTask).filter_by(user_id=callback_query.from_user.id, status='active').update({'status': 'paused'})
        db.commit()
    finally:
        db.close()
    
    await bot.answer_callback_query(callback_query.id, "✅ Рассылки остановлены", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == 'my_broadcasts')
async def process_my_broadcasts(callback_query: types.CallbackQuery):
    db = SessionLocal()
    try:
        tasks = db.query(BroadcastTask).filter_by(user_id=callback_query.from_user.id).order_by(BroadcastTask.created_at.desc()).limit(10).all()
    finally:
        db.close()
    
    if not tasks:
        await bot.answer_callback_query(callback_query.id, "У вас нет рассылок", show_alert=True)
        return
    
    text = "📊 <b>Ваши рассылки:</b>\n\n"
    for task in tasks:
        status_emoji = "✅" if task.status == 'completed' else "🔄" if task.status == 'active' else "⏸"
        text += f"{status_emoji} ID: {task.id}\n"
        text += f"📝 {task.message_text[:50]}...\n"
        text += f"⏱ {task.interval_minutes} мин\n\n"
    
    await bot.send_message(callback_query.from_user.id, text)

# Admin handlers
@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ У вас нет доступа к админ-панели.")
        return
    
    await message.answer("🔐 <b>Админ-панель</b>", reply_markup=get_admin_keyboard())

@dp.callback_query_handler(lambda c: c.data == 'admin_create_key')
async def process_admin_create_key(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return
    
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "🔑 Введите срок действия в днях:\n"
        "• Число (например 30)\n"
        "• -1 для бессрочного"
    )
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
        await message.answer(
            f"✅ Ключ создан:\n"
            f"🔑 <code>{key}</code>\n"
            f"📅 Срок: {duration_text}"
        )
        await state.finish()
    except ValueError:
        await message.answer("❌ Введите число или -1")

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
        await bot.answer_callback_query(callback_query.id, "Нет пользователей", show_alert=True)
        return
    
    text = "👥 <b>Пользователи:</b>\n\n"
    for user in users:
        status = "🚫" if user.is_blocked else "✅"
        license_status = "✅" if user.license_expiry and user.license_expiry > datetime.utcnow() else "❌"
        text += f"{status} ID: {user.user_id}\n"
        text += f"👤 {user.first_name or 'Нет имени'}"
        if user.username:
            text += f" (@{user.username})"
        text += f"\n🔑 {license_status}\n\n"
    
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await bot.send_message(callback_query.from_user.id, part)
    else:
        await bot.send_message(callback_query.from_user.id, text)

@dp.callback_query_handler(lambda c: c.data == 'admin_stats')
async def process_admin_stats(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return
    
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        active_licenses = db.query(User).filter(User.license_expiry > datetime.utcnow()).count()
        total_keys = db.query(LicenseKey).count()
        used_keys = db.query(LicenseKey).filter_by(is_used=True).count()
        total_broadcasts = db.query(BroadcastTask).count()
    finally:
        db.close()
    
    stats_text = f"""
📊 <b>Статистика:</b>

👥 Пользователей: <b>{total_users}</b>
✅ Активных лицензий: <b>{active_licenses}</b>
🔑 Всего ключей: <b>{total_keys}</b>
📤 Использовано: <b>{used_keys}</b>
📨 Рассылок: <b>{total_broadcasts}</b>
"""
    
    await bot.send_message(callback_query.from_user.id, stats_text)

@dp.callback_query_handler(lambda c: c.data == 'admin_block_user')
async def process_admin_block_user(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        return
    
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.from_user.id, "Введите ID пользователя для блокировки/разблокировки:")
    await dp.current_state(user=callback_query.from_user.id).set_state('admin_block_user')

@dp.message_handler(lambda message: message.from_user.id == ADMIN_ID and dp.current_state(user=message.from_user.id).get_state() == 'admin_block_user')
async def process_admin_block_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            if not user:
                await message.answer("❌ Пользователь не найден.")
                await state.finish()
                return
            
            user.is_blocked = not user.is_blocked
            db.commit()
            status = "заблокирован" if user.is_blocked else "разблокирован"
        finally:
            db.close()
        
        await message.answer(f"✅ Пользователь {user_id} {status}.")
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите корректный ID.")

# Error handler
@dp.errors_handler()
async def errors_handler(update, error):
    logger.error(f"Update: {update} \nError: {error}")
    return True

# Startup
async def on_startup(dp):
    logger.info("Bot started!")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен и готов к работе!")
    except:
        pass

# Main entry point
if __name__ == '__main__':
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
