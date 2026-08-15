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
from aiogram.types import ParseMode, ReplyKeyboardMarkup, KeyboardButton
from pyrogram import Client
from pyrogram.errors import FloodWait, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded

# Database imports
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, Boolean, Text, text
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

# Models
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)
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
    used_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BroadcastTask(Base):
    __tablename__ = 'broadcast_tasks'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    message_text = Column(Text, nullable=False)
    interval_minutes = Column(Integer, default=30)
    status = Column(String, default='active')
    groups_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(engine)
logger.info("✅ Tables ready")

# States
class UserStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_message = State()
    waiting_interval = State()
    waiting_license = State()
    admin_create_key = State()
    admin_block_user = State()

# Initialize bot
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# Хранилище для клиентов и данных
active_clients = {}
phone_code_hashes = {}  # Храним хэши кодов

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
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    
    if is_valid_license(user_id):
        keyboard.add(
            KeyboardButton("📱 Подключить аккаунт"),
            KeyboardButton("📨 Создать рассылку")
        )
        keyboard.add(
            KeyboardButton("⏹ Остановить рассылки"),
            KeyboardButton("📊 Мои рассылки")
        )
    else:
        keyboard.add(KeyboardButton("🔑 Активировать лицензию"))
    
    return keyboard

def get_admin_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(
        KeyboardButton("🔑 Создать ключ"),
        KeyboardButton("👥 Пользователи")
    )
    keyboard.add(
        KeyboardButton("📊 Статистика"),
        KeyboardButton("🚫 Заблокировать")
    )
    keyboard.add(KeyboardButton("⬅️ Главное меню"))
    return keyboard

# User handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    logger.info(f"Start command from user {message.from_user.id}")
    create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"Я бот для рассылки сообщений в группы.\n\n"
        f"Используйте кнопки ниже:",
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

# Кнопки
@dp.message_handler(lambda message: message.text == "🔑 Активировать лицензию")
async def activate_license(message: types.Message):
    await message.answer("🔑 Введите ваш лицензионный ключ:")
    await UserStates.waiting_license.set()

@dp.message_handler(lambda message: message.text == "📱 Подключить аккаунт")
async def connect_account(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ У вас нет активной лицензии!")
        return
    
    await message.answer(
        "📱 <b>Подключение аккаунта</b>\n\n"
        "Введите номер телефона в формате:\n"
        "<code>+79123456789</code>"
    )
    await UserStates.waiting_phone.set()

@dp.message_handler(lambda message: message.text == "📨 Создать рассылку")
async def create_broadcast(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ У вас нет активной лицензии!")
        return
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if not user or not user.session_string:
            await message.answer("❌ Сначала подключите аккаунт!")
            return
    finally:
        db.close()
    
    await message.answer("📝 <b>Создание рассылки</b>\n\nВведите текст сообщения:")
    await UserStates.waiting_message.set()

@dp.message_handler(lambda message: message.text == "⏹ Остановить рассылки")
async def stop_broadcasts(message: types.Message):
    db = SessionLocal()
    try:
        db.query(BroadcastTask).filter_by(
            user_id=message.from_user.id,
            status='active'
        ).update({'status': 'paused'})
        db.commit()
    finally:
        db.close()
    
    await message.answer("✅ Все рассылки остановлены.")

@dp.message_handler(lambda message: message.text == "📊 Мои рассылки")
async def my_broadcasts(message: types.Message):
    db = SessionLocal()
    try:
        tasks = db.query(BroadcastTask).filter_by(
            user_id=message.from_user.id
        ).order_by(BroadcastTask.created_at.desc()).limit(10).all()
    finally:
        db.close()
    
    if not tasks:
        await message.answer("У вас нет рассылок.")
        return
    
    text = "📊 <b>Ваши рассылки:</b>\n\n"
    for task in tasks:
        status_emoji = "✅" if task.status == 'completed' else "🔄" if task.status == 'active' else "⏸"
        text += f"{status_emoji} ID: {task.id}\n"
        text += f"📝 {task.message_text[:50]}...\n"
        text += f"⏱ Интервал: {task.interval_minutes} мин\n\n"
    
    await message.answer(text)

# Админ кнопки
@dp.message_handler(lambda message: message.text == "🔑 Создать ключ")
async def admin_create_key(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    
    await message.answer(
        "🔑 <b>Создание ключа</b>\n\n"
        "Введите срок действия:\n"
        "• <code>30</code> - на месяц\n"
        "• <code>365</code> - на год\n"
        "• <code>-1</code> - навсегда"
    )
    await UserStates.admin_create_key.set()

@dp.message_handler(lambda message: message.text == "👥 Пользователи")
async def admin_users(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    
    db = SessionLocal()
    try:
        users = db.query(User).all()
        
        if not users:
            await message.answer("Нет пользователей.")
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
        
        await message.answer(text)
    finally:
        db.close()

@dp.message_handler(lambda message: message.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_keys = db.query(LicenseKey).count()
        used_keys = db.query(LicenseKey).filter_by(is_used=True).count()
        
        await message.answer(
            f"📊 <b>Статистика:</b>\n\n"
            f"👥 Пользователей: <b>{total_users}</b>\n"
            f"🔑 Ключей: <b>{total_keys}</b>\n"
            f"📤 Использовано: <b>{used_keys}</b>"
        )
    finally:
        db.close()

@dp.message_handler(lambda message: message.text == "🚫 Заблокировать")
async def admin_block_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    
    await message.answer("Введите ID пользователя для блокировки/разблокировки:")
    await UserStates.admin_block_user.set()

@dp.message_handler(lambda message: message.text == "⬅️ Главное меню")
async def back_to_main(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Вы в главном меню.", reply_markup=get_admin_keyboard())
    else:
        await message.answer("Вы в главном меню.", reply_markup=get_main_keyboard(message.from_user.id))

# Обработка состояний
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
            f"📅 Действует до: <b>{expiry.strftime('%d.%m.%Y')}</b>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
    finally:
        db.close()

@dp.message_handler(state=UserStates.admin_create_key)
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
            f"✅ <b>Ключ создан!</b>\n\n"
            f"🔑 Ключ: <code>{key}</code>\n"
            f"📅 Срок: {duration_text}"
        )
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите число.")

@dp.message_handler(state=UserStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    logger.info(f"Phone entered: {phone}")
    
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с '+'. Попробуйте снова.")
        return
    
    # Создаем клиента и ОСТАВЛЯЕМ его подключенным
    client = Client(
        f"session_{message.from_user.id}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        
        # Сохраняем ВСЕ данные
        await state.update_data(phone=phone)
        phone_code_hashes[message.from_user.id] = sent_code.phone_code_hash
        
        # НЕ отключаем клиента!
        active_clients[message.from_user.id] = client
        
        logger.info(f"Code sent to {phone}, hash: {sent_code.phone_code_hash[:20]}...")
        
        await message.answer(
            "📨 <b>Код отправлен!</b>\n\n"
            "Введите код из SMS:"
        )
        await UserStates.waiting_code.set()
        
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()
        try:
            await client.disconnect()
        except:
            pass

@dp.message_handler(state=UserStates.waiting_code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    
    # Получаем сохраненного клиента
    client = active_clients.get(message.from_user.id)
    
    if not client:
        await message.answer("❌ Сессия истекла. Нажмите '📱 Подключить аккаунт' снова.")
        await state.finish()
        return
    
    phone_code_hash = phone_code_hashes.get(message.from_user.id)
    
    if not phone_code_hash:
        await message.answer("❌ Хэш кода не найден. Попробуйте снова.")
        await state.finish()
        return
    
    try:
        logger.info(f"Attempting to sign in with code: {code}")
        
        try:
            await client.sign_in(phone, phone_code_hash, code)
        except SessionPasswordNeeded:
            await message.answer(
                "🔐 <b>Требуется 2FA пароль</b>\n\n"
                "Введите ваш пароль:"
            )
            await UserStates.waiting_password.set()
            return
        except PhoneCodeExpired:
            await message.answer(
                "❌ <b>Код истек!</b>\n\n"
                "Нажмите '📱 Подключить аккаунт' снова."
            )
            await state.finish()
            return
        except PhoneCodeInvalid:
            await message.answer(
                "❌ <b>Неверный код!</b>\n\n"
                "Проверьте код и введите снова."
            )
            return
        
        # Успешный вход
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
        
        await message.answer(
            "✅ <b>Аккаунт успешно подключен!</b>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
        
    except Exception as e:
        logger.error(f"Error signing in: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_password)
async def process_2fa_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    client = active_clients.get(message.from_user.id)
    
    if not client:
        await message.answer("❌ Сессия истекла.")
        await state.finish()
        return
    
    try:
        await client.check_password(password)
        
        session_string = await client.export_session_string()
        data = await state.get_data()
        phone = data.get('phone')
        
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=message.from_user.id).first()
            if user:
                user.phone_number = phone
                user.session_string = session_string
                db.commit()
        finally:
            db.close()
        
        await message.answer(
            "✅ <b>Аккаунт успешно подключен!</b>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
        
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_message)
async def process_message_text(message: types.Message, state: FSMContext):
    await state.update_data(message_text=message.text)
    await message.answer("⏱ Введите интервал в минутах (мин. 5):")
    await UserStates.waiting_interval.set()

@dp.message_handler(state=UserStates.waiting_interval)
async def process_interval(message: types.Message, state: FSMContext):
    try:
        interval = int(message.text)
        if interval < 5:
            await message.answer("❌ Минимум 5 минут.")
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
        finally:
            db.close()
        
        await message.answer(
            f"✅ <b>Рассылка создана!</b>\n\n"
            f"📝 {message_text[:100]}\n"
            f"⏱ Интервал: {interval} мин",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите число.")

@dp.message_handler(state=UserStates.admin_block_user)
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
            await message.answer(f"✅ Пользователь {user_id} {status}.")
            await state.finish()
        finally:
            db.close()
    except ValueError:
        await message.answer("❌ Введите ID.")

# Startup
async def on_startup(dp):
    logger.info("✅ Bot started successfully!")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен!\nИспользуйте /admin")
    except:
        pass

# Error handler
@dp.errors_handler()
async def errors_handler(update, error):
    logger.error(f"Error: {error}")
    return True

# Main
if __name__ == '__main__':
    logger.info("🚀 Starting bot...")
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
