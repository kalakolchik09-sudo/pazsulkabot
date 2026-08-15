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
from pyrogram.errors import FloodWait, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded

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
    current_cycle = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Пересоздаем таблицу broadcast_tasks
try:
    # Проверяем существующие таблицы
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    
    if 'broadcast_tasks' in existing_tables:
        # Получаем существующие колонки
        columns = [col['name'] for col in inspector.get_columns('broadcast_tasks')]
        
        # Если нет новых колонок, пересоздаем таблицу
        if 'current_cycle' not in columns or 'sent_count' not in columns:
            logger.info("Recreating broadcast_tasks table...")
            BroadcastTask.__table__.drop(engine)
    
    Base.metadata.create_all(engine)
    logger.info("✅ Tables ready")
except Exception as e:
    logger.error(f"Error with tables: {e}")

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
            db.add(user)
            db.commit()
        return user
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
    return keyboard

# Функция рассылки
async def start_broadcast(user_id: int, task_id: int):
    """Бесконечная рассылка с случайными интервалами"""
    db = SessionLocal()
    try:
        task = db.query(BroadcastTask).filter_by(id=task_id).first()
        user = db.query(User).filter_by(user_id=user_id).first()
    finally:
        db.close()
    
    if not task or not user or not user.session_string:
        logger.error(f"No task or user or session for {user_id}")
        return
    
    client = Client(
        f"broadcast_{user_id}_{task_id}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=user.session_string
    )
    
    try:
        await client.start()
        logger.info(f"Broadcast client started for user {user_id}")
        
        # Получаем все группы
        dialogs = []
        try:
            async for dialog in client.get_dialogs():
                if dialog.chat.type in ['group', 'supergroup']:
                    dialogs.append({
                        'id': dialog.chat.id,
                        'title': dialog.chat.title or 'Без названия'
                    })
            logger.info(f"Found {len(dialogs)} groups for user {user_id}")
        except Exception as e:
            logger.error(f"Error getting dialogs: {e}")
            await bot.send_message(user_id, f"❌ Ошибка получения групп: {str(e)}")
            return
        
        if not dialogs:
            await bot.send_message(
                user_id,
                "❌ <b>У вас нет групп!</b>\n\n"
                "Добавьте аккаунт в группы и попробуйте снова."
            )
            return
        
        # Обновляем количество групп
        db = SessionLocal()
        try:
            db.query(BroadcastTask).filter_by(id=task_id).update({'groups_count': len(dialogs)})
            db.commit()
        finally:
            db.close()
        
        await bot.send_message(
            user_id,
            f"🚀 <b>Рассылка запущена!</b>\n\n"
            f"👥 Найдено групп: <b>{len(dialogs)}</b>\n"
            f"📝 Сообщение: {task.message_text[:100]}\n"
            f"⏱ Базовый интервал: {task.interval_minutes} мин\n"
            f"🎲 Случайное отклонение: ±5 мин\n\n"
            f"Начинаю отправку..."
        )
        
        cycle = 0
        total_sent = 0
        
        # Бесконечный цикл рассылки
        while True:
            # Проверяем статус задачи
            db = SessionLocal()
            try:
                current_task = db.query(BroadcastTask).filter_by(id=task_id).first()
            finally:
                db.close()
            
            if not current_task or current_task.status != 'active':
                logger.info(f"Task {task_id} stopped")
                break
            
            cycle += 1
            sent_in_cycle = 0
            
            logger.info(f"Starting cycle {cycle} for task {task_id}")
            
            # Отправляем сообщения во все группы
            for group in dialogs:
                # Проверяем статус
                db = SessionLocal()
                try:
                    current_task = db.query(BroadcastTask).filter_by(id=task_id).first()
                finally:
                    db.close()
                
                if not current_task or current_task.status != 'active':
                    break
                
                try:
                    await client.send_message(group['id'], task.message_text)
                    sent_in_cycle += 1
                    total_sent += 1
                    
                    logger.info(f"Cycle {cycle}: Sent to {group['title']} ({sent_in_cycle}/{len(dialogs)})")
                    
                    # Обновляем счетчики
                    db = SessionLocal()
                    try:
                        db.query(BroadcastTask).filter_by(id=task_id).update({
                            'current_cycle': cycle,
                            'sent_count': total_sent
                        })
                        db.commit()
                    finally:
                        db.close()
                    
                    # Уведомляем пользователя
                    await bot.send_message(
                        user_id,
                        f"📨 <b>Цикл {cycle}</b>\n"
                        f"👥 Группа: {group['title']}\n"
                        f"📊 Прогресс: {sent_in_cycle}/{len(dialogs)}\n"
                        f"✅ Всего отправлено: {total_sent}"
                    )
                    
                    # Случайная пауза между группами (30-60 секунд)
                    group_pause = random.randint(30, 60)
                    await asyncio.sleep(group_pause)
                    
                except FloodWait as e:
                    logger.warning(f"FloodWait: {e.value} seconds")
                    await bot.send_message(
                        user_id,
                        f"⚠️ <b>Пауза из-за FloodWait</b>\n"
                        f"⏱ Ждем {e.value} секунд..."
                    )
                    await asyncio.sleep(e.value)
                except Exception as e:
                    logger.error(f"Error sending to {group['title']}: {e}")
                    continue
            
            # Цикл завершен
            if sent_in_cycle > 0:
                await bot.send_message(
                    user_id,
                    f"✅ <b>Цикл {cycle} завершен!</b>\n\n"
                    f"📨 Отправлено в {sent_in_cycle} групп\n"
                    f"📊 Всего за все время: {total_sent}\n\n"
                    f"⏱ Следующий цикл через {task.interval_minutes}±5 мин..."
                )
            
            # Случайный интервал между циклами
            base_interval = task.interval_minutes * 60
            random_offset = random.randint(-300, 300)  # ±5 минут
            next_interval = max(300, base_interval + random_offset)  # Минимум 5 минут
            
            next_time = datetime.now() + timedelta(seconds=next_interval)
            logger.info(f"Next cycle in {next_interval} seconds at {next_time.strftime('%H:%M:%S')}")
            
            # Ждем до следующего цикла
            await asyncio.sleep(next_interval)
        
        # Задача остановлена
        db = SessionLocal()
        try:
            db.query(BroadcastTask).filter_by(id=task_id).update({'status': 'completed'})
            db.commit()
        finally:
            db.close()
        
        await bot.send_message(
            user_id,
            f"⏹ <b>Рассылка остановлена</b>\n\n"
            f"📊 Всего циклов: {cycle}\n"
            f"📨 Всего отправлено: {total_sent}"
        )
        
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        await bot.send_message(user_id, f"❌ Ошибка рассылки: {str(e)}")
    finally:
        await client.stop()

# User handlers
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"Используйте кнопки ниже:",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    await message.answer("🔐 Админ-панель:", reply_markup=get_admin_keyboard())

# Кнопки
@dp.message_handler(lambda message: message.text == "🔑 Активировать лицензию")
async def activate_license(message: types.Message):
    await message.answer("🔑 Введите лицензионный ключ:")
    await UserStates.waiting_license.set()

@dp.message_handler(lambda message: message.text == "📱 Подключить аккаунт")
async def connect_account(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    await message.answer(
        "📱 <b>Подключение аккаунта</b>\n\n"
        "Введите номер телефона в международном формате:\n"
        "Например: <code>+79123456789</code>"
    )
    await UserStates.waiting_phone.set()

@dp.message_handler(lambda message: message.text == "📨 Создать рассылку")
async def create_broadcast(message: types.Message):
    if not is_valid_license(message.from_user.id):
        await message.answer("❌ Нет активной лицензии!")
        return
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        if not user or not user.session_string:
            await message.answer("❌ Сначала подключите аккаунт!")
            return
    finally:
        db.close()
    
    await message.answer("📝 Введите текст сообщения для рассылки:")
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
    await message.answer("✅ Рассылки остановлены.")

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
        text += f"⏱ Интервал: {task.interval_minutes} мин\n"
        text += f"👥 Групп: {task.groups_count}\n"
        text += f"🔄 Циклов: {task.current_cycle}\n"
        text += f"📨 Отправлено: {task.sent_count}\n\n"
    
    await message.answer(text)

# Админ кнопки
@dp.message_handler(lambda message: message.text == "🔑 Создать ключ")
async def admin_create_key(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    await message.answer("🔑 Введите срок (дней, -1 для бессрочного):")
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
            text += f"\n🔑 {license_status}\n\n"
        
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
        total_broadcasts = db.query(BroadcastTask).count()
        
        await message.answer(
            f"📊 <b>Статистика:</b>\n\n"
            f"👥 Пользователей: <b>{total_users}</b>\n"
            f"🔑 Ключей: <b>{total_keys}</b>\n"
            f"📤 Использовано: <b>{used_keys}</b>\n"
            f"📨 Рассылок: <b>{total_broadcasts}</b>"
        )
    finally:
        db.close()

@dp.message_handler(lambda message: message.text == "🚫 Заблокировать")
async def admin_block_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Нет доступа")
        return
    await message.answer("Введите ID пользователя:")
    await UserStates.admin_block_user.set()

# Обработка состояний
@dp.message_handler(state=UserStates.waiting_license)
async def process_license_key(message: types.Message, state: FSMContext):
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
            f"✅ <b>Лицензия активирована!</b>\n"
            f"📅 До: <b>{expiry.strftime('%d.%m.%Y')}</b>",
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
        await message.answer(f"✅ Ключ: <code>{key}</code>\nСрок: {duration_text}")
        await state.finish()
    except ValueError:
        await message.answer("❌ Введите число.")

@dp.message_handler(state=UserStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    logger.info(f"Phone received: {phone}")
    
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с '+'. Попробуйте снова:")
        return
    
    await message.answer("⏳ Отправляю код подтверждения...")
    
    client = Client(
        f"session_{message.from_user.id}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    
    try:
        await client.connect()
        logger.info("Client connected")
        
        sent_code = await client.send_code(phone)
        logger.info(f"Code sent to {phone}")
        
        await state.update_data(phone=phone)
        phone_code_hashes[message.from_user.id] = sent_code.phone_code_hash
        active_clients[message.from_user.id] = client
        
        await message.answer(
            "📨 <b>Код отправлен!</b>\n\n"
            "Введите код из SMS:"
        )
        await UserStates.waiting_code.set()
        
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await message.answer(f"❌ Ошибка отправки кода: {str(e)}")
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
    client = active_clients.get(message.from_user.id)
    phone_code_hash = phone_code_hashes.get(message.from_user.id)
    
    if not client or not phone_code_hash:
        await message.answer("❌ Сессия истекла. Нажмите '📱 Подключить аккаунт' снова.")
        await state.finish()
        return
    
    await message.answer("⏳ Проверяю код...")
    
    try:
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
        
        await message.answer(
            "✅ <b>Аккаунт успешно подключен!</b>",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.finish()
        
    except Exception as e:
        logger.error(f"Error signing in: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")
        await state.finish()

@dp.message_handler(state=UserStates.waiting_message)
async def process_message_text(message: types.Message, state: FSMContext):
    await state.update_data(message_text=message.text)
    await message.answer("⏱ Введите базовый интервал в минутах (мин. 5):")
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
                status='active',
                current_cycle=0,
                sent_count=0
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            task_id = task.id
        finally:
            db.close()
        
        # Запускаем рассылку в фоне
        asyncio.create_task(start_broadcast(message.from_user.id, task_id))
        
        await message.answer(
            f"✅ <b>Рассылка запущена!</b>\n\n"
            f"📝 {message_text[:100]}\n"
            f"⏱ Базовый интервал: {interval} мин (±5 мин)",
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
