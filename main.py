import os
import asyncio
import logging
import random
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

# ==== ENV ====
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
REDIS_URL = os.getenv('REDIS_URL', '')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required")
if not REDIS_URL:
    raise ValueError("REDIS_URL is required (подключи Railway Redis)")

# ==== Logging ====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ==== Telegram imports ====
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.redis import RedisStorage2
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import executor

from pyrogram import Client
from pyrogram.errors import (
    FloodWait, PhoneCodeExpired, PhoneCodeInvalid,
    SessionPasswordNeeded, PasswordHashInvalid,
    AuthKeyUnregistered, UserDeactivated, ChatWriteForbidden,
    UserAlreadyParticipant, PeerFlood,
)
from pyrogram.enums import ChatType

# ==== DB imports ====
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger,
    String, DateTime, Boolean, Text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ==== DATABASE ====
def get_database_url():
    for key in ('DATABASE_PUBLIC_URL', 'DATABASE_URL'):
        url = os.getenv(key, '')
        if url:
            if url.startswith('postgres://'):
                url = url.replace('postgres://', 'postgresql://', 1)
            return url.strip()
    return 'sqlite:///bot.db'


DATABASE_URL = get_database_url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


# ==== MODELS ====
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
    session_string = Column(Text, nullable=True)  # ОТКРЫТЫМ ТЕКСТОМ
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


Base.metadata.create_all(engine)


# ==== STATES ====
class UserStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_license = State()
    admin_create_key = State()
    admin_broadcast = State()
    waiting_message = State()
    waiting_interval = State()
    selecting_accounts = State()
    waiting_more_messages = State()
    waiting_safe_messages = State()


# ==== BOT + STORAGE ====
_r = urlparse(REDIS_URL)
_storage_kwargs = dict(
    host=_r.hostname,
    port=_r.port or 6379,
    db=int((_r.path or '/0').lstrip('/') or 0),
    password=_r.password,
    pool_size=20,
    prefix='fsm',
)
if _r.scheme == 'rediss':
    _storage_kwargs['ssl'] = True

storage = RedisStorage2(**_storage_kwargs)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# незавершённые клиенты логина (RAM)
active_clients: dict = {}


# ==== EXCEPTIONS ====
class _StopBroadcast(Exception):
    pass


# ==== HELPERS ====
def generate_license_key() -> str:
    db = SessionLocal()
    try:
        while True:
            key = f"LIC-{secrets.token_urlsafe(16).upper()}"
            if not db.query(LicenseKey).filter_by(key=key).first():
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
            user.max_accounts = 999999 if user_id == ADMIN_ID else 3
            db.add(user)
            db.commit()
        return user
    finally:
        db.close()


# ==== KEYBOARDS ====
def get_main_keyboard(user_id: int):
    keyboard = InlineKeyboardMarkup(row_width=2)

    if is_valid_license(user_id):
        keyboard.add(
            InlineKeyboardButton("📱 Аккаунты", callback_data="menu_accounts"),
            InlineKeyboardButton("📨 Рассылка", callback_data="menu_broadcast"),
        )
        keyboard.add(
            InlineKeyboardButton("👤 Профиль", callback_data="menu_profile"),
            InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
        )
        keyboard.add(InlineKeyboardButton("⏹ Остановить все", callback_data="stop_all"))
        if user_id == ADMIN_ID:
            keyboard.add(InlineKeyboardButton("🔐 Админ-панель", callback_data="menu_admin"))
    else:
        keyboard.add(InlineKeyboardButton("🔑 Активировать лицензию", callback_data="menu_license"))
        keyboard.add(InlineKeyboardButton("👤 Профиль", callback_data="menu_profile"))

    return keyboard


def get_back_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    return keyboard


# ==== GROUPS + BROADCAST ====
async def get_user_groups(client: Client):
    groups = []
    try:
        async for dialog in client.get_dialogs():
            if dialog.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                groups.append({
                    'id': dialog.chat.id,
                    'title': dialog.chat.title or 'Без названия',
                })
    except FloodWait as e:
        logger.warning(f"get_dialogs FloodWait {e.value}s")
        await asyncio.sleep(e.value)
    except Exception:
        logger.exception("get_dialogs упал")
    return groups


async def start_broadcast(user_id: int, task_id: int):
    db = SessionLocal()
    try:
        task = db.query(BroadcastTask).filter_by(id=task_id).first()
        if not task:
            return
        account_ids = json.loads(task.account_ids or '[]')
        messages = json.loads(task.messages or '[]')
        safe_mode = task.safe_mode
        interval_minutes = task.interval_minutes
    finally:
        db.close()

    if not account_ids or not messages:
        return

    clients = []
    db = SessionLocal()
    try:
        for acc_id in account_ids:
            account = db.query(Account).filter_by(id=acc_id).first()
            if account and account.session_string:
                clients.append(Client(
                    f"acc_{acc_id}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=account.session_string,
                    in_memory=True,
                ))
    finally:
        db.close()

    if not clients:
        await bot.send_message(user_id, "❌ Нет валидных аккаунтов для рассылки.")
        return

    alive_clients = []
    for client in clients:
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"{client.name}: не авторизован, пропускаю")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                continue
            alive_clients.append(client)
        except FloodWait as e:
            logger.warning(f"{client.name}: FloodWait {e.value}s при connect")
            await asyncio.sleep(e.value)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    alive_clients.append(client)
            except Exception:
                logger.exception(f"{client.name}: не удалось переподключиться")
        except Exception:
            logger.exception(f"{client.name}: ошибка подключения")

    if not alive_clients:
        await bot.send_message(user_id, "❌ Все аккаунты невалидны (сессии отозваны).")
        return

    stop_keyboard = InlineKeyboardMarkup()
    stop_keyboard.add(InlineKeyboardButton(
        "⏹ Остановить рассылку",
        callback_data=f"stop_task_{task_id}",
    ))

    status_msg = await bot.send_message(
        user_id,
        f"🚀 <b>Рассылка запущена</b>\n"
        f"📱 Аккаунтов: {len(alive_clients)}\n"
        f"📝 Текстов: {len(messages)}\n"
        f"🛡 Режим: {'Безопасный' if safe_mode else 'Обычный'}\n"
        f"⏱ Интервал: {interval_minutes} мин",
        reply_markup=stop_keyboard,
    )

    cycle = 0
    total_sent = 0

    try:
        while True:
            db = SessionLocal()
            try:
                current = db.query(BroadcastTask).filter_by(id=task_id).first()
            finally:
                db.close()
            if not current or current.status != 'active':
                break

            cycle += 1

            for client in list(alive_clients):
                groups = await get_user_groups(client)

                for group in groups:
                    db = SessionLocal()
                    try:
                        check = db.query(BroadcastTask).filter_by(id=task_id).first()
                    finally:
                        db.close()
                    if not check or check.status != 'active':
                        raise _StopBroadcast()

                    text = random.choice(messages)
                    try:
                        await client.send_message(group['id'], text)
                        total_sent += 1
                    except FloodWait as e:
                        logger.warning(f"FloodWait {e.value}s в {group['id']}")
                        await asyncio.sleep(e.value)
                        continue
                    except (UserDeactivated, AuthKeyUnregistered):
                        logger.error(f"{client.name}: аккаунт мёртв, снимаю с рассылки")
                        if client in alive_clients:
                            alive_clients.remove(client)
                        break
                    except (ChatWriteForbidden, UserAlreadyParticipant):
                        continue
                    except PeerFlood:
                        logger.error(f"{client.name}: PeerFlood, пауза 5 мин")
                        await asyncio.sleep(300)
                        continue
                    except Exception as e:
                        logger.error(f"{client.name} → {group['id']}: {type(e).__name__}: {e}")
                        continue

                    db = SessionLocal()
                    try:
                        db.query(BroadcastTask).filter_by(id=task_id).update({
                            'current_cycle': cycle,
                            'sent_count': total_sent,
                            'groups_count': len(groups),
                        })
                        db.commit()
                    finally:
                        db.close()

                    try:
                        await status_msg.edit_text(
                            f"🔄 <b>Цикл {cycle}</b>\n"
                            f"📨 Отправлено: <b>{total_sent}</b>\n"
                            f"📱 Аккаунтов активно: {len(alive_clients)}",
                            reply_markup=stop_keyboard,
                        )
                    except Exception:
                        pass

                    await asyncio.sleep(1)

            if not alive_clients:
                break

            if safe_mode:
                base = interval_minutes * 60
                variation = int(base * 0.2)
                interval_seconds = max(1800, min(7200, base + random.randint(-variation, variation)))
            else:
                interval_seconds = interval_minutes * 60

            for _ in range(interval_seconds // 5):
                db = SessionLocal()
                try:
                    check = db.query(BroadcastTask).filter_by(id=task_id).first()
                finally:
                    db.close()
                if not check or check.status != 'active':
                    raise _StopBroadcast()
                await asyncio.sleep(5)

    except _StopBroadcast:
        pass
    except Exception:
        logger.exception("start_broadcast упал")
    finally:
        for client in clients:
            try:
                await client.disconnect()
            except Exception:
                pass

        try:
            await status_msg.edit_text(
                f"⏹ <b>Рассылка остановлена</b>\n"
                f"📨 Всего: <b>{total_sent}</b>\n"
                f"🔄 Циклов: <b>{cycle}</b>",
            )
        except Exception:
            pass


# ==== BASIC HANDLERS ====
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    create_user_if_not_exists(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\nВыбери действие:",
        reply_markup=get_main_keyboard(message.from_user.id),
    )


@dp.message_handler(commands=['cancel'], state='*')
async def cmd_cancel(message: types.Message, state: FSMContext):
    await _cleanup_login(message.from_user.id, state)
    await message.answer("Отменено.", reply_markup=get_main_keyboard(message.from_user.id))


@dp.callback_query_handler(lambda c: c.data == "back")
async def back(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await _cleanup_login(callback_query.from_user.id, state)
    await bot.edit_message_text(
        "Главное меню:",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=get_main_keyboard(callback_query.from_user.id),
    )


@dp.callback_query_handler(lambda c: c.data == "stop_all")
async def cb_stop_all(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    db = SessionLocal()
    try:
        db.query(BroadcastTask).filter_by(
            user_id=callback_query.from_user.id,
            status='active',
        ).update({'status': 'paused'})
        db.commit()
    finally:
        db.close()

    await bot.edit_message_text(
        "✅ <b>Все рассылки остановлены!</b>",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=get_main_keyboard(callback_query.from_user.id),
    )


@dp.callback_query_handler(lambda c: c.data.startswith("stop_task_"))
async def cb_stop_task(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id, "⏹ Останавливаю...")
    task_id = int(callback_query.data.split("_")[2])
    db = SessionLocal()
    try:
        task = db.query(BroadcastTask).filter_by(id=task_id).first()
        if task:
            task.status = 'paused'
            db.commit()
    finally:
        db.close()

    await bot.edit_message_text(
        "⏹ <b>Рассылка остановлена!</b>",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=get_main_keyboard(callback_query.from_user.id),
    )


@dp.callback_query_handler(lambda c: c.data == "menu_profile")
async def cb_profile(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=callback_query.from_user.id).first()
        if not user:
            return
        accounts = db.query(Account).filter_by(user_id=callback_query.from_user.id).all()
        total_broadcasts = db.query(BroadcastTask).filter_by(user_id=callback_query.from_user.id).count()

        if user.user_id == ADMIN_ID:
            license_status = "👑 <b>Администратор</b>"
        elif user.license_expiry and user.license_expiry > datetime.utcnow():
            license_status = f"✅ <b>Активна</b> (до {user.license_expiry.strftime('%d.%m.%Y')})"
        else:
            license_status = "❌ <b>Не активирована</b>"

        text = (
            "👤 <b>ПРОФИЛЬ</b>\n\n"
            f"🆔 ID: <code>{user.user_id}</code>\n"
            f"👤 Имя: {user.first_name or '—'}\n\n"
            f"💳 Подписка: {license_status}\n\n"
            f"📱 Аккаунты: <b>{len(accounts)}</b> / "
            f"{'♾' if user.max_accounts >= 999999 else user.max_accounts}\n\n"
            f"📊 Рассылок: <b>{total_broadcasts}</b>"
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
        await bot.edit_message_text(
            text,
            callback_query.from_user.id,
            callback_query.message.message_id,
            reply_markup=keyboard,
        )
    finally:
        db.close()


@dp.callback_query_handler(lambda c: c.data == "menu_stats")
async def cb_stats(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_accounts = db.query(Account).count()
        total_broadcasts = db.query(BroadcastTask).count()
        text = (
            "📊 <b>СТАТИСТИКА</b>\n\n"
            f"👥 Пользователей: <b>{total_users}</b>\n"
            f"📱 Аккаунтов: <b>{total_accounts}</b>\n"
            f"📨 Рассылок: <b>{total_broadcasts}</b>"
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
        await bot.edit_message_text(
            text,
            callback_query.from_user.id,
            callback_query.message.message_id,
            reply_markup=keyboard,
        )
    finally:
        db.close()


@dp.callback_query_handler(lambda c: c.data == "menu_license")
async def cb_license(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "🔑 Введи лицензионный ключ:",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )
    await UserStates.waiting_license.set()


@dp.callback_query_handler(lambda c: c.data == "menu_accounts")
async def cb_accounts(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("➕ Добавить", callback_data="acc_add"),
        InlineKeyboardButton("📋 Список", callback_data="acc_list"),
    )
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "📱 <b>Аккаунты</b>",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda c: c.data == "acc_add")
async def cb_acc_add(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(user_id=callback_query.from_user.id).first()
        count = db.query(Account).filter_by(user_id=callback_query.from_user.id).count()
        if user and count >= user.max_accounts:
            await bot.answer_callback_query(
                callback_query.id,
                f"Лимит: {user.max_accounts}",
                show_alert=True,
            )
            return
    finally:
        db.close()

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "📱 Введи номер в формате <code>+380123456789</code>:",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )
    await UserStates.waiting_phone.set()


@dp.callback_query_handler(lambda c: c.data == "acc_list")
async def cb_acc_list(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter_by(user_id=callback_query.from_user.id).all()
    finally:
        db.close()

    if not accounts:
        text = "❌ Нет аккаунтов."
    else:
        text = "📋 <b>Твои аккаунты:</b>\n\n"
        for i, acc in enumerate(accounts, 1):
            text += f"{i}. <code>{acc.phone_number or '—'}</code>\n"

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        text,
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda c: c.data == "menu_broadcast")
async def cb_broadcast(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter_by(user_id=callback_query.from_user.id).all()
    finally:
        db.close()

    if not accounts:
        await bot.answer_callback_query(callback_query.id, "Сначала добавь аккаунт!", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(row_width=1)
    for acc in accounts:
        keyboard.add(InlineKeyboardButton(f"📱 {acc.phone_number}", callback_data=f"sel_acc_{acc.id}"))
    keyboard.add(InlineKeyboardButton("✅ Все аккаунты", callback_data="sel_acc_all"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "📱 Выбери аккаунт:",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda c: c.data.startswith("sel_acc_"))
async def cb_select_account(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    if callback_query.data == "sel_acc_all":
        db = SessionLocal()
        try:
            accounts = db.query(Account).filter_by(user_id=callback_query.from_user.id).all()
            selected_ids = [acc.id for acc in accounts]
        finally:
            db.close()
    else:
        acc_id = int(callback_query.data.split("_")[2])
        selected_ids = [acc_id]

    await state.update_data(account_ids=selected_ids)

    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("🛡 Безопасный режим", callback_data="mode_safe"))
    keyboard.add(InlineKeyboardButton("⚡ Обычный режим", callback_data="mode_normal"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "🛡 Выбери режим:",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda c: c.data.startswith("mode_"))
async def cb_mode(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    safe_mode = callback_query.data == "mode_safe"
    await state.update_data(safe_mode=safe_mode)

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))

    if safe_mode:
        await state.update_data(safe_messages=[], safe_counter=0)
        await bot.edit_message_text(
            "📝 <b>Безопасный режим</b>\n\nОтправь текст №1 (из 3):",
            callback_query.from_user.id,
            callback_query.message.message_id,
            reply_markup=keyboard,
        )
        await UserStates.waiting_safe_messages.set()
    else:
        await bot.edit_message_text(
            "📝 Введи текст сообщения:",
            callback_query.from_user.id,
            callback_query.message.message_id,
            reply_markup=keyboard,
        )
        await UserStates.waiting_interval.set()


@dp.callback_query_handler(lambda c: c.data == "menu_admin")
async def cb_admin(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    if callback_query.from_user.id != ADMIN_ID:
        return

    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔑 Создать ключ", callback_data="adm_key"),
        InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_broadcast"),
    )
    keyboard.add(InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "🔐 <b>Админ-панель</b>",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda c: c.data == "adm_key")
async def cb_adm_key(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "🔑 Введи срок (дней, -1 = бессрочно):",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )
    await UserStates.admin_create_key.set()


@dp.callback_query_handler(lambda c: c.data == "adm_broadcast")
async def cb_adm_broadcast(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back"))
    await bot.edit_message_text(
        "📝 Введи текст для рассылки:",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard,
    )
    await UserStates.admin_broadcast.set()


# ==== MESSAGE HANDLERS ====
@dp.message_handler(state=UserStates.waiting_license)
async def process_license(message: types.Message, state: FSMContext):
    key = message.text.strip()
    db = SessionLocal()
    try:
        lic = db.query(LicenseKey).filter_by(key=key).first()
        if not lic:
            await message.answer("❌ Неверный ключ.", reply_markup=get_main_keyboard(message.from_user.id))
            await state.finish()
            return
        if lic.is_used:
            await message.answer("❌ Ключ уже использован.", reply_markup=get_main_keyboard(message.from_user.id))
            await state.finish()
            return

        user = db.query(User).filter_by(user_id=message.from_user.id).first()
        expiry = datetime.utcnow() + timedelta(days=lic.duration_days if lic.duration_days != -1 else 36500)

        user.license_key = key
        user.license_expiry = expiry
        lic.is_used = True
        lic.used_by = user.user_id
        db.commit()

        await message.answer(
            f"✅ <b>Лицензия активирована!</b>\n📅 До: <b>{expiry.strftime('%d.%m.%Y')}</b>",
            reply_markup=get_main_keyboard(message.from_user.id),
        )
        await state.finish()
    finally:
        db.close()


@dp.message_handler(state=UserStates.admin_create_key)
async def process_admin_key(message: types.Message, state: FSMContext):
    try:
        duration = int(message.text)
    except ValueError:
        await message.answer("❌ Введи число.")
        return

    key = generate_license_key()
    db = SessionLocal()
    try:
        db.add(LicenseKey(key=key, duration_days=duration))
        db.commit()
    finally:
        db.close()

    await message.answer(f"✅ Ключ: <code>{key}</code>", reply_markup=get_main_keyboard(message.from_user.id))
    await state.finish()


@dp.message_handler(state=UserStates.admin_broadcast)
async def process_admin_broadcast(message: types.Message, state: FSMContext):
    db = SessionLocal()
    try:
        users = db.query(User).all()
    finally:
        db.close()

    count = 0
    for user in users:
        try:
            await bot.send_message(user.user_id, f"📢 <b>Сообщение:</b>\n\n{message.text}")
            count += 1
        except Exception:
            continue

    await message.answer(f"✅ Отправлено {count} пользователям.", reply_markup=get_main_keyboard(message.from_user.id))
    await state.finish()


# ==== LOGIN FLOW ====
async def _cleanup_login(user_id: int, state: FSMContext):
    client = active_clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    try:
        await state.finish()
    except Exception:
        pass


async def _save_account_and_finish(message: types.Message, state: FSMContext, client: Client, phone: str):
    try:
        session_string = await client.export_session_string()
    except Exception as e:
        logger.exception("export_session_string упал")
        await message.answer(f"❌ Не удалось сохранить сессию: {e}")
        await _cleanup_login(message.from_user.id, state)
        return

    db = SessionLocal()
    try:
        db.add(Account(
            user_id=message.from_user.id,
            phone_number=phone,
            session_string=session_string,  # ОТКРЫТЫМ ТЕКСТОМ
        ))
        db.commit()
    finally:
        db.close()

    await _cleanup_login(message.from_user.id, state)
    await message.answer("✅ <b>Аккаунт добавлен!</b>", reply_markup=get_main_keyboard(message.from_user.id))


@dp.message_handler(state=UserStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с '+'")
        return

    client = Client(
        name="tmp_login",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        device_model="Samsung Galaxy S21",
        system_version="Android 13",
        app_version="10.2.0",
        lang_code="ru",
        system_lang_code="ru-RU",
    )

    try:
        await client.connect()
    except Exception as e:
        logger.exception("Не удалось подключиться к Telegram")
        await message.answer(f"❌ Не удалось подключиться к Telegram: {e}")
        await state.finish()
        return

    try:
        sent = await client.send_code(phone)
    except FloodWait as e:
        logger.warning(f"FloodWait на send_code: {e.value}s")
        await message.answer(
            f"⏳ Telegram ограничил запросы кода.\n"
            f"Подожди <b>{e.value} сек</b> и попробуй снова."
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        await state.finish()
        return
    except Exception as e:
        logger.exception("send_code упал")
        await message.answer(f"❌ Не удалось отправить код: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        await state.finish()
        return

    await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
    active_clients[message.from_user.id] = client

    await message.answer(
        "📨 <b>Код отправлен.</b>\n\n"
        "⚠️ Если у тебя уже открыт Telegram на другом устройстве — "
        "код придёт в чат «Telegram» (служебный), а не по SMS.\n\n"
        "Введи код:",
        reply_markup=get_back_keyboard(),
    )
    await UserStates.waiting_code.set()


@dp.message_handler(state=UserStates.waiting_code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    phone_code_hash = data.get('phone_code_hash')
    client = active_clients.get(message.from_user.id)

    if not client or not phone_code_hash or not phone:
        await message.answer("❌ Сессия истекла. Начни заново: /start → Аккаунты → Добавить")
        await _cleanup_login(message.from_user.id, state)
        return

    try:
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=code,
        )
    except SessionPasswordNeeded:
        await message.answer("🔐 Введи пароль 2FA:", reply_markup=get_back_keyboard())
        await UserStates.waiting_password.set()
        return
    except PhoneCodeInvalid:
        await message.answer("❌ Неверный код. Попробуй ещё раз.")
        return
    except PhoneCodeExpired:
        await message.answer("❌ Код истёк. Начни заново.")
        await _cleanup_login(message.from_user.id, state)
        return
    except FloodWait as e:
        await message.answer(f"⏳ Слишком много попыток. Подожди {e.value} сек.")
        return
    except Exception as e:
        logger.exception("sign_in упал")
        await message.answer(f"❌ Ошибка входа: {e}")
        await _cleanup_login(message.from_user.id, state)
        return

    await _save_account_and_finish(message, state, client, phone)


@dp.message_handler(state=UserStates.waiting_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    client = active_clients.get(message.from_user.id)

    if not client or not phone:
        await message.answer("❌ Сессия истекла.")
        await _cleanup_login(message.from_user.id, state)
        return

    try:
        await client.check_password(password)
    except PasswordHashInvalid:
        await message.answer("❌ Неверный пароль. Попробуй ещё раз.")
        return
    except FloodWait as e:
        await message.answer(f"⏳ Подожди {e.value} сек.")
        return
    except Exception as e:
        logger.exception("check_password упал")
        await message.answer(f"❌ Ошибка: {e}")
        await _cleanup_login(message.from_user.id, state)
        return

    await _save_account_and_finish(message, state, client, phone)


# ==== BROADCAST FLOW ====
@dp.message_handler(state=UserStates.waiting_interval)
async def process_text_normal(message: types.Message, state: FSMContext):
    await state.update_data(messages=[message.text.strip()])
    await message.answer("⏱ Интервал (30-120 минут):", reply_markup=get_back_keyboard())
    await UserStates.waiting_more_messages.set()


@dp.message_handler(state=UserStates.waiting_safe_messages)
async def process_safe_messages(message: types.Message, state: FSMContext):
    data = await state.get_data()
    safe_messages = data.get('safe_messages', [])
    safe_counter = data.get('safe_counter', 0)

    safe_messages.append(message.text.strip())
    safe_counter += 1

    if safe_counter < 3:
        await state.update_data(safe_messages=safe_messages, safe_counter=safe_counter)
        await message.answer(f"📝 Отправь текст №{safe_counter + 1} (из 3):")
    else:
        await state.update_data(messages=safe_messages)
        await message.answer("⏱ Интервал (30-120 минут):", reply_markup=get_back_keyboard())
        await UserStates.waiting_more_messages.set()


@dp.message_handler(state=UserStates.waiting_more_messages)
async def process_final(message: types.Message, state: FSMContext):
    try:
        interval = int(message.text)
    except ValueError:
        await message.answer("❌ Введи число.")
        return

    if interval < 30 or interval > 120:
        await message.answer("❌ Интервал 30-120 минут.")
        return

    data = await state.get_data()
    db = SessionLocal()
    try:
        task = BroadcastTask(
            user_id=message.from_user.id,
            account_ids=json.dumps(data.get('account_ids', [])),
            messages=json.dumps(data.get('messages', [])),
            interval_minutes=interval,
            safe_mode=data.get('safe_mode', False),
            status='active',
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    asyncio.create_task(start_broadcast(message.from_user.id, task_id))

    await message.answer(
        f"✅ <b>Рассылка запущена!</b>\n⏱ Интервал: {interval} мин\n\n"
        f"Используй кнопку ⏹ для остановки",
        reply_markup=get_main_keyboard(message.from_user.id),
    )
    await state.finish()


# ==== STARTUP + ERRORS ====
async def on_startup(dp):
    logger.info("✅ Bot started!")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен!")
    except Exception:
        pass


@dp.errors_handler()
async def errors_handler(update, error):
    logger.exception(f"aiogram error: {error}")
    return True


if __name__ == '__main__':
    logger.info("🚀 Starting bot...")
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
