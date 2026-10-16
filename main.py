import asyncio
import os
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получение переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # ID администратора Telegram

if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== РАБОТА С БАЗОЙ ДАННЫХ ====================
DB_NAME = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            points INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, balance, points FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def add_user(user_id: int, username: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username, balance, points)
        VALUES (?, ?, 0.0, 0)
    """, (user_id, username))
    conn.commit()
    conn.close()

def update_user_balance(user_id: int, new_balance: float):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    conn.commit()
    conn.close()

def update_user_points(user_id: int, new_points: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET points = ? WHERE user_id = ?", (new_points, user_id))
    conn.commit()
    conn.close()

# ==================== СОСТОЯНИЯ FSM (ДЛЯ АДМИНКИ) ====================
class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    choosing_action = State()
    waiting_for_balance = State()
    waiting_for_points = State()

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard(user_id: int):
    buttons = [
        [KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="⭐ Очки")]
    ]
    if ADMIN_ID and str(user_id) == str(ADMIN_ID):
        buttons.append([KeyboardButton(text="🛠️ Админ-панель")])
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_admin_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить баланс", callback_data="admin_change_balance")],
        [InlineKeyboardButton(text="Изменить очки", callback_data="admin_change_points")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]
    ])

# ==================== ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ====================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    add_user(user_id, username)
    
    await message.answer(
        f"Добро пожаловать, {message.from_user.first_name}!\nВоспользуйтесь меню для навигации.",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    user = get_user(message.from_user.id)
    if not user:
        add_user(message.from_user.id, message.from_user.username or message.from_user.first_name)
        user = get_user(message.from_user.id)
        
    u_id, username, balance, points = user
    text = (
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"🆔 <b>ID:</b> <code>{u_id}</code>\n"
        f"👤 <b>Имя:</b> {message.from_user.first_name}\n"
        f"💰 <b>Баланс:</b> {balance:.2f} руб.\n"
        f"⭐ <b>Очки:</b> {points}"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "💰 Баланс")
async def show_balance(message: types.Message):
    user = get_user(message.from_user.id)
    balance = user[2] if user else 0.0
    await message.answer(f"💰 Ваш текущий баланс: <b>{balance:.2f} руб.</b>", parse_mode="HTML")

@dp.message(F.text == "⭐ Очки")
async def show_points(message: types.Message):
    user = get_user(message.from_user.id)
    points = user[3] if user else 0
    await message.answer(f"⭐ Ваши очки: <b>{points}</b>", parse_mode="HTML")

# ==================== ХЕНДЛЕРЫ АДМИН-ПАНЕЛИ ====================
@dp.message(F.text == "🛠️ Админ-панель")
async def admin_panel(message: types.Message, state: FSMContext):
    if not ADMIN_ID or str(message.from_user.id) != str(ADMIN_ID):
        await message.answer("У вас нет доступа к админ-панели.")
        return
    
    await state.set_state(AdminStates.waiting_for_user_id)
    await message.answer("🛠️ <b>Админ-панель</b>\n\nВведите Telegram ID пользователя для управления:", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_user_id)
async def admin_process_user_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите корректный числовой ID:")
        return
    
    target_id = int(message.text)
    target_user = get_user(target_id)
    
    if not target_user:
        await message.answer("Пользователь с таким ID не найден в базе данных.")
        await state.clear()
        return
    
    await state.update_data(target_id=target_id)
    await state.set_state(AdminStates.choosing_action)
    
    u_id, username, balance, points = target_user
    text = (
        f"Управление пользователем <code>{u_id}</code> (@{username}):\n\n"
        f"💰 Баланс: {balance:.2f}\n"
        f"⭐ Очки: {points}\n\n"
        f"Выберите действие:"
    )
    await message.answer(text, reply_markup=get_admin_inline_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.")
    await callback.answer()

@dp.callback_query(F.data == "admin_change_balance")
async def admin_start_change_balance(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_balance)
    await callback.message.edit_text("Введите новое значение баланса (число):")
    await callback.answer()

@dp.message(AdminStates.waiting_for_balance)
async def admin_set_balance(message: types.Message, state: FSMContext):
    try:
        new_balance = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Некорректное значение. Введите число:")
        return
    
    data = await state.get_data()
    target_id = data.get("target_id")
    
    update_user_balance(target_id, new_balance)
    await state.clear()
    await message.answer(f"✅ Баланс пользователя <code>{target_id}</code> успешно изменен на <b>{new_balance:.2f}</b>", parse_mode="HTML")

@dp.callback_query(F.data == "admin_change_points")
async def admin_start_change_points(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_points)
    await callback.message.edit_text("Введите новое значение очков (целое число):")
    await callback.answer()

@dp.message(AdminStates.waiting_for_points)
async def admin_set_points(message: types.Message, state: FSMContext):
    if not message.text.lstrip('-').isdigit():
        await message.answer("Некорректное значение. Введите целое число:")
        return
    
    new_points = int(message.text)
    data = await state.get_data()
    target_id = data.get("target_id")
    
    update_user_points(target_id, new_points)
    await state.clear()
    await message.answer(f"✅ Очки пользователя <code>{target_id}</code> успешно изменены на <b>{new_points}</b>", parse_mode="HTML")

# ==================== ЗАПУСК БОТА ====================
async def main():
    init_db()
    logging.info("База данных инициализирована.")
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())