import os
import asyncio
import logging
import random
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
from pyrogram.errors import FloodWait, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded, PasswordHashInvalid
from pyrogram.enums import ChatType

# Database imports
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, Boolean, Text, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import inspect

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

# Database URL
def get_database_url():
    public_url = os.getenv('DATABASE_PUBLIC_URL', '')
    if public_url:
        if public_url.startswith('postgres://'):
            public_url = public_url.replace('postgres://', 'postgresql://', 1)
        return public_url.strip()
    
    db_url = os.getenv('DATABASE_URL', '')
    if db_url:
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        return db_url.strip()
    
    return 'sqlite:///bot.db'

DATABASE_URL = get_database_url()

# Create engine
try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("✅ Database connected")
except Exception as e:
    logger.error(f"Database error: {e}")
    DATABASE_URL = 'sqlite:///bot.db'
    engine = create_engine(DATABASE_URL)

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
    created_at = Column(DateTime, default=datetime.utcnow)
    max_accounts = Column(Integer, default=3)  # Лимит аккаунтов

class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    phone_number = Column(String, nullable=True)
    session_string = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

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
    account_ids = Column(Text, nullable=True)  # JSON список ID аккаунтов
    messages = Column(Text, nullable=False)  # JSON список сообщений
    interval_minutes = Column(Integer, default=30)
    safe_mode = Column(Boolean, default=False)
    status = Column(String, default='active')
    groups_count = Column(Integer, default=0)
    current_cycle = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Пересоздаем таблицы
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
logger.info("✅ Tables ready")

# States
class UserStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_license = State()
    admin_create_key = State()
    admin_block_user = State()
    admin_set_accounts = State()
    waiting_message = State()
    waiting_interval = State()
    waiting_more_messages = State()
    selecting_accounts = State()

# Initialize bot
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

active_clients = {}
phone_code_hashes = {}

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
            if user_id == ADMIN_ID:
                user.max_accounts = 999999  # Бесконечно для админа
            else:
                user.max_accounts = 3  # Стандарт 3
            db.add(user)
            db.commit()
        return user
    finally:
        db.close()

def get_user_accounts(user_id: int):
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(user_id=user_id).all()
    finally:
        db.close()

def get_back_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("⬅️ Назад"))
    return keyboard

# Keyboards
def get_main_keyboard(user_id: int):
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    
    if user_id == ADMIN_ID:
        keyboard.add(KeyboardButton("🔐 Админ-панель"))
    
    if is_valid_license(user_id):
        keyboard.add(
            KeyboardButton("📱 Подключить аккаунт"),
            KeyboardButton("📨 Создать рассылку")
        )
        keyboard.add(
            KeyboardButton("⏹ Остановить рассылки"),
            KeyboardButton("📊 Мои рассылки")
        )
        keyboard.add(KeyboardButton("👤 Профиль"))
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
    keyboard.add(KeyboardButton("⚙️ Управление аккаунтами"))
    keyboard.add(KeyboardButton("⬅️ Назад"))
    return keyboard

# Функция получения групп
async def get_user_groups(client: Client):
    groups = []
    try:
        dialogs = []
        async for dialog in client.get_dialogs(limit=None):
            dialogs.append(dialog)
        
        for dialog in dialogs:
            chat = dialog.chat
            if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                groups.append({
                    'id': chat.id,
                    'title': chat.title or 'Без названия'
                })
    except Exception as e:
        logger.error(f"Error getting groups: {e}")
    return groups

# Функция рассылки
async def start_broadcast(user_id: int, task_id: int):
    db = SessionLocal()
    try:
        task = db.query(BroadcastTask).filter_by(id=task_id).first()
        user = db.query(User).filter_by(user_id=user_id).first()
    finally:
        db.close()
    
    if not task or not user:
        return
    
    import json
    account_ids = json.loads(task.account_ids or '[]')
    messages = json.loads(task.messages or '[]')
    
    if not account_ids or not messages:
        return
    
    clients = []
    db = SessionLocal()
    try:
        for acc_id in account_ids:
            account = db.query(Account).filter_by(id=acc_id).first()
            if account and account.session_string:
                client = Client(
                    f"broadcast_{acc_id}_{task_id}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=account.session_string
                )
                clients.append(client)
    finally:
        db.close()
    
    if not clients:
        return
    
    status_message = await bot.send_message(
        user_id,
        f"🚀 <b>Рассылка запущена!</b>\n\n"
        f"👥 Аккаунтов: <b>{len(clients)}</b>\n"
        f"📝 Сообщений: <b>{len(messages)}</b>\n"
        f"⏱ Интервал: {task.interval_minutes} мин\n"
        f"🛡 Безопасный режим: {'✅' if task.safe_mode else '❌'}\n\n"
        f"⏳ Начинаю..."
    )
    
    cycle = 0
    total_sent = 0
    
    try:
        for client in clients:
            await client.start()
        
        while True:
            db = SessionLocal()
            try:
                current_task = db.query(BroadcastTask).filter_by(id=task_id).first()
            finally:
                db.close()
            
            if not current_task or current_task.status != 'active':
                break
            
            cycle += 1
            
            for client in clients:
                groups = await get_user_groups(client)
                
                if not groups:
                    continue
                
                for group in groups:
                    # Выбираем случайное сообщение
                    message_text = random.choice(messages)
                    
                    try:
                        await client.send_message(group['id'], message_text)
                        total_sent += 1
                        
                        db = SessionLocal()
                        try:
                            db.query(BroadcastTask).filter_by(id=task_id).update({
                                'current_cycle': cycle,
                                'sent_count': total_sent,
                                'groups_count': len(groups)
                            })
                            db.commit()
                        finally:
                            db.close()
                        
                        try:
                            await status_message.edit_text(
                                f"🔄 <b>Цикл {cycle}</b>\n\n"
                                f"📨 Отправлено: <b>{total_sent}</b>\n"
                                f"📝 Сообщение: {message_text[:30]}...\n\n"
                                f"⏳ Отправляю..."
                            )
                        except:
                            pass
                        
                        await asyncio.sleep(1)
                        
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        logger.error(f"Error: {e}")
                        continue
            
            # Случайный интервал для безопасного режима
            if task.safe_mode:
                base = task.interval_minutes * 60
                variation = int(base * 0.2)  # ±20%
                interval_seconds = max(1800, base + random.randint(-variation, variation))  # Минимум 30 мин
            else:
                interval_seconds = task.interval_minutes * 60
            
            try:
                await status_message.edit_text(
                    f"✅ <b>Цикл {cycle} завершен!</b>\n\n"
                    f"📨 Всего: <b>{total_sent}</b>\n\n"
                    f"⏱ Следующий цикл через {interval_seconds // 60} мин..."
                )
            except:
                pass
            
            await asyncio.sleep(interval_seconds)
        
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        await bot.send_message(user_id, f"❌ Ошибка: {str(e)}")
    finally:
        for client in clients:
            try:
                await client.stop()
            except:
                pass

# User handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"Используйте кнопки ниже:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

# Кнопка Назад
@dp.message_handler(lambda message: message.text == "⬅️ Назад")
async def go_back(message: types.Message):
    await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))

# Кнопка Профиль
@dp.message_handler(lambda message: message.text == "👤 Профиль")
async def show_profile(message: types.Message):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if not user:
            await message.answer("❌ Пользователь не найден.")
            return
        
        accounts = db.query(Account).filter_by(user_id=message.from_user.id).all()
        
        if user.license_expiry and user.license_expiry > datetime.utcnow():
            days_left = (user.license_expiry - datetime.utcnow()).days
            license_status = f"✅ <b>Активна</b>\n📅 До: <b>{user.license_expiry.strftime('%d.%m.%Y')}</b>\n⏳ Осталось: <b>{days_left} дней</b>"
        elif user.user_id == ADMIN_ID:
            license_status = "✅ <b>Бессрочная (Админ)</b>"
        else:
            license_status = "❌ <b>Не активирована</b>"
        
        total_broadcasts = db.query(BroadcastTask).filter_by(user_id=message.from_user.id).count()
        
        profile_text = f"""
👤 <b>Ваш профиль</b>

🆔 ID: <code>{user.user_id}</code>
👤 Имя: <b>{user.first_name or 'Не указано'}</b>

━━━━━━━━━━━━━━

💳 <b>Подписка:</b>
{license_status}

━━━━━━━━━━━━━━

📱 <b>Аккаунты:</b>
Подключено: <b>{len(accounts)}</b>
Лимит: <b>{'∞' if user.max_accounts >= 999999 else user.max_accounts}</b>

"""
        for acc in accounts:
            profile_text += f"• <code>{acc.phone_number or 'Неизвестно'}</code>\n"
        
        profile_text += f"""
━━━━━━━━━━━━━━

📊 <b>Рассылок:</b> <b>{total_broadcasts}</b>

━━━━━━━━━━━━━━

📅 <b>Регистрация:</b>
{user.created_at.strftime('%d.%m.%Y')}
"""
        
        await message.answer(profile_text, reply_markup=get_main_keyboard(message.from_user.id))
    finally:
        db.close()

# Кнопки
@dp.message_handler(lambda message: message.text == "🔑 Активировать лицензию")
async def activate_license(message: types.Message):
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("⬅️ Назад"))
    await message.answer("🔑 Введите лицензионный ключ:", reply_markup=keyboard)
    await UserStates.waiting_license.set()

@dp.message_handler(lambda message: message.text == "📱 Подключить аккаунт")
async def connect_account(message: types.Message):
    if message.from_user.id != ADMIN_ID and not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        accounts_count = db.query(Account).filter_by(user_id=message.from_user.id).count()
        
        if accounts_count >= user.max_accounts:
            await message.answer(f"❌ Достигнут лимит аккаунтов ({user.max_accounts})!")
            return
    finally:
        db.close()
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("⬅️ Назад"))
    await message.answer("📱 Введите номер телефона:\n<code>+380123456789</code>", reply_markup=keyboard)
    await UserStates.waiting_phone.set()

# Админ-панель
@dp.message_handler(lambda message: message.text == "🔐 Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔐 Админ-панель:", reply_markup=get_admin_keyboard())

@dp.message_handler(lambda message: message.text == "🔑 Создать ключ")
async def admin_create_key(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("⬅️ Назад"))
    await message.answer("🔑 Введите срок (дней, -1 для бессрочного):", reply_markup=keyboard)
    await UserStates.admin_create_key.set()

@dp.message_handler(lambda message: message.text == "⚙️ Управление аккаунтами")
async def admin_manage_accounts(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("➕ Выдать аккаунт"), KeyboardButton("➖ Забрать аккаунт"))
    keyboard.add(KeyboardButton("♾ Выдать бесконечно"), KeyboardButton("❌ Забрать все"))
    keyboard.add(KeyboardButton("⬅️ Назад"))
    
    await message.answer(
        "⚙️ <b>Управление аккаунтами</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text == "➕ Выдать аккаунт")
async def admin_give_account(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Введите ID пользователя и количество аккаунтов через пробел:\n<code>123456789 5</code>")
    await UserStates.admin_set_accounts.set()

@dp.message_handler(lambda message: message.text == "♾ Выдать бесконечно")
async def admin_give_unlimited(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Введите ID пользователя:")
    await dp.current_state(user=message.from_user.id).set_state('admin_give_unlimited')

@dp.message_handler(lambda message: message.text == "❌ Забрать все")
async def admin_remove_all(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Введите ID пользователя:")
    await dp.current_state(user=message.from_user.id).set_state('admin_remove_all')

@dp.message_handler(state=UserStates.admin_set_accounts)
async def process_admin_set_accounts(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        parts = message.text.split()
        user_id = int(parts[0])
        count = int(parts[1])
        
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            if user:
                user.max_accounts = count
                db.commit()
                await message.answer(f"✅ Пользователю {user_id} установлен лимит {count} аккаунтов.")
            else:
                await message.answer("❌ Пользователь не найден.")
        finally:
            db.close()
        
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
    except:
        await message.answer("❌ Формат: <code>ID количество</code>")

@dp.message_handler(lambda message: message.from_user.id == ADMIN_ID and dp.current_state(user=message.from_user.id).get_state() == 'admin_give_unlimited')
async def process_give_unlimited(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            if user:
                user.max_accounts = 999999
                db.commit()
                await message.answer(f"✅ Пользователю {user_id} выдано бесконечно аккаунтов.")
            else:
                await message.answer("❌ Пользователь не найден.")
        finally:
            db.close()
    except:
        await message.answer("❌ Введите ID.")
    
    await state.finish()
    await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())

@dp.message_handler(lambda message: message.from_user.id == ADMIN_ID and dp.current_state(user=message.from_user.id).get_state() == 'admin_remove_all')
async def process_remove_all(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            if user:
                user.max_accounts = 0
                db.commit()
                await message.answer(f"✅ У пользователя {user_id} забраны все аккаунты.")
            else:
                await message.answer("❌ Пользователь не найден.")
        finally:
            db.close()
    except:
        await message.answer("❌ Введите ID.")
    
    await state.finish()
    await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())

# Создание рассылки
@dp.message_handler(lambda message: message.text == "📨 Создать рассылку")
async def create_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID and not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter_by(user_id=message.from_user.id).all()
        if not accounts:
            await message.answer("❌ Сначала подключите аккаунт!")
            return
    finally:
        db.close()
    
    # Показываем список аккаунтов для выбора
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    for acc in accounts:
        keyboard.add(KeyboardButton(f"Аккаунт {acc.id}: {acc.phone_number}"))
    keyboard.add(KeyboardButton("✅ Все аккаунты"))
    keyboard.add(KeyboardButton("⬅️ Назад"))
    
    await message.answer("📱 Выберите аккаунты для рассылки:", reply_markup=keyboard)
    await UserStates.selecting_accounts.set()

@dp.message_handler(state=UserStates.selecting_accounts)
async def process_account_selection(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter_by(user_id=message.from_user.id).all()
    finally:
        db.close()
    
    selected_ids = []
    
    if message.text == "✅ Все аккаунты":
        selected_ids = [acc.id for acc in accounts]
    else:
        # Парсим выбранный аккаунт
        for acc in accounts:
            if message.text.startswith(f"Аккаунт {acc.id}:"):
                selected_ids = [acc.id]
                break
    
    if not selected_ids:
        await message.answer("❌ Выберите аккаунт из списка.")
        return
    
    await state.update_data(account_ids=selected_ids)
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🛡 Безопасный режим"), KeyboardButton("⚡ Обычный режим"))
    keyboard.add(KeyboardButton("⬅️ Назад"))
    
    await message.answer("Выберите режим рассылки:", reply_markup=keyboard)
    await UserStates.waiting_message.set()

@dp.message_handler(state=UserStates.waiting_message)
async def process_message_text(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    safe_mode = message.text == "🛡 Безопасный режим"
    await state.update_data(safe_mode=safe_mode)
    
    await message.answer("📝 Введите текст сообщения (или несколько текстов через разделитель <code>|||</code> для безопасного режима):")
    await UserStates.waiting_interval.set()

@dp.message_handler(state=UserStates.waiting_interval)
async def process_interval(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    messages_text = message.text
    messages = [m.strip() for m in messages_text.split('|||') if m.strip()]
    
    if not messages:
        await message.answer("❌ Введите хотя бы одно сообщение.")
        return
    
    data = await state.get_data()
    safe_mode = data.get('safe_mode', False)
    
    if safe_mode and len(messages) < 3:
        await message.answer("❌ Для безопасного режима нужно минимум 3 текста (разделитель <code>|||</code>).")
        return
    
    await state.update_data(messages=messages)
    
    await message.answer("⏱ Введите базовый интервал в минутах (мин. 30):")
    await UserStates.waiting_more_messages.set()

@dp.message_handler(state=UserStates.waiting_more_messages)
async def process_final_interval(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        interval = int(message.text)
        if interval < 30:
            await message.answer("❌ Минимум 30 минут.")
            return
        
        data = await state.get_data()
        account_ids = data.get('account_ids', [])
        messages = data.get('messages', [])
        safe_mode = data.get('safe_mode', False)
        
        import json
        db = SessionLocal()
        try:
            task = BroadcastTask(
                user_id=message.from_user.id,
                account_ids=json.dumps(account_ids),
                messages=json.dumps(messages),
                interval_minutes=interval,
                safe_mode=safe_mode,
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
            f"✅ <b>Рассылка запущена!</b>\n\n"
            f"📱 Аккаунтов: {len(account_ids)}\n"
            f"📝 Текстов: {len(messages)}\n"
            f"🛡 Режим: {'Безопасный' if safe_mode else 'Обычный'}\n"
            f"⏱ Интервал: {interval} мин",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите число.")

# Обработка подключения аккаунта
@dp.message_handler(state=UserStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    phone = message.text.strip()
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с '+'")
        return
    
    client = Client(
        f"session_{message.from_user.id}_{len(phone_code_hashes)}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        await state.update_data(phone=phone)
        phone_code_hashes[message.from_user.id] = sent_code.phone_code_hash
        active_clients[message.from_user.id] = client
        await message.answer("📨 Код отправлен! Введите код из SMS:")
        await UserStates.waiting_code.set()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_code)
async def process_code(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    client = active_clients.get(message.from_user.id)
    phone_code_hash = phone_code_hashes.get(message.from_user.id)
    
    if not client or not phone_code_hash:
        await message.answer("❌ Сессия истекла.")
        await state.finish()
        return
    
    try:
        try:
            await client.sign_in(phone, phone_code_hash, code)
        except SessionPasswordNeeded:
            await message.answer("🔐 Введите пароль 2FA:")
            await UserStates.waiting_password.set()
            return
        
        session_string = await client.export_session_string()
        
        db = SessionLocal()
        try:
            account = Account(
                user_id=message.from_user.id,
                phone_number=phone,
                session_string=session_string
            )
            db.add(account)
            db.commit()
        finally:
            db.close()
        
        await message.answer("✅ <b>Аккаунт подключен!</b>", reply_markup=get_main_keyboard(message.from_user.id))
        await state.finish()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    client = active_clients.get(message.from_user.id)
    
    if not client:
        await message.answer("❌ Сессия истекла.")
        await state.finish()
        return
    
    try:
        await client.check_password(password)
        session_string = await client.export_session_string()
        
        db = SessionLocal()
        try:
            account = Account(
                user_id=message.from_user.id,
                phone_number=phone,
                session_string=session_string
            )
            db.add(account)
            db.commit()
        finally:
            db.close()
        
        await message.answer("✅ <b>Аккаунт подключен!</b>", reply_markup=get_main_keyboard(message.from_user.id))
        await state.finish()
    except PasswordHashInvalid:
        await message.answer("❌ Неверный пароль. Попробуйте снова:")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

# Обработка лицензии
@dp.message_handler(state=UserStates.waiting_license)
async def process_license_key(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    license_key = message.text.strip()
    db = SessionLocal()
    try:
        license_obj = db.query(LicenseKey).filter_by(key=license_key).first()
        
        if not license_obj:
            await message.answer("❌ Неверный ключ.")
            return
        if license_obj.is_used:
            await message.answer("❌ Ключ уже использован.")
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
        
        await message.answer(
            f"✅ <b>Лицензия активирована!</b>\n📅 До: <b>{expiry.strftime('%d.%m.%Y')}</b>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
    finally:
        db.close()

@dp.message_handler(state=UserStates.admin_create_key)
async def process_admin_key_duration(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
        return
    
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
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
    except ValueError:
        await message.answer("❌ Введите число.")

# Остановка рассылок
@dp.message_handler(lambda message: message.text == "⏹ Остановить рассылки")
async def stop_broadcasts(message: types.Message):
    db = SessionLocal()
    try:
        db.query(BroadcastTask).filter_by(user_id=message.from_user.id, status='active').update({'status': 'paused'})
        db.commit()
    finally:
        db.close()
    await message.answer("✅ Рассылки остановлены.", reply_markup=get_main_keyboard(message.from_user.id))

# Мои рассылки
@dp.message_handler(lambda message: message.text == "📊 Мои рассылки")
async def my_broadcasts(message: types.Message):
    db = SessionLocal()
    try:
        tasks = db.query(BroadcastTask).filter_by(user_id=message.from_user.id).order_by(BroadcastTask.created_at.desc()).limit(10).all()
    finally:
        db.close()
    
    if not tasks:
        await message.answer("У вас нет рассылок.")
        return
    
    text = "📊 <b>Ваши рассылки:</b>\n\n"
    for task in tasks:
        status_emoji = "✅" if task.status == 'completed' else "🔄" if task.status == 'active' else "⏸"
        text += f"{status_emoji} ID: {task.id}\n"
        text += f"🛡 {'Безопасный' if task.safe_mode else 'Обычный'}\n"
        text += f"⏱ {task.interval_minutes} мин\n"
        text += f"👥 Групп: {task.groups_count}\n"
        text += f"🔄 Циклов: {task.current_cycle}\n"
        text += f"📨 Отправлено: {task.sent_count}\n\n"
    
    await message.answer(text)

# Startup
async def on_startup(dp):
    logger.info("✅ Bot started!")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен!")
    except:
        pass

@dp.errors_handler()
async def errors_handler(update, error):
    logger.error(f"Error: {error}")
    return True

if __name__ == '__main__':
    logger.info("🚀 Starting bot...")
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
