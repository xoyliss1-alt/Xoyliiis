import asyncio
import html
import logging
import os
from decimal import Decimal, InvalidOperation

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("woodoogifi")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).split(",")
    if value.strip().isdigit()
}
PORT = int(os.getenv("PORT", "10000"))
REFERRAL_REWARD_DEFAULT = max(0, int(os.getenv("REFERRAL_REWARD", "3")))
MIN_REFERRALS_DEFAULT = max(0, int(os.getenv("MIN_REFERRALS", "5")))

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the environment.")
if not DATABASE_URL:
    raise RuntimeError("Set DATABASE_URL to your PostgreSQL connection string.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool: asyncpg.Pool | None = None


class Form(StatesGroup):
    withdraw_destination = State()
    admin_balance_user = State()
    admin_balance_value = State()
    admin_reward_value = State()
    admin_add_channel = State()
    admin_remove_channel = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="⭐ Баланс")],
        [KeyboardButton(text="🎁 Заработать звезды"), KeyboardButton(text="📤 Вывести звезды")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="ℹ️ О проекте")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Изменить баланс", callback_data="adm:balance")],
        [InlineKeyboardButton(text="🎁 Награда за реферала", callback_data="adm:reward")],
        [InlineKeyboardButton(text="📢 Обязательные каналы", callback_data="adm:channels")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])


def channel_keyboard(channels: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    rows = []
    for channel in channels:
        rows.append([InlineKeyboardButton(
            text=f"📢 {channel['title']}",
            url=channel["url"],
        )])
    rows.append([InlineKeyboardButton(text="🔄 Проверить подписки", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_admin_keyboard(channels: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm:channel_add")],
        [InlineKeyboardButton(text="➖ Удалить канал", callback_data="adm:channel_remove")],
    ]
    for channel in channels:
        rows.append([InlineKeyboardButton(
            text=f"❌ {channel['title']} ({channel['chat_id']})",
            callback_data=f"adm:delchan:{channel['chat_id']}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def db() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool is not initialized")
    return pool


async def init_db() -> None:
    global pool
    # Render commonly provides postgres://; asyncpg expects postgresql://.
    dsn = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
                referrer_id BIGINT REFERENCES users(user_id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS referrals (
                invited_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                referrer_id BIGINT NOT NULL REFERENCES users(user_id),
                rewarded BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                rewarded_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS channels (
                chat_id BIGINT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS withdrawals (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                amount BIGINT NOT NULL CHECK (amount > 0),
                destination TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                decided_at TIMESTAMPTZ
            );
        """)
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES('referral_reward', $1) ON CONFLICT(key) DO NOTHING",
            str(REFERRAL_REWARD_DEFAULT),
        )
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES('min_referrals', $1) ON CONFLICT(key) DO NOTHING",
            str(MIN_REFERRALS_DEFAULT),
        )


async def get_setting(key: str, default: str) -> str:
    async with (await db()).acquire() as conn:
        value = await conn.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return value if value is not None else default


async def set_setting(key: str, value: str) -> None:
    async with (await db()).acquire() as conn:
        await conn.execute(
            "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            key, value,
        )


async def get_channels() -> list[asyncpg.Record]:
    async with (await db()).acquire() as conn:
        return await conn.fetch("SELECT chat_id,url,title FROM channels ORDER BY title")


async def ensure_user(user: types.User) -> None:
    async with (await db()).acquire() as conn:
        await conn.execute("""
            INSERT INTO users(user_id,username,first_name)
            VALUES($1,$2,$3)
            ON CONFLICT(user_id) DO UPDATE SET
                username=EXCLUDED.username, first_name=EXCLUDED.first_name
        """, user.id, user.username, user.first_name or "")


async def user_stats(user_id: int) -> asyncpg.Record | None:
    async with (await db()).acquire() as conn:
        return await conn.fetchrow("""
            SELECT u.user_id,u.username,u.first_name,u.balance,
                   (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.user_id) AS invited,
                   (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.user_id AND r.rewarded=TRUE) AS rewarded
            FROM users u WHERE u.user_id=$1
        """, user_id)


async def check_subscriptions(user_id: int) -> tuple[bool, list[asyncpg.Record]]:
    channels = await get_channels()
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel["chat_id"], user_id=user_id)
            if member.status in ("left", "kicked", "restricted"):
                missing.append(channel)
        except Exception:
            log.exception("Subscription check failed for channel %s", channel["chat_id"])
            # Fail closed: if the bot cannot verify membership, don't grant access.
            missing.append(channel)
    return not missing, missing


async def home_or_subscription(message: types.Message, user_id: int) -> None:
    ok, missing = await check_subscriptions(user_id)
    if not ok:
        await message.answer(
            "🔒 Чтобы пользоваться WoodooGifi, подпишитесь на все каналы ниже, "
            "затем нажмите «Проверить подписки».",
            reply_markup=channel_keyboard(missing),
        )
        return
    # If no channels are configured, or the user is already subscribed, reward a
    # pending referral immediately. The database flag prevents duplicate rewards.
    await reward_referral_if_ready(user_id)
    await message.answer("🌟 Добро пожаловать в WoodooGifi!", reply_markup=main_keyboard(user_id))


async def reward_referral_if_ready(invited_id: int) -> bool:
    ok, _ = await check_subscriptions(invited_id)
    if not ok:
        return False
    async with (await db()).acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT referrer_id,rewarded FROM referrals WHERE invited_id=$1 FOR UPDATE",
                invited_id,
            )
            if not row or row["rewarded"]:
                return False
            reward = int(await conn.fetchval(
                "SELECT value FROM settings WHERE key='referral_reward'"
            ) or REFERRAL_REWARD_DEFAULT)
            await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", reward, row["referrer_id"])
            await conn.execute(
                "UPDATE referrals SET rewarded=TRUE,rewarded_at=NOW() WHERE invited_id=$1",
                invited_id,
            )
            referrer_id = row["referrer_id"]
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 Ваш друг выполнил условия! Начислено <b>+{reward} ⭐</b>.",
            parse_mode="HTML",
        )
    except Exception:
        log.info("Could not notify referrer %s", referrer_id)
    return True


@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    if not message.from_user or message.from_user.is_bot:
        return
    user = message.from_user
    async with (await db()).acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval("SELECT user_id FROM users WHERE user_id=$1", user.id)
            await conn.execute("""
                INSERT INTO users(user_id,username,first_name)
                VALUES($1,$2,$3)
                ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name
            """, user.id, user.username, user.first_name or "")
            parts = (message.text or "").split(maxsplit=1)
            if not existing and len(parts) == 2 and parts[1].strip().isdigit():
                referrer_id = int(parts[1].strip())
                if referrer_id != user.id:
                    ref_exists = await conn.fetchval("SELECT user_id FROM users WHERE user_id=$1", referrer_id)
                    if ref_exists:
                        await conn.execute(
                            "INSERT INTO referrals(invited_id,referrer_id) VALUES($1,$2) ON CONFLICT(invited_id) DO NOTHING",
                            user.id, referrer_id,
                        )
    await home_or_subscription(message, user.id)


@dp.callback_query(F.data == "check_subs")
async def check_subs(callback: types.CallbackQuery):
    if not callback.from_user or callback.from_user.is_bot:
        await callback.answer()
        return
    ok, missing = await check_subscriptions(callback.from_user.id)
    if not ok:
        await callback.answer("Подпишитесь на все каналы и попробуйте снова.", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=channel_keyboard(missing))
        return
    await reward_referral_if_ready(callback.from_user.id)
    if callback.message:
        await callback.message.answer("✅ Подписки подтверждены!", reply_markup=main_keyboard(callback.from_user.id))
    await callback.answer("Подписки проверены!")


@dp.message(F.text == "👤 Мой профиль")
async def profile(message: types.Message):
    await ensure_user(message.from_user)
    user = await user_stats(message.from_user.id)
    name = html.escape(user["first_name"] or "Пользователь")
    username = f"@{html.escape(user['username'])}" if user["username"] else "не указан"
    await message.answer(
        f"👤 <b>Ваш профиль</b>\n\n🆔 ID: <code>{user['user_id']}</code>\n"
        f"Имя: {name}\nUsername: {username}\n⭐ Баланс: <b>{user['balance']} ⭐</b>\n"
        f"👥 Приглашено: <b>{user['invited']}</b>",
        parse_mode="HTML",
    )


@dp.message(F.text == "⭐ Баланс")
async def balance(message: types.Message):
    await ensure_user(message.from_user)
    user = await user_stats(message.from_user.id)
    await message.answer(f"⭐ Ваш баланс: <b>{user['balance']} Stars</b>\nПриглашайте друзей, чтобы увеличить баланс.", parse_mode="HTML")


@dp.message(F.text == "🎁 Заработать звезды")
async def earn(message: types.Message):
    await ensure_user(message.from_user)
    user = await user_stats(message.from_user.id)
    me = await bot.get_me()
    reward = await get_setting("referral_reward", str(REFERRAL_REWARD_DEFAULT))
    link = f"https://t.me/{me.username}?start={message.from_user.id}"
    share_text = f"🌟 Залетай в WoodooGifi и зарабатывай Stars вместе со мной! {link}"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Пригласить друга в 1 клик", url=f"https://t.me/share/url?url={link}&text=Залетай%20в%20WoodooGifi%20и%20зарабатывай%20Stars%20вместе%20со%20мной%21")],
        [InlineKeyboardButton(text="📋 Показать ссылку", callback_data="show_ref_link")],
    ])
    await message.answer(
        f"🎁 <b>Зарабатывай Stars с WoodooGifi!</b>\n\n"
        f"За каждого приглашённого друга, который выполнит условия подписки, начисляется <b>+{reward} ⭐</b>.\n"
        f"👥 Приглашено: <b>{user['invited']}</b>\n⭐ Начислено за друзей: <b>{user['rewarded']}</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>",
        reply_markup=markup, parse_mode="HTML",
    )


@dp.callback_query(F.data == "show_ref_link")
async def show_ref_link(callback: types.CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    if callback.message:
        await callback.message.answer(f"🔗 Ваша реферальная ссылка:\n<code>{link}</code>", parse_mode="HTML")
    await callback.answer()


@dp.message(F.text == "📤 Вывести звезды")
async def withdraw_start(message: types.Message, state: FSMContext):
    await ensure_user(message.from_user)
    user = await user_stats(message.from_user.id)
    minimum = int(await get_setting("min_referrals", str(MIN_REFERRALS_DEFAULT)))
    if user["rewarded"] < minimum:
        await message.answer(f"🔒 Для вывода нужно минимум {minimum} друзей, выполнивших условия подписки.\nОсталось: {minimum - user['rewarded']}.")
        return
    if user["balance"] <= 0:
        await message.answer("⭐ На балансе пока нет Stars для вывода.")
        return
    await state.set_state(Form.withdraw_destination)
    await message.answer(
        f"Доступно к выводу: <b>{user['balance']} ⭐</b>\n"
        "Отправьте свой Telegram @username или числовой Telegram ID для заявки.\n"
        "Сумма будет зарезервирована до решения администратора.",
        parse_mode="HTML",
    )


@dp.message(Form.withdraw_destination)
async def withdraw_submit(message: types.Message, state: FSMContext):
    destination = (message.text or "").strip()
    if not destination or len(destination) > 100 or not (
        destination.startswith("@") or destination.isdigit()
    ):
        await message.answer("Введите @username или числовой Telegram ID.")
        return
    user_id = message.from_user.id
    async with (await db()).acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            if not user or user["balance"] <= 0:
                await state.clear()
                await message.answer("Нет доступного баланса для вывода.")
                return
            amount = user["balance"]
            withdrawal_id = await conn.fetchval(
                "INSERT INTO withdrawals(user_id,amount,destination) VALUES($1,$2,$3) RETURNING id",
                user_id, amount, destination,
            )
            await conn.execute("UPDATE users SET balance=0 WHERE user_id=$1", user_id)
    await state.clear()
    await message.answer(f"✅ Заявка №{withdrawal_id} создана на <b>{amount} ⭐</b>. Ожидайте решения администратора.", parse_mode="HTML")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"wd:approve:{withdrawal_id}"),
        InlineKeyboardButton(text="↩️ Отклонить и вернуть", callback_data=f"wd:reject:{withdrawal_id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📤 <b>Новая заявка на вывод №{withdrawal_id}</b>\n"
                f"Пользователь: <code>{user_id}</code>\nСумма: <b>{amount} ⭐</b>\nПолучатель: {html.escape(destination)}",
                parse_mode="HTML", reply_markup=markup,
            )
        except Exception:
            log.exception("Unable to notify admin %s", admin_id)


@dp.callback_query(F.data.startswith("wd:"))
async def decide_withdrawal(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        withdrawal_id = int(raw_id)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная заявка.", show_alert=True)
        return
    async with (await db()).acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE", withdrawal_id)
            if not row or row["status"] != "pending":
                await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
                return
            if action == "reject":
                await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", row["amount"], row["user_id"])
                status = "rejected"
            elif action == "approve":
                status = "approved"
            else:
                await callback.answer("Неизвестное действие.", show_alert=True)
                return
            await conn.execute("UPDATE withdrawals SET status=$1,decided_at=NOW() WHERE id=$2", status, withdrawal_id)
    label = "одобрена" if status == "approved" else "отклонена, баланс возвращён"
    try:
        await bot.send_message(row["user_id"], f"📤 Заявка №{withdrawal_id} {label}.")
    except Exception:
        log.exception("Could not notify withdrawal owner")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Заявка №{withdrawal_id}: {label}.")
    await callback.answer("Готово.")


@dp.message(F.text == "📊 Статистика")
async def statistics(message: types.Message):
    async with (await db()).acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
    await message.answer(f"📊 <b>WoodooGifi</b>\nПользователей: <b>{count}</b>\nСтатус: работает", parse_mode="HTML")


@dp.message(F.text == "ℹ️ О проекте")
async def about(message: types.Message):
    await message.answer(
        "⭐ <b>WoodooGifi</b> — реферальный проект с внутренним балансом наград.\n"
        "Приглашайте друзей, выполняйте условия подписки и отправляйте заявки на вывод.\n\n"
        "Важно: баланс в боте — учёт наград. Одобрение заявки само по себе не переводит реальные Telegram Stars; фактическую выплату выполняет администратор.",
        parse_mode="HTML",
    )


@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: types.Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("⚙️ <b>Админ-панель WoodooGifi</b>", reply_markup=admin_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "adm:home")
async def admin_home(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("⚙️ Админ-панель", reply_markup=admin_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "menu")
async def back_menu(callback: types.CallbackQuery):
    if callback.message:
        await callback.message.answer("Главное меню:", reply_markup=main_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "adm:balance")
async def admin_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(Form.admin_balance_user)
    await callback.message.answer("Введите Telegram ID пользователя:")
    await callback.answer()


@dp.message(Form.admin_balance_user)
async def admin_balance_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if not (message.text or "").isdigit():
        await message.answer("Введите числовой Telegram ID.")
        return
    target = int(message.text)
    exists = await (await db()).fetchval("SELECT user_id FROM users WHERE user_id=$1", target)
    if not exists:
        await message.answer("Пользователь не найден.")
        return
    await state.update_data(target_id=target)
    await state.set_state(Form.admin_balance_value)
    await message.answer("Введите баланс: число для установки, +10 для прибавления или -5 для вычитания.")


@dp.message(Form.admin_balance_value)
async def admin_balance_value(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = Decimal(raw)
        if not value.is_finite() or value != value.to_integral_value():
            raise InvalidOperation
        amount = int(value)
    except (InvalidOperation, ValueError):
        await message.answer("Введите целое число, например 25, +10 или -5.")
        return
    data = await state.get_data()
    target = data["target_id"]
    async with (await db()).acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", target)
            new_balance = amount if raw[0] not in "+-" else current + amount
            if new_balance < 0:
                await message.answer("Баланс не может быть отрицательным.")
                return
            await conn.execute("UPDATE users SET balance=$1 WHERE user_id=$2", new_balance, target)
    await state.clear()
    await message.answer(f"✅ Баланс пользователя {target}: <b>{new_balance} ⭐</b>", parse_mode="HTML")


@dp.callback_query(F.data == "adm:reward")
async def admin_reward_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    current = await get_setting("referral_reward", str(REFERRAL_REWARD_DEFAULT))
    await state.set_state(Form.admin_reward_value)
    await callback.message.answer(f"Текущая награда: {current} ⭐. Введите новое целое неотрицательное значение:")
    await callback.answer()


@dp.message(Form.admin_reward_value)
async def admin_reward_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите целое число 0 или больше.")
        return
    await set_setting("referral_reward", raw)
    await state.clear()
    await message.answer(f"✅ Награда за реферала теперь {raw} ⭐.")


@dp.callback_query(F.data == "adm:channels")
async def admin_channels(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    channels = await get_channels()
    text = "📢 <b>Обязательные каналы</b>\n"
    text += "\n".join(f"• {html.escape(ch['title'])} — <code>{ch['chat_id']}</code>" for ch in channels) or "\nПока каналов нет."
    if callback.message:
        await callback.message.edit_text(text, reply_markup=channel_admin_keyboard(channels), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm:channel_add")
async def admin_channel_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(Form.admin_add_channel)
    await callback.message.answer("Отправьте одной строкой: ID канала | ссылка | название\nПример: -1001234567890 | https://t.me/example | Example")
    await callback.answer()


@dp.message(Form.admin_add_channel)
async def admin_channel_add(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    parts = [part.strip() for part in (message.text or "").split("|", 2)]
    if len(parts) != 3 or not parts[0].lstrip("-").isdigit() or not parts[1].startswith("https://t.me/") or not parts[2]:
        await message.answer("Формат неверный. Используйте: ID | https://t.me/channel | Название")
        return
    chat_id, url, title = int(parts[0]), parts[1], parts[2][:100]
    try:
        await bot.get_chat_member(chat_id=chat_id, user_id=(await bot.get_me()).id)
    except Exception:
        # Membership check may fail if the bot is not an administrator; don't save unusable channel.
        await message.answer("Не удалось проверить доступ бота к каналу. Добавьте бота администратором и проверьте ID.")
        return
    async with (await db()).acquire() as conn:
        await conn.execute(
            "INSERT INTO channels(chat_id,url,title) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET url=EXCLUDED.url,title=EXCLUDED.title",
            chat_id, url, title,
        )
    await state.clear()
    await message.answer(f"✅ Канал «{html.escape(title)}» сохранён.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm:delchan:"))
async def admin_channel_delete(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        chat_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    async with (await db()).acquire() as conn:
        await conn.execute("DELETE FROM channels WHERE chat_id=$1", chat_id)
    channels = await get_channels()
    if callback.message:
        await callback.message.edit_text("📢 <b>Обязательные каналы</b>", reply_markup=channel_admin_keyboard(channels), parse_mode="HTML")
    await callback.answer("Канал удалён.")


@dp.callback_query(F.data == "adm:channel_remove")
async def admin_channel_remove_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    channels = await get_channels()
    if not channels:
        await callback.answer("Список каналов пуст.", show_alert=True)
        return
    await state.set_state(Form.admin_remove_channel)
    await callback.message.answer("Введите ID канала для удаления:")
    await callback.answer()


@dp.message(Form.admin_remove_channel)
async def admin_channel_remove(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if not (message.text or "").lstrip("-").isdigit():
        await message.answer("Введите числовой ID канала.")
        return
    async with (await db()).acquire() as conn:
        status = await conn.execute("DELETE FROM channels WHERE chat_id=$1", int(message.text))
    await state.clear()
    await message.answer("✅ Канал удалён." if status.endswith("1") else "Канал не найден.")


async def health(_: web.Request) -> web.Response:
    try:
        async with (await db()).acquire() as conn:
            await conn.fetchval("SELECT 1")
        return web.json_response({"status": "ok", "service": "woodoogifi"})
    except Exception:
        log.exception("Health check failed")
        return web.json_response({"status": "error"}, status=503)


async def run_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health server listening on port %s", PORT)
    return runner


async def main():
    await init_db()
    runner = await run_web_server()
    try:
        log.info("WoodooGifi bot starting")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        if pool:
            await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
