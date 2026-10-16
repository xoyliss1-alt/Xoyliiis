import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "xoyli").lstrip("@").lower()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "/var/data/woodoogifi.sqlite3")
PORT = int(os.getenv("PORT", "10000"))
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render environment variables.")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("woodoogifi")

def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          referrer_id INTEGER, balance INTEGER NOT NULL DEFAULT 0,
          joined_at TEXT NOT NULL, rewarded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channels(
          chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS withdrawals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          amount INTEGER NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        c.execute("INSERT OR IGNORE INTO settings VALUES('referral_reward','3')")
        c.commit()

def setting(key, default=""):
    with closing(db()) as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def is_admin(user):
    return bool(user and (user.id in ADMIN_IDS or (user.username and user.username.lower() == ADMIN_USERNAME)))

class AdminStates(StatesGroup):
    reward = State()
    channel_id = State()
    channel_link = State()
    channel_name = State()
    user_id = State()
    balance_value = State()

def admin_inline_keyboard():
    return inline([
      [InlineKeyboardButton(text="🎁 Награда за реферала", callback_data="adm_reward")],
      [InlineKeyboardButton(text="📢 Каналы", callback_data="adm_channels")],
      [InlineKeyboardButton(text="💰 Изменить баланс", callback_data="adm_balance")],
      [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
      [InlineKeyboardButton(text="📥 Заявки на вывод", callback_data="adm_withdrawals")],
    ])

def admin_back():
    return inline([[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]])

def main_keyboard(admin=False):
    rows = [
      [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="⭐ Баланс")],
      [KeyboardButton(text="🎁 Заработать звёзды"), KeyboardButton(text="📥 Вывести звёзды")],
      [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="ℹ️ О проекте")]
    ]
    if admin:
        rows.append([KeyboardButton(text="🛡 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def channels_keyboard(channels):
    rows = [[InlineKeyboardButton(text=f"📢 {r['title']}", url=r['url'])] for r in channels]
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")])
    return inline(rows)

def get_channels():
    with closing(db()) as c:
        return c.execute("SELECT chat_id,title,url FROM channels ORDER BY title").fetchall()

async def subscribed(bot, user_id):
    for ch in get_channels():
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            log.exception("Subscription check failed for channel %s", ch["chat_id"])
            return False
    return True

def upsert_user(user):
    with closing(db()) as c:
        c.execute("""INSERT INTO users(user_id,username,first_name,joined_at)
          VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, first_name=excluded.first_name""",
          (user.id, user.username or "", user.first_name or "", datetime.now(timezone.utc).isoformat()))
        c.commit()

async def start_flow(message: Message, bot: Bot, ref_id=None):
    user = message.from_user
    upsert_user(user)
    if ref_id and ref_id != user.id:
        with closing(db()) as c:
            row = c.execute("SELECT referrer_id FROM users WHERE user_id=?", (user.id,)).fetchone()
            if row and row["referrer_id"] is None:
                c.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (ref_id, user.id))
                c.commit()
    channels = get_channels()
    if channels and not await subscribed(bot, user.id):
        await message.answer(
          "⭐ Добро пожаловать в WoodooGifi! ⭐\n\nЧтобы пользоваться ботом и засчитать приглашение, подпишись на все обязательные каналы, затем нажми «Проверить подписку».",
          reply_markup=channels_keyboard(channels))
        return
    await reward_referrer(user.id, bot)
    await message.answer(
      f"⭐ Добро пожаловать в WoodooGifi! ⭐\n\n👋 Привет, {user.first_name or 'друг'}!\n🚀 Получай звёзды за приглашение людей! WoodooGifi кормит!\n\n✨ Зарабатывай Telegram Stars, собирай команду друзей и отправляй заявку на вывод.\n\n👇 Выбери нужный раздел в меню:",
      reply_markup=main_keyboard(is_admin(user)))

async def reward_referrer(joined_id, bot):
    with closing(db()) as c:
        row = c.execute("SELECT referrer_id,rewarded FROM users WHERE user_id=?", (joined_id,)).fetchone()
        if not row or not row["referrer_id"] or row["rewarded"]:
            return
        referrer = row["referrer_id"]
        reward = max(0, int(setting("referral_reward", "3")))
        # Mark and credit atomically so repeated checks cannot pay twice.
        c.execute("UPDATE users SET rewarded=1 WHERE user_id=? AND rewarded=0", (joined_id,))
        if c.total_changes == 0:
            c.rollback()
            return
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (reward, referrer))
        c.execute("INSERT INTO ledger(user_id,delta,reason,created_at) VALUES(?,?,?,?)",
                  (referrer, reward, f"Реферал {joined_id}", datetime.now(timezone.utc).isoformat()))
        c.commit()
    try:
        await bot.send_message(referrer, f"🎉 Новый реферал!\nПользователь прошёл проверку подписки.\nВам начислено +{reward} ⭐ звёзд!")
    except Exception:
        log.info("Could not notify referrer %s", referrer)

async def stats_text(user_id):
    with closing(db()) as c:
        u = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        invited = c.execute("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (user_id,)).fetchone()["n"]
        rewarded = c.execute("SELECT COUNT(*) n FROM users WHERE referrer_id=? AND rewarded=1", (user_id,)).fetchone()["n"]
    return u["balance"] if u else 0, invited, rewarded

async def admin_panel(message_or_call, user):
    text = ("🛡 Админ-панель WoodooGifi\n\n"
            "Награда за реферала и каналы настраиваются командами ниже.\n"
            "Команды администратора:\n"
            "/stats — общая статистика\n"
            "/setreward 3 — награда за реферала\n"
            "/addchannel @channel https://t.me/channel — добавить канал\n"
            "/delchannel @channel — удалить канал\n"
            "/channels — список каналов\n"
            "/credit USER_ID AMOUNT — изменить баланс\n"
            "/withdrawals — заявки на вывод\n"
            "/approve ID или /reject ID — обработать заявку\n\n"
            "Бот должен быть администратором обязательных каналов, чтобы проверка подписки работала надёжно.")
    if isinstance(message_or_call, Message):
        await message_or_call.answer(text, reply_markup=admin_inline_keyboard())
    else:
        await message_or_call.message.answer(text, reply_markup=admin_inline_keyboard())

async def on_start(message: Message, bot: Bot):
    arg = (message.text or "").partition(" ")[2].strip()
    ref_id = None
    if arg.startswith("ref_"):
        try: ref_id = int(arg[4:])
        except ValueError: pass
    await start_flow(message, bot, ref_id)

async def check_sub(call: CallbackQuery, bot: Bot):
    if not await subscribed(bot, call.from_user.id):
        await call.answer("Пока не вижу подписку на все каналы.", show_alert=True)
        return
    await reward_referrer(call.from_user.id, bot)
    await call.message.answer("✅ Подписка подтверждена! Теперь всё готово.", reply_markup=main_keyboard(is_admin(call.from_user)))
    await call.answer("Подписка подтверждена!")

async def handle_menu(message: Message, bot: Bot):
    u = message.from_user
    t = message.text or ""
    upsert_user(u)
    if t == "👤 Мой профиль":
        bal, invited, rewarded = await stats_text(u.id)
        await message.answer(f"👤 Профиль\nID: <code>{u.id}</code>\nИмя: {u.first_name}\nПриглашено: {invited}\nПодписку прошли: {rewarded}\nБаланс: ⭐ {bal}")
    elif t == "⭐ Баланс":
        bal, _, _ = await stats_text(u.id)
        await message.answer(f"⭐ Твой баланс: <b>{bal}</b> звёзд")
    elif t == "🎁 Заработать звёзды":
        me = await bot.get_me()
        bal, invited, rewarded = await stats_text(u.id)
        await message.answer(f"🎁 Приглашай друзей по ссылке:\nhttps://t.me/{me.username}?start=ref_{u.id}\n\nЗа каждого приглашённого, который подпишется на все обязательные каналы, начисляется ⭐ {setting('referral_reward','3')}\n\nВсего приглашено: {invited}\nПодписку прошли: {rewarded}")
    elif t == "📥 Вывести звёзды":
        bal, _, _ = await stats_text(u.id)
        await message.answer(f"📥 Баланс: ⭐ {bal}\n\nЧтобы создать заявку, отправь:\n/withdraw КОЛИЧЕСТВО реквизиты\nНапример: /withdraw 10 @username\nЗаявка будет проверена администратором. Это не автоматический перевод Telegram Stars.")
    elif t == "📊 Статистика":
        bal, invited, rewarded = await stats_text(u.id)
        await message.answer(f"📊 Твоя статистика\nПриглашено: {invited}\nПодписку прошли: {rewarded}\nНачислено звёзд на балансе: {bal}")
    elif t == "ℹ️ О проекте":
        await message.answer("ℹ️ WoodooGifi — реферальный проект. Звёзды в боте отображаются как внутренний баланс. Вывод проходит через заявку и подтверждение администратора.")
    elif t == "🛡 Админ-панель" and is_admin(u):
        await admin_panel(message, u)

async def withdraw(message: Message):
    try:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3: raise ValueError()
        amount, details = int(parts[1]), parts[2].strip()
        if amount <= 0 or not details: raise ValueError()
    except ValueError:
        await message.answer("Формат: /withdraw КОЛИЧЕСТВО реквизиты\nМинимальный вывод: 100 ⭐\nНапример: /withdraw 100 @username")
        return
    with closing(db()) as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
        if not row or row["balance"] < amount:
            await message.answer("❌ Недостаточно звёзд на балансе.")
            return
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, message.from_user.id))
        cur = c.execute("INSERT INTO withdrawals(user_id,amount,details,created_at) VALUES(?,?,?,?)",
            (message.from_user.id, amount, details, datetime.now(timezone.utc).isoformat()))
        wid = cur.lastrowid
        c.execute("INSERT INTO ledger(user_id,delta,reason,created_at) VALUES(?,?,?,?)",
            (message.from_user.id, -amount, f"Заявка на вывод #{wid}", datetime.now(timezone.utc).isoformat()))
        c.commit()
    await message.answer(f"✅ Заявка #{wid} создана. Администратор рассмотрит её.")
    # Notifications are sent to configured numeric admin IDs.
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, f"📥 Заявка #{wid}\nПользователь: {message.from_user.id}\nСумма: {amount} ⭐\nРеквизиты: {details}\n/approve {wid} или /reject {wid}")
        except Exception:
            log.exception("Could not notify admin %s", admin_id)

async def admin_command(message: Message):
    if not is_admin(message.from_user):
        await message.answer("⛔ Эта команда доступна только администратору @"+ADMIN_USERNAME)
        return False
    return True

async def cmd_stats(message: Message):
    if not await admin_command(message): return
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        rewarded = c.execute("SELECT COUNT(*) n FROM users WHERE rewarded=1").fetchone()["n"]
        balance = c.execute("SELECT COALESCE(SUM(balance),0) n FROM users").fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'").fetchone()["n"]
    await message.answer(f"📊 Всего пользователей: {users}\nРефералов с наградой: {rewarded}\nСумма балансов: {balance} ⭐\nЗаявок ожидает: {pending}")

async def setreward(message: Message):
    if not await admin_command(message): return
    try: amount = int(message.text.split(maxsplit=1)[1])
    except (ValueError, IndexError):
        await message.answer("Использование: /setreward 3"); return
    if not 0 <= amount <= 100000:
        await message.answer("Награда должна быть от 0 до 100000."); return
    with closing(db()) as c:
        c.execute("INSERT INTO settings(key,value) VALUES('referral_reward',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(amount),)); c.commit()
    await message.answer(f"✅ Награда теперь {amount} ⭐")

async def addchannel(message: Message):
    if not await admin_command(message): return
    try:
        _, chat_id, url = message.text.split(maxsplit=2)
        if not url.startswith("https://t.me/"): raise ValueError()
        title = chat_id.lstrip("@")
        with closing(db()) as c:
            c.execute("INSERT OR REPLACE INTO channels(chat_id,title,url) VALUES(?,?,?)", (chat_id,title,url)); c.commit()
        await message.answer(f"✅ Канал {title} добавлен. Убедись, что бот администратор канала.")
    except ValueError:
        await message.answer("Использование: /addchannel @channel https://t.me/channel")

async def delchannel(message: Message):
    if not await admin_command(message): return
    try: channel = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("Использование: /delchannel @channel"); return
    with closing(db()) as c:
        cur = c.execute("DELETE FROM channels WHERE chat_id=?", (channel,))
        if cur.rowcount == 0:
            cur = c.execute("DELETE FROM channels WHERE title=?", (channel.lstrip("@"),))
        c.commit()
    await message.answer("✅ Канал удалён." if cur.rowcount else "Канал не найден.")

async def channels_cmd(message: Message):
    if not await admin_command(message): return
    channels = get_channels()
    await message.answer("Обязательные каналы:\n" + ("\n".join(f"• {x['title']} ({x['chat_id']}) — {x['url']}" for x in channels) if channels else "Пока нет каналов."))

async def credit(message: Message):
    if not await admin_command(message): return
    try:
        _, uid, amount = message.text.split(maxsplit=2); uid, amount = int(uid), int(amount)
    except ValueError:
        await message.answer("Использование: /credit USER_ID AMOUNT"); return
    with closing(db()) as c:
        row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await message.answer("Пользователь ещё не запускал бота."); return
        c.execute("UPDATE users SET balance=MAX(0,balance+?) WHERE user_id=?", (amount,uid))
        c.execute("INSERT INTO ledger(user_id,delta,reason,created_at) VALUES(?,?,?,?)",(uid,amount,"Изменение администратором",datetime.now(timezone.utc).isoformat())); c.commit()
    await message.answer("✅ Баланс обновлён.")

async def withdrawals(message: Message):
    if not await admin_command(message): return
    with closing(db()) as c:
        rows = c.execute("SELECT id,user_id,amount,details,status FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 30").fetchall()
    await message.answer("\n\n".join(f"#{r['id']} | user {r['user_id']} | {r['amount']} ⭐ | {r['details']}\n/approve {r['id']} или /reject {r['id']}" for r in rows) if rows else "Ожидающих заявок нет.")

async def process_withdrawal(message: Message, approve: bool):
    if not await admin_command(message): return
    try: wid = int(message.text.split(maxsplit=1)[1])
    except (ValueError, IndexError):
        await message.answer("Укажи ID заявки."); return
    with closing(db()) as c:
        row = c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if not row:
            await message.answer("Заявка не найдена или уже обработана."); return
        status = "approved" if approve else "rejected"
        c.execute("UPDATE withdrawals SET status=? WHERE id=?", (status,wid))
        if not approve:
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (row["amount"],row["user_id"]))
            c.execute("INSERT INTO ledger(user_id,delta,reason,created_at) VALUES(?,?,?,?)",(row["user_id"],row["amount"],f"Возврат заявки #{wid}",datetime.now(timezone.utc).isoformat()))
        c.commit()
    await message.answer(f"✅ Заявка #{wid}: {status}")
    try: await message.bot.send_message(row["user_id"], f"Заявка на вывод #{wid}: {'одобрена администратором' if approve else 'отклонена, сумма возвращена на баланс'}.")
    except Exception: pass


async def cb_admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    await call.message.edit_text("🛡 Админ-панель WoodooGifi\nВыберите действие:", reply_markup=admin_inline_keyboard())
    await call.answer()

async def cb_admin_reward(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    await state.set_state(AdminStates.reward)
    await call.message.edit_text("🎁 Награда за реферала сейчас: " + setting("referral_reward", "3") + " ⭐\nОтправьте новую сумму от 0 до 100000:", reply_markup=admin_back())
    await call.answer()

async def process_admin_reward(message: Message, state: FSMContext):
    if not is_admin(message.from_user): await state.clear(); return
    try: amount = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введите целое число от 0 до 100000."); return
    if not 0 <= amount <= 100000:
        await message.answer("❌ Значение должно быть от 0 до 100000."); return
    with closing(db()) as c:
        c.execute("INSERT INTO settings(key,value) VALUES('referral_reward',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(amount),)); c.commit()
    await state.clear()
    await message.answer("✅ Награда изменена: " + str(amount) + " ⭐", reply_markup=main_keyboard(True))

async def cb_admin_channels(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    channels = get_channels()
    lines = ["📢 Обязательные каналы:"]
    lines += [str(x["title"]) + " (" + str(x["chat_id"]) + ") — " + str(x["url"]) for x in channels]
    if not channels: lines.append("Список пуст.")
    rows = [[InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")]]
    rows += [[InlineKeyboardButton(text="❌ " + str(x["title"])[:40], callback_data="adm_delch:" + str(x["chat_id"]))] for x in channels]
    rows += [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]
    await call.message.edit_text("\n".join(lines), reply_markup=inline(rows), disable_web_page_preview=True)
    await call.answer()

async def cb_admin_add_channel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    await state.set_state(AdminStates.channel_id)
    await call.message.edit_text("Отправьте ID канала (например, -1001234567890) или @username:", reply_markup=admin_back())
    await call.answer()

async def process_admin_channel_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user): await state.clear(); return
    value = (message.text or "").strip()
    if not value or len(value) > 150:
        await message.answer("❌ Некорректный ID."); return
    await state.update_data(ch_id=value)
    await state.set_state(AdminStates.channel_link)
    await message.answer("Отправьте ссылку канала https://t.me/...")

async def process_admin_channel_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user): await state.clear(); return
    value = (message.text or "").strip()
    if not value.startswith("https://t.me/") or len(value) > 300:
        await message.answer("❌ Ссылка должна начинаться с https://t.me/"); return
    await state.update_data(ch_link=value)
    await state.set_state(AdminStates.channel_name)
    await message.answer("Отправьте название канала:")

async def process_admin_channel_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user): await state.clear(); return
    title = (message.text or "").strip()
    if not title or len(title) > 100:
        await message.answer("❌ Название должно быть от 1 до 100 символов."); return
    data = await state.get_data()
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO channels(chat_id,title,url) VALUES(?,?,?)", (data["ch_id"], title, data["ch_link"])); c.commit()
    await state.clear()
    await message.answer("✅ Канал добавлен. Бот должен быть администратором канала.", reply_markup=main_keyboard(True))

async def cb_admin_delete_channel(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    ch_id = call.data.split(":", 1)[1]
    with closing(db()) as c:
        cur = c.execute("DELETE FROM channels WHERE chat_id=?", (ch_id,)); c.commit()
    await call.answer("Канал удалён." if cur.rowcount else "Канал не найден.", show_alert=True)
    channels = get_channels()
    lines = ["📢 Обязательные каналы:"] + [str(x["title"]) + " (" + str(x["chat_id"]) + ")" for x in channels]
    if not channels: lines.append("Список пуст.")
    rows = [[InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")]]
    rows += [[InlineKeyboardButton(text="❌ " + str(x["title"])[:40], callback_data="adm_delch:" + str(x["chat_id"]))] for x in channels]
    rows += [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]
    await call.message.edit_text("\n".join(lines), reply_markup=inline(rows))

async def cb_admin_balance(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    await state.set_state(AdminStates.user_id)
    await call.message.edit_text("💰 Отправьте Telegram ID пользователя:", reply_markup=admin_back())
    await call.answer()

async def process_admin_user_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user): await state.clear(); return
    try: uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом."); return
    with closing(db()) as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await message.answer("❌ Пользователь не найден. Он должен сначала запустить бота."); return
    await state.update_data(target_id=uid)
    await state.set_state(AdminStates.balance_value)
    await message.answer("Баланс: " + str(row["balance"]) + " ⭐\nВведите новое значение (100) или изменение (+10 / -5):")

async def process_admin_balance_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user): await state.clear(); return
    raw = (message.text or "").strip()
    try: value = int(raw)
    except ValueError:
        await message.answer("❌ Введите целое число."); return
    data = await state.get_data()
    uid = data["target_id"]
    with closing(db()) as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await message.answer("❌ Пользователь не найден."); await state.clear(); return
        new_balance = max(0, value if not raw.startswith(("+", "-")) else row["balance"] + value)
        delta = new_balance - row["balance"]
        c.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, uid))
        c.execute("INSERT INTO ledger(user_id,delta,reason,created_at) VALUES(?,?,?,?)", (uid, delta, "Изменение администратором", datetime.now(timezone.utc).isoformat())); c.commit()
    await state.clear()
    await message.answer("✅ Новый баланс: " + str(new_balance) + " ⭐", reply_markup=main_keyboard(True))

async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        rewards = c.execute("SELECT COUNT(*) n FROM users WHERE rewarded=1").fetchone()["n"]
        total = c.execute("SELECT COALESCE(SUM(balance),0) n FROM users").fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'").fetchone()["n"]
    await call.message.edit_text("📊 Статистика\nПользователей: " + str(users) + "\nРефералов с наградой: " + str(rewards) + "\nБаланс пользователей: " + str(total) + " ⭐\nОжидающих заявок: " + str(pending), reply_markup=admin_back())
    await call.answer()

async def cb_admin_withdrawals(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True); return
    with closing(db()) as c:
        rows = c.execute("SELECT id,user_id,amount FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 15").fetchall()
    text = "📥 Заявки на вывод\n\n"
    text += "\n".join("#" + str(x["id"]) + " · пользователь " + str(x["user_id"]) + " · " + str(x["amount"]) + " ⭐\n/approve " + str(x["id"]) + " или /reject " + str(x["id"]) for x in rows) if rows else "Нет ожидающих заявок."
    await call.message.edit_text(text, reply_markup=admin_back())
    await call.answer()

async def health(request):
    return web.Response(text="WoodooGifi is alive")

async def main():
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.register(on_start, CommandStart())
    dp.callback_query.register(check_sub, F.data == "check_sub")
    dp.callback_query.register(cb_admin_panel, F.data == "admin_panel")
    dp.callback_query.register(cb_admin_reward, F.data == "adm_reward")
    dp.callback_query.register(cb_admin_channels, F.data == "adm_channels")
    dp.callback_query.register(cb_admin_add_channel, F.data == "adm_add_channel")
    dp.callback_query.register(cb_admin_delete_channel, F.data.startswith("adm_delch:"))
    dp.callback_query.register(cb_admin_balance, F.data == "adm_balance")
    dp.callback_query.register(cb_admin_stats, F.data == "adm_stats")
    dp.callback_query.register(cb_admin_withdrawals, F.data == "adm_withdrawals")
    dp.message.register(process_admin_reward, AdminStates.reward)
    dp.message.register(process_admin_channel_id, AdminStates.channel_id)
    dp.message.register(process_admin_channel_link, AdminStates.channel_link)
    dp.message.register(process_admin_channel_name, AdminStates.channel_name)
    dp.message.register(process_admin_user_id, AdminStates.user_id)
    dp.message.register(process_admin_balance_value, AdminStates.balance_value)
    dp.message.register(withdraw, Command("withdraw"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(setreward, Command("setreward"))
    dp.message.register(addchannel, Command("addchannel"))
    dp.message.register(delchannel, Command("delchannel"))
    dp.message.register(channels_cmd, Command("channels"))
    dp.message.register(credit, Command("credit"))
    dp.message.register(withdrawals, Command("withdrawals"))
    dp.message.register(lambda m: process_withdrawal(m, True), Command("approve"))
    dp.message.register(lambda m: process_withdrawal(m, False), Command("reject"))
    dp.message.register(handle_menu, F.text)
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health server listening on %s", PORT)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
