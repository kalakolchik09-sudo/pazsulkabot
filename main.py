import os
import asyncio
import logging
import random
import json
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
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=40)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("✅ Database connected")
except Exception as e:
    logger.error(f"Database error: {e}")
    DATABASE_URL = 'sqlite:///bot.db'
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
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
    max_accounts = Column(Integer, default=3)

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
    account_ids = Column(Text, nullable=True)
    messages = Column(Text, nullable=False)
    interval_minutes = Column(Integer, default=30)
    safe_mode = Column(Boolean, default=False)
    status = Column(String, default='active')
    groups_count = Column(Integer, default=0)
    current_cycle = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(engine)
logger.info("✅ Tables ready")

# States
class UserStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_license = State()
    admin_create_key = State()
    admin_set_accounts = State()
    admin_give_unlimited = State()
    admin_remove_all = State()
    admin_broadcast = State()
    waiting_message = State()
    waiting_interval = State()
    selecting_accounts = State()
    waiting_more_messages = State()

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
    if user_id == ADMIN_ID:
        return True
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
                user.max_accounts = 999999
            else:
                user.max_accounts = 3
            db.add(user)
            db.commit()
        return user
    finally:
        db.close()

def get_back_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard

# Keyboards
def get_main_keyboard(user_id: int):
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    if user_id == ADMIN_ID:
        keyboard.insert(KeyboardButton("🔐 Админ-панель"))
    
    if is_valid_license(user_id):
        keyboard.add(
            KeyboardButton("📱 Аккаунты"),
            KeyboardButton("📨 Рассылка")
        )
        keyboard.add(
            KeyboardButton("👤 Профиль"),
            KeyboardButton("📊 Статистика")
        )
    else:
        keyboard.add(KeyboardButton("🔑 Активировать лицензию"))
        keyboard.add(KeyboardButton("👤 Профиль"))
    
    return keyboard

def get_accounts_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("➕ Добавить аккаунт"),
        KeyboardButton("📋 Мои аккаунты")
    )
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard

def get_admin_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("🔑 Создать ключ"),
        KeyboardButton("👥 Пользователи")
    )
    keyboard.add(
        KeyboardButton("⚙️ Лимиты аккаунтов"),
        KeyboardButton("📢 Рассылка всем")
    )
    keyboard.add(KeyboardButton("📊 Статистика"), KeyboardButton("🚫 Блокировка"))
    keyboard.add(KeyboardButton("🔙 Назад"))
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
    finally:
        db.close()
    
    if not task:
        return
    
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
                    f"b_{acc_id}_{task_id}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=account.session_string
                )
                clients.append(client)
    finally:
        db.close()
    
    if not clients:
        return
    
    status_msg = await bot.send_message(user_id, "🚀 Запускаю рассылку...")
    
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
                
                for group in groups:
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
                            await status_msg.edit_text(
                                f"🔄 <b>Цикл {cycle}</b>\n"
                                f"📨 Отправлено: <b>{total_sent}</b>\n"
                                f"⏳ Продолжаю..."
                            )
                        except:
                            pass
                        
                        await asyncio.sleep(1)
                        
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except:
                        continue
            
            if task.safe_mode:
                base = task.interval_minutes * 60
                variation = int(base * 0.2)
                interval_seconds = base + random.randint(-variation, variation)
                # Ограничиваем от 30 до 120 минут
                interval_seconds = max(1800, min(7200, interval_seconds))
            else:
                interval_seconds = task.interval_minutes * 60
            
            try:
                await status_msg.edit_text(
                    f"✅ <b>Цикл {cycle} завершен</b>\n"
                    f"📨 Всего: <b>{total_sent}</b>\n"
                    f"⏱ Следующий через ~{interval_seconds // 60} мин"
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
        f"Я бот для рассылки сообщений в группы.\n"
        f"Выберите действие:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message_handler(lambda message: message.text == "🔙 Назад")
async def go_back(message: types.Message):
    await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))

# Профиль
@dp.message_handler(lambda message: message.text == "👤 Профиль")
async def show_profile(message: types.Message):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if not user:
            await message.answer("❌ Профиль не найден.")
            return
        
        accounts = db.query(Account).filter_by(user_id=message.from_user.id).all()
        total_broadcasts = db.query(BroadcastTask).filter_by(user_id=message.from_user.id).count()
        active_broadcasts = db.query(BroadcastTask).filter_by(user_id=message.from_user.id, status='active').count()
        
        if user.user_id == ADMIN_ID:
            license_status = "👑 <b>Администратор</b>\n♾ Бессрочная"
        elif user.license_expiry and user.license_expiry > datetime.utcnow():
            days_left = (user.license_expiry - datetime.utcnow()).days
            license_status = f"✅ <b>Активна</b>\n📅 До: <b>{user.license_expiry.strftime('%d.%m.%Y')}</b>\n⏳ Осталось: <b>{days_left} дн.</b>"
        else:
            license_status = "❌ <b>Не активирована</b>"
        
        profile_text = f"""
╭━━━━━━━━━━━━━━━╮
┃   👤 <b>ПРОФИЛЬ</b>   ┃
╰━━━━━━━━━━━━━━━╯

🆔 <b>ID:</b> <code>{user.user_id}</code>
👤 <b>Имя:</b> {user.first_name or '—'}
{f"🔗 <b>Username:</b> @{user.username}" if user.username else ""}

━━━━━━━━━━━━━━━

💳 <b>Подписка</b>
{license_status}

━━━━━━━━━━━━━━━

📱 <b>Аккаунты</b>
Подключено: <b>{len(accounts)}</b>
Лимит: <b>{'♾' if user.max_accounts >= 999999 else user.max_accounts}</b>

"""
        if accounts:
            profile_text += "<b>Список:</b>\n"
            for i, acc in enumerate(accounts, 1):
                profile_text += f"  {i}. <code>{acc.phone_number or '—'}</code>\n"
        
        profile_text += f"""
━━━━━━━━━━━━━━━

📊 <b>Статистика</b>
📨 Рассылок: <b>{total_broadcasts}</b>
🔄 Активных: <b>{active_broadcasts}</b>

━━━━━━━━━━━━━━━

📅 <b>Регистрация:</b> {user.created_at.strftime('%d.%m.%Y')}
"""
        
        await message.answer(profile_text, reply_markup=get_main_keyboard(message.from_user.id))
    finally:
        db.close()

# Аккаунты
@dp.message_handler(lambda message: message.text == "📱 Аккаунты")
async def accounts_menu(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    await message.answer("📱 <b>Управление аккаунтами</b>", reply_markup=get_accounts_keyboard())

@dp.message_handler(lambda message: message.text == "➕ Добавить аккаунт")
async def add_account(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        accounts_count = db.query(Account).filter_by(user_id=message.from_user.id).count()
        
        if accounts_count >= user.max_accounts:
            await message.answer(f"❌ Лимит аккаунтов исчерпан ({user.max_accounts})!")
            return
    finally:
        db.close()
    
    await message.answer("📱 Введите номер:\n<code>+380123456789</code>", reply_markup=get_back_keyboard())
    await UserStates.waiting_phone.set()

@dp.message_handler(lambda message: message.text == "📋 Мои аккаунты")
async def my_accounts_list(message: types.Message):
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter_by(user_id=message.from_user.id).all()
    finally:
        db.close()
    
    if not accounts:
        await message.answer("❌ Нет подключенных аккаунтов.")
        return
    
    text = "📋 <b>Ваши аккаунты:</b>\n\n"
    for i, acc in enumerate(accounts, 1):
        text += f"{i}. <code>{acc.phone_number or '—'}</code> (ID: {acc.id})\n"
    
    await message.answer(text)

# Рассылка
@dp.message_handler(lambda message: message.text == "📨 Рассылка")
async def broadcast_menu(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter_by(user_id=message.from_user.id).all()
    finally:
        db.close()
    
    if not accounts:
        await message.answer("❌ Сначала добавьте аккаунт!")
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    for acc in accounts:
        keyboard.add(KeyboardButton(f"📱 {acc.phone_number}"))
    keyboard.add(KeyboardButton("✅ Все аккаунты"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    await message.answer("📱 Выберите аккаунт(ы):", reply_markup=keyboard)
    await UserStates.selecting_accounts.set()

@dp.message_handler(state=UserStates.selecting_accounts)
async def process_account_selection(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
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
        for acc in accounts:
            if message.text == f"📱 {acc.phone_number}":
                selected_ids = [acc.id]
                break
    
    if not selected_ids:
        await message.answer("❌ Выберите из списка.")
        return
    
    await state.update_data(account_ids=selected_ids)
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🛡 Безопасный режим"), KeyboardButton("⚡ Обычный режим"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    await message.answer("🛡 Выберите режим:", reply_markup=keyboard)
    await UserStates.waiting_message.set()

@dp.message_handler(state=UserStates.waiting_message)
async def process_mode(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    safe_mode = message.text == "🛡 Безопасный режим"
    await state.update_data(safe_mode=safe_mode)
    
    if safe_mode:
        await message.answer(
            "📝 Введите 3 текста через разделитель <code>|||</code>:\n"
            "<code>Текст1|||Текст2|||Текст3</code>",
            reply_markup=get_back_keyboard()
        )
    else:
        await message.answer("📝 Введите текст сообщения:", reply_markup=get_back_keyboard())
    
    await UserStates.waiting_interval.set()

@dp.message_handler(state=UserStates.waiting_interval)
async def process_text(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    messages = [m.strip() for m in message.text.split('|||') if m.strip()]
    data = await state.get_data()
    safe_mode = data.get('safe_mode', False)
    
    if safe_mode and len(messages) < 3:
        await message.answer("❌ Нужно минимум 3 текста для безопасного режима!")
        return
    
    await state.update_data(messages=messages)
    await message.answer("⏱ Введите интервал в минутах (30-120):", reply_markup=get_back_keyboard())
    await UserStates.waiting_more_messages.set()

@dp.message_handler(state=UserStates.waiting_more_messages)
async def process_final(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        interval = int(message.text)
        if interval < 30:
            await message.answer("❌ Минимум 30 минут.")
            return
        if interval > 120:
            await message.answer("❌ Максимум 120 минут.")
            return
        
        data = await state.get_data()
        account_ids = data.get('account_ids', [])
        messages = data.get('messages', [])
        safe_mode = data.get('safe_mode', False)
        
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
            f"⏱ Интервал: {interval} мин"
            f"{' (±20%)' if safe_mode else ''}",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
        
    except ValueError:
        await message.answer("❌ Введите число.")

# Админ-панель
@dp.message_handler(lambda message: message.text == "🔐 Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔐 <b>Админ-панель</b>", reply_markup=get_admin_keyboard())

@dp.message_handler(lambda message: message.text == "📢 Рассылка всем")
async def admin_broadcast_all(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer("📝 Введите текст для рассылки всем пользователям:", reply_markup=get_back_keyboard())
    await UserStates.admin_broadcast.set()

@dp.message_handler(state=UserStates.admin_broadcast)
async def process_admin_broadcast(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
        return
    
    db = SessionLocal()
    try:
        users = db.query(User).all()
        count = 0
        for user in users:
            try:
                await bot.send_message(user.user_id, f"📢 <b>Сообщение от администратора:</b>\n\n{message.text}")
                count += 1
            except:
                continue
    finally:
        db.close()
    
    await message.answer(f"✅ Отправлено {count} пользователям.", reply_markup=get_admin_keyboard())
    await state.finish()

@dp.message_handler(lambda message: message.text == "🔑 Создать ключ")
async def admin_create_key(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔑 Введите срок (дней, -1 = бессрочно):", reply_markup=get_back_keyboard())
    await UserStates.admin_create_key.set()

@dp.message_handler(state=UserStates.admin_create_key)
async def process_key(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
        return
    
    try:
        duration = int(message.text)
        key = generate_license_key(duration)
        db = SessionLocal()
        try:
            db.add(LicenseKey(key=key, duration_days=duration))
            db.commit()
        finally:
            db.close()
        
        duration_text = "♾ Бессрочная" if duration == -1 else f"{duration} дн."
        await message.answer(f"✅ Ключ: <code>{key}</code>\nСрок: {duration_text}", reply_markup=get_admin_keyboard())
        await state.finish()
    except ValueError:
        await message.answer("❌ Введите число.")

@dp.message_handler(lambda message: message.text == "⚙️ Лимиты аккаунтов")
async def admin_limits(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("➕ Выдать"), KeyboardButton("➖ Забрать"))
    keyboard.add(KeyboardButton("♾ Бесконечно"), KeyboardButton("❌ Обнулить"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    
    await message.answer("⚙️ <b>Лимиты аккаунтов</b>", reply_markup=keyboard)

@dp.message_handler(lambda message: message.text == "➕ Выдать")
async def admin_give(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Введите: <code>ID количество</code>", reply_markup=get_back_keyboard())
    await UserStates.admin_set_accounts.set()

@dp.message_handler(lambda message: message.text == "♾ Бесконечно")
async def admin_unlimited(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Введите ID пользователя:", reply_markup=get_back_keyboard())
    await UserStates.admin_give_unlimited.set()

@dp.message_handler(lambda message: message.text == "❌ Обнулить")
async def admin_zero(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Введите ID пользователя:", reply_markup=get_back_keyboard())
    await UserStates.admin_remove_all.set()

@dp.message_handler(state=UserStates.admin_set_accounts)
async def process_set_accounts(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
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
                await message.answer(f"✅ Лимит {count} акк. для {user_id}", reply_markup=get_admin_keyboard())
            else:
                await message.answer("❌ Не найден.")
        finally:
            db.close()
        
        await state.finish()
    except:
        await message.answer("❌ Формат: <code>ID количество</code>")

@dp.message_handler(state=UserStates.admin_give_unlimited)
async def process_unlimited(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
        return
    
    try:
        user_id = int(message.text)
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            if user:
                user.max_accounts = 999999
                db.commit()
                await message.answer(f"✅ ♾ для {user_id}", reply_markup=get_admin_keyboard())
        finally:
            db.close()
        await state.finish()
    except:
        await message.answer("❌ Введите ID.")

@dp.message_handler(state=UserStates.admin_remove_all)
async def process_remove_all(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Админ-панель:", reply_markup=get_admin_keyboard())
        return
    
    try:
        user_id = int(message.text)
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            if user:
                user.max_accounts = 0
                db.commit()
                await message.answer(f"✅ Обнулено для {user_id}", reply_markup=get_admin_keyboard())
        finally:
            db.close()
        await state.finish()
    except:
        await message.answer("❌ Введите ID.")

# Статистика
@dp.message_handler(lambda message: message.text == "📊 Статистика")
async def show_stats(message: types.Message):
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_accounts = db.query(Account).count()
        total_broadcasts = db.query(BroadcastTask).count()
        active_broadcasts = db.query(BroadcastTask).filter_by(status='active').count()
        total_keys = db.query(LicenseKey).count()
        used_keys = db.query(LicenseKey).filter_by(is_used=True).count()
        
        stats_text = f"""
📊 <b>СТАТИСТИКА</b>

👥 Пользователей: <b>{total_users}</b>
📱 Аккаунтов: <b>{total_accounts}</b>
📨 Рассылок: <b>{total_broadcasts}</b>
🔄 Активных: <b>{active_broadcasts}</b>
🔑 Ключей: <b>{total_keys}</b>
📤 Использовано: <b>{used_keys}</b>
"""
        await message.answer(stats_text, reply_markup=get_main_keyboard(message.from_user.id))
    finally:
        db.close()

# Подключение аккаунта
@dp.message_handler(state=UserStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    phone = message.text.strip()
    if not phone.startswith('+'):
        await message.answer("❌ Номер с '+'")
        return
    
    client = Client(
        f"s_{message.from_user.id}_{len(phone_code_hashes)}",
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
        await message.answer("📨 Код отправлен! Введите код:", reply_markup=get_back_keyboard())
        await UserStates.waiting_code.set()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_code)
async def process_code(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
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
            await message.answer("🔐 Введите пароль 2FA:", reply_markup=get_back_keyboard())
            await UserStates.waiting_password.set()
            return
        
        session_string = await client.export_session_string()
        
        db = SessionLocal()
        try:
            db.add(Account(user_id=message.from_user.id, phone_number=phone, session_string=session_string))
            db.commit()
        finally:
            db.close()
        
        await message.answer("✅ <b>Аккаунт добавлен!</b>", reply_markup=get_main_keyboard(message.from_user.id))
        await state.finish()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_password)
async def process_password(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
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
            db.add(Account(user_id=message.from_user.id, phone_number=phone, session_string=session_string))
            db.commit()
        finally:
            db.close()
        
        await message.answer("✅ <b>Аккаунт добавлен!</b>", reply_markup=get_main_keyboard(message.from_user.id))
        await state.finish()
    except PasswordHashInvalid:
        await message.answer("❌ Неверный пароль:")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

# Активация лицензии
@dp.message_handler(lambda message: message.text == "🔑 Активировать лицензию")
async def activate_license(message: types.Message):
    await message.answer("🔑 Введите ключ:", reply_markup=get_back_keyboard())
    await UserStates.waiting_license.set()

@dp.message_handler(state=UserStates.waiting_license)
async def process_license(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    key = message.text.strip()
    db = SessionLocal()
    try:
        lic = db.query(LicenseKey).filter_by(key=key).first()
        if not lic:
            await message.answer("❌ Неверный ключ.")
            return
        if lic.is_used:
            await message.answer("❌ Ключ использован.")
            return
        
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if lic.duration_days == -1:
            expiry = datetime.utcnow() + timedelta(days=36500)
        else:
            expiry = datetime.utcnow() + timedelta(days=lic.duration_days)
        
        user.license_key = key
        user.license_expiry = expiry
        lic.is_used = True
        lic.used_by = user.user_id
        db.commit()
        
        await message.answer(
            f"✅ <b>Лицензия активирована!</b>\n📅 До: <b>{expiry.strftime('%d.%m.%Y')}</b>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
    finally:
        db.close()

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
