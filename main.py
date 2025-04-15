import asyncio
import logging
import re
from datetime import datetime
import os
import firebase_admin
from firebase_admin import credentials, db
from openai import OpenAI
from dotenv import load_dotenv
import pytz
import pandas as pd

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError, TelegramConflictError

# 🔒 Загрузка переменных окружения из .env
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FIREBASE_URL = os.getenv("FIREBASE_URL")

if not API_TOKEN:
    raise ValueError("❌ API_TOKEN не найден! Проверь .env файл.")
if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY не найден! Проверь .env файл.")
if not FIREBASE_URL:
    raise ValueError("❌ FIREBASE_URL не найден! Проверь .env файл.")

# ⚙️ Инициализация Firebase
cred = credentials.Certificate("/Users/kainen/Desktop/davleniye/serviceAccountKey.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_URL
})

# ⚙️ Настройка логирования
logging.basicConfig(level=logging.INFO)

# 🤖 Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)

# Часовой пояс
TIMEZONE = pytz.timezone("Europe/Moscow")

# 📦 Состояния для регистрации
class Registration(StatesGroup):
    name = State()
    age = State()
    gender = State()
    height = State()
    weight = State()

# 📦 Состояния для давления
class PressureMeasurement(StatesGroup):
    first_measurement = State()
    second_measurement = State()

# 📦 Состояния для редактирования профиля
class EditProfile(StatesGroup):
    field = State()
    new_value = State()

# 📦 Состояния для установки напоминаний
class SetReminder(StatesGroup):
    time = State()

# 📦 Состояние для диалога с ИИ
class ChatWithAI(StatesGroup):
    active = State()

# Хранилище для истории переписки с ChatGPT (вопросы и ответы)
chat_history = {}

# ⏳ Загрузка данных из Firebase
def load_data():
    users_ref = db.reference('users')
    measurements_ref = db.reference('measurements')
    reminders_ref = db.reference('reminder_settings')

    users_data = users_ref.get() or {}
    measurements_data = measurements_ref.get() or {}
    reminders_data = reminders_ref.get() or {}

    # Преобразуем ключи в int (Firebase хранит их как строки)
    users = {int(k): v for k, v in users_data.items()}
    measurements = {int(k): v for k, v in measurements_data.items()}
    reminder_settings = {int(k): v for k, v in reminders_data.items()}

    logging.info(f"Loaded users: {users}")
    logging.info(f"Loaded measurements: {measurements}")
    logging.info(f"Loaded reminder_settings: {reminder_settings}")

    return users, measurements, reminder_settings

# ⏳ Сохранение данных в Firebase
def save_data(users, measurements, reminder_settings):
    users_ref = db.reference('users')
    measurements_ref = db.reference('measurements')
    reminders_ref = db.reference('reminder_settings')

    logging.info(f"Saving users: {users}")
    logging.info(f"Saving measurements: {measurements}")
    logging.info(f"Saving reminder_settings: {reminder_settings}")

    users_ref.set(users)
    measurements_ref.set(measurements)
    reminders_ref.set(reminder_settings)

# Загружаем данные при старте
users, measurements, reminder_settings = load_data()

# 📋 Главное меню
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Померить давление")],
            [KeyboardButton(text="Установить напоминания")],
            [KeyboardButton(text="Выключить напоминания")],
            [KeyboardButton(text="Показать историю")],
            [KeyboardButton(text="Экспорт данных")],
            [KeyboardButton(text="Редактировать профиль")],
            [KeyboardButton(text="Начать диалог с ИИ")],
        ],
        resize_keyboard=True
    )

# 📋 Меню диалога с ИИ
def get_ai_chat_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Закончить диалог с ИИ")]
        ],
        resize_keyboard=True
    )

# 📋 Меню выбора поля для редактирования профиля
def get_edit_profile_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Имя")],
            [KeyboardButton(text="Возраст")],
            [KeyboardButton(text="Пол")],
            [KeyboardButton(text="Рост")],
            [KeyboardButton(text="Вес")],
            [KeyboardButton(text="Сбросить историю измерений")],
            [KeyboardButton(text="Отмена")]
        ],
        resize_keyboard=True
    )

# Генерация промпта для анализа ChatGPT с пульсовым давлением и динамикой (в стиле кардиолога)
def generate_analysis_prompt(user, current, history):
    name = user["name"]
    age = user["age"]
    gender = user["gender"]
    height = user["height"]
    weight = user["weight"]

    # Вычисляем пульсовое давление
    sys_first, dia_first = map(int, current['first'].split('/'))
    sys_second, dia_second = map(int, current['second'].split('/'))
    pulse_pressure_first = sys_first - dia_first
    pulse_pressure_second = sys_second - dia_second

    # Средние значения за историю
    if history:
        sys_values = [int(entry['first'].split('/')[0]) for entry in history]
        dia_values = [int(entry['first'].split('/')[1]) for entry in history]
        avg_sys = sum(sys_values) / len(sys_values)
        avg_dia = sum(dia_values) / len(dia_values)
        trend = "стабильное"
        if len(history) > 1:
            if sys_values[-1] > sys_values[-2]:
                trend = "повышающееся"
            elif sys_values[-1] < sys_values[-2]:
                trend = "понижающееся"
    else:
        avg_sys, avg_dia, trend = "нет данных", "нет данных", "нет данных"

    history_lines = "\n".join(
        f"{entry['date']} — Первое: {entry['first']}, Второе: {entry['second']}"
        for entry in history[-10:]
    )

    prompt = (
        f"Я — твой личный кардиолог. Давай разберём твои показатели артериального давления, {name}.\n\n"
        f"Твои данные: возраст {age} лет, пол: {gender}, рост {height} см, вес {weight} кг.\n"
        f"Текущие измерения:\n"
        f"Первое измерение: {current['first']} (пульсовое давление: {pulse_pressure_first} мм рт. ст.)\n"
        f"Второе измерение: {current['second']} (пульсовое давление: {pulse_pressure_second} мм рт. ст.)\n\n"
        f"Средние значения по твоей истории:\n"
        f"Среднее систолическое: {avg_sys}\n"
        f"Среднее диастолическое: {avg_dia}\n"
        f"Динамика давления: {trend}\n\n"
        f"История измерений:\n{history_lines or 'Истории измерений пока нет.'}\n\n"
        f"Давай проанализируем твои показатели:\n"
        f"- Проверим, есть ли признаки гипертонии (давление выше 140/90) или гипотонии (давление ниже 90/60).\n"
        f"- Оценим пульсовое давление (норма 30-50 мм рт. ст.) и разницу между первым и вторым замером.\n"
        f"- Посмотрим на динамику твоих показателей.\n"
        f"После анализа я дам рекомендации по образу жизни или диете, которые могут помочь.\n\n"
        f"❗️Важно: Я не ставлю диагнозы. Если что-то вызывает беспокойство, рекомендую обратиться к врачу для очной консультации."
    )
    return prompt

# Генерация промпта для диалога с ИИ (в стиле кардиолога)
def generate_chat_prompt(user_id, question):
    user = users.get(user_id, {})
    user_measurements = measurements.get(user_id, [])
    name = user.get("name", "Неизвестно")
    age = user.get("age", "Неизвестно")
    gender = user.get("gender", "Неизвестно")
    height = user.get("height", "Неизвестно")
    weight = user.get("weight", "Неизвестно")

    history_lines = "\n".join(
        f"{entry['date']} — Первое: {entry['first']}, Второе: {entry['second']}"
        for entry in user_measurements[-10:]
    )

    # Получаем историю переписки
    user_chat_history = chat_history.get(user_id, [])
    chat_history_text = "\n".join(
        f"Пациент: {entry['question']}\nЯ: {entry['answer']}"
        for entry in user_chat_history[-5:]  # Ограничим 5 последними сообщениями
    )

    prompt = (
        f"Я — твой личный кардиолог, {name}. Моя задача — помогать тебе следить за артериальным давлением и отвечать на твои вопросы, связанные с сердцем и сосудами.\n\n"
        f"Твои данные:\n"
        f"Имя: {name}, возраст: {age} лет, пол: {gender}, рост: {height} см, вес: {weight} кг.\n\n"
        f"Твоя история измерений давления:\n{history_lines or 'Истории измерений пока нет.'}\n\n"
        f"Наша предыдущая переписка:\n{chat_history_text or 'Переписки пока нет.'}\n\n"
        f"Твой вопрос: {question}\n\n"
        f"Я постараюсь ответить максимально профессионально и понятно, учитывая твои данные и нашу переписку. "
        f"Если вопрос связан с давлением, дам рекомендации с учётом твоих показателей. "
        f"Если это общий вопрос, отвечу с учётом твоего здоровья и контекста. "
        f"❗️Важно: Я не ставлю диагнозы. Если есть сомнения, рекомендую обратиться к врачу для очной консультации."
    )
    return prompt

# /start
@dp.message(CommandStart())
async def start_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    logging.info(f"User {user_id} started the bot")
    try:
        if user_id not in users:
            await message.answer("Привет! Как тебя зовут?")
            await state.set_state(Registration.name)
        else:
            await message.answer(f"Привет, {users[user_id]['name']}! Что делаем? ❤️", reply_markup=get_main_menu())
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Регистрация: имя
@dp.message(Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Имя не может быть пустым. Попробуй еще раз!")
        return
    await state.update_data(name=name)
    try:
        await message.answer("Сколько тебе лет?")
        await state.set_state(Registration.age)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {message.from_user.id}")

# Регистрация: возраст
@dp.message(Registration.age)
async def process_age(message: types.Message, state: FSMContext):
    try:
        age = int(message.text)
        if not 1 <= age <= 120:
            raise ValueError
    except ValueError:
        await message.answer("Возраст должен быть числом от 1 до 120. Попробуй еще!")
        return
    await state.update_data(age=age)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Мужской")],
            [KeyboardButton(text="Женский")],
            [KeyboardButton(text="Другое")]
        ], resize_keyboard=True
    )
    try:
        await message.answer("Укажи пол:", reply_markup=keyboard)
        await state.set_state(Registration.gender)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {message.from_user.id}")

# Регистрация: пол
@dp.message(Registration.gender)
async def process_gender(message: types.Message, state: FSMContext):
    gender = message.text
    if gender not in ["Мужской", "Женский", "Другое"]:
        await message.answer("Выбери пол из предложенных вариантов!")
        return
    await state.update_data(gender=gender)
    try:
        await message.answer("Какой у тебя рост (в см)?", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(Registration.height)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {message.from_user.id}")

# Регистрация: рост
@dp.message(Registration.height)
async def process_height(message: types.Message, state: FSMContext):
    try:
        height = int(message.text)
        if not 50 <= height <= 250:
            raise ValueError
    except ValueError:
        await message.answer("Рост должен быть числом от 50 до 250 см. Попробуй еще!")
        return
    await state.update_data(height=height)
    try:
        await message.answer("Какой у тебя вес (в кг)?")
        await state.set_state(Registration.weight)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {message.from_user.id}")

# Регистрация: вес
@dp.message(Registration.weight)
async def process_weight(message: types.Message, state: FSMContext):
    try:
        weight = int(message.text)
        if not 20 <= weight <= 300:
            raise ValueError
    except ValueError:
        await message.answer("Вес должен быть числом от 20 до 300 кг. Попробуй еще!")
        return
    user_data = await state.get_data()
    user_data["weight"] = weight
    user_id = message.from_user.id
    users[user_id] = user_data
    measurements[user_id] = measurements.get(user_id, [])
    # Сохраняем данные в Firebase
    save_data(users, measurements, reminder_settings)
    try:
        await message.answer(
            f"Готово, {user_data['name']}! Твои данные: возраст {user_data['age']}, пол {user_data['gender']}, "
            f"рост {user_data['height']} см, вес {user_data['weight']} кг. Что делаем? ❤️",
            reply_markup=get_main_menu()
        )
        await state.clear()
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Померить давление
@dp.message(lambda message: message.text == "Померить давление")
async def measure_pressure(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    logging.info(f"User {user_id} started measuring pressure")
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    try:
        await message.answer(
            "Измерь давление и запиши результат в формате СИСТОЛИЧЕСКОЕ/ДИАСТОЛИЧЕСКОЕ (например, 120/80).",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(PressureMeasurement.first_measurement)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Первое измерение
@dp.message(PressureMeasurement.first_measurement)
async def process_first_measurement(message: types.Message, state: FSMContext):
    pressure = message.text.strip()
    if not re.match(r"^\d{2,3}/\d{2,3}$", pressure):
        await message.answer("Неверный формат! Введи, например, 120/80.")
        return
    try:
        sys, dia = map(int, pressure.split("/"))
        if not (50 <= sys <= 300 and 30 <= dia <= 200):
            raise ValueError
    except ValueError:
        await message.answer("Значения должны быть в пределах: систолическое 50-300, диастолическое 30-200.")
        return
    # Проверяем на высокое давление
    if sys > 140 or dia > 90:
        await message.answer("⚠️ Внимание: Ваше давление выше нормы (140/90). Рекомендуем обратиться к врачу.")
    await state.update_data(first_measurement=pressure)
    try:
        await message.answer("Хорошо, через 2-3 минуты измерь еще раз и запиши результат.")
        await state.set_state(PressureMeasurement.second_measurement)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {message.from_user.id}")

# Второе измерение
@dp.message(PressureMeasurement.second_measurement)
async def process_second_measurement(message: types.Message, state: FSMContext):
    pressure = message.text.strip()
    if not re.match(r"^\d{2,3}/\d{2,3}$", pressure):
        await message.answer("Неверный формат! Введи, например, 120/80.")
        return
    try:
        sys, dia = map(int, pressure.split("/"))
        if not (50 <= sys <= 300 and 30 <= dia <= 200):
            raise ValueError
    except ValueError:
        await message.answer("Значения должны быть в пределах: систолическое 50-300, диастолическое 30-200.")
        return
    # Проверяем на высокое давление
    if sys > 140 or dia > 90:
        await message.answer("⚠️ Внимание: Ваше давление выше нормы (140/90). Рекомендуем обратиться к врачу.")
    user_id = message.from_user.id
    user_data = await state.get_data()
    first = user_data["first_measurement"]
    entry = {
        "date": datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M"),
        "first": first,
        "second": pressure,
    }
    if user_id not in measurements:
        measurements[user_id] = []
    measurements[user_id].append(entry)
    # Сохраняем данные в Firebase
    save_data(users, measurements, reminder_settings)
    try:
        await message.answer(f"Записал! Первое: {first}, Второе: {pressure}. Что дальше? ❤️",
                             reply_markup=get_main_menu())
        # Анализ через ChatGPT
        prompt = generate_analysis_prompt(users[user_id], entry, measurements[user_id])
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=700
            )
            answer = response.choices[0].message.content.strip()
            await message.answer("📊 Анализирую данные давления...")
            await message.answer(answer)
        except Exception as e:
            logging.error(f"Ошибка анализа через ChatGPT: {e}")
            await message.answer("Произошла ошибка при анализе давления. Попробуй позже.")
        await state.clear()
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Показать историю
@dp.message(lambda message: message.text == "Показать историю")
async def show_history(message: types.Message):
    user_id = message.from_user.id
    logging.info(f"Showing history for user {user_id}")
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    user_measurements = measurements.get(user_id, [])
    logging.info(f"User {user_id} measurements: {user_measurements}")
    if not user_measurements:
        await message.answer("У тебя пока нет измерений. Давай измерим давление? ❤️")
        return
    history_text = "📜 Твоя история измерений:\n\n"
    for entry in user_measurements:
        history_text += f"Дата: {entry['date']}\nПервое: {entry['first']}\nВторое: {entry['second']}\n\n"
    try:
        await message.answer(history_text)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Экспорт данных
@dp.message(lambda message: message.text == "Экспорт данных")
async def export_data(message: types.Message):
    user_id = message.from_user.id
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    user_measurements = measurements.get(user_id, [])
    if not user_measurements:
        await message.answer("У тебя пока нет данных для экспорта. Давай измерим давление? ❤️")
        return
    df = pd.DataFrame(user_measurements)
    filename = f"measurements_{user_id}.xlsx"
    df.to_excel(filename, index=False)
    try:
        with open(filename, "rb") as file:
            await message.answer_document(types.BufferedInputFile(file.read(), filename=filename))
        await message.answer("📤 Данные экспортированы в Excel!")
        os.remove(filename)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Установить напоминания
@dp.message(lambda message: message.text == "Установить напоминания")
async def set_reminders(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    try:
        await message.answer(
            "Введи время для напоминания в формате ЧЧ:ММ (например, 09:00). "
            "Если хочешь несколько напоминаний, введи их через запятую (например, 09:00, 15:00).",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(SetReminder.time)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Обработка времени напоминаний
@dp.message(SetReminder.time)
async def process_reminder_time(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    times = [t.strip() for t in message.text.split(",")]
    valid_times = []
    for t in times:
        if not re.match(r"^\d{2}:\d{2}$", t):
            await message.answer(f"Неверный формат времени: {t}. Введи в формате ЧЧ:ММ (например, 09:00).")
            return
        try:
            hour, minute = map(int, t.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            valid_times.append(t)
        except ValueError:
            await message.answer(f"Некорректное время: {t}. Часы: 0-23, минуты: 0-59.")
            return
    reminder_settings[user_id] = {"times": valid_times, "active": True}
    # Сохраняем данные в Firebase
    save_data(users, measurements, reminder_settings)
    try:
        await message.answer(f"Напоминания установлены на: {', '.join(valid_times)}", reply_markup=get_main_menu())
        await state.clear()
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Выключить напоминания
@dp.message(lambda message: message.text == "Выключить напоминания")
async def disable_reminders(message: types.Message):
    user_id = message.from_user.id
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    reminder_settings[user_id] = reminder_settings.get(user_id, {})
    reminder_settings[user_id]["active"] = False
    # Сохраняем данные в Firebase
    save_data(users, measurements, reminder_settings)
    try:
        await message.answer("⛔ Напоминания отключены! Включи снова, когда будет нужно.", reply_markup=get_main_menu())
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Редактировать профиль
@dp.message(lambda message: message.text == "Редактировать профиль")
async def edit_profile(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    try:
        await message.answer("Что хочешь изменить?", reply_markup=get_edit_profile_menu())
        await state.set_state(EditProfile.field)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Выбор поля для редактирования
@dp.message(EditProfile.field)
async def process_edit_field(message: types.Message, state: FSMContext):
    field = message.text
    user_id = message.from_user.id
    logging.info(f"User {user_id} selected edit field: {field}")
    if field == "Отмена":
        await message.answer("Редактирование отменено.", reply_markup=get_main_menu())
        await state.clear()
        return
    if field == "Сбросить историю измерений":
        measurements[user_id] = []
        save_data(users, measurements, reminder_settings)
        await message.answer("История измерений сброшена.", reply_markup=get_main_menu())
        await state.clear()
        return
    if field not in ["Имя", "Возраст", "Пол", "Рост", "Вес"]:
        await message.answer("Выбери поле из предложенных!")
        return
    await state.update_data(field=field.lower())
    if field == "Пол":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Мужской")],
                [KeyboardButton(text="Женский")],
                [KeyboardButton(text="Другое")]
            ], resize_keyboard=True
        )
        await message.answer("Выбери новый пол:", reply_markup=keyboard)
    else:
        await message.answer(f"Введи новое значение для {field}:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(EditProfile.new_value)

# Новое значение для профиля
@dp.message(EditProfile.new_value)
async def process_new_value(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    field = data["field"]
    value = message.text.strip()

    try:
        if field == "имя":
            if not value:
                await message.answer("Имя не может быть пустым!")
                return
        elif field == "возраст":
            value = int(value)
            if not 1 <= value <= 120:
                await message.answer("Возраст должен быть числом от 1 до 120!")
                return
        elif field == "пол":
            if value not in ["Мужской", "Женский", "Другое"]:
                await message.answer("Выбери пол из предложенных вариантов!")
                return
        elif field == "рост":
            value = int(value)
            if not 50 <= value <= 250:
                await message.answer("Рост должен быть числом от 50 до 250 см!")
                return
        elif field == "вес":
            value = int(value)
            if not 20 <= value <= 300:
                await message.answer("Вес должен быть числом от 20 до 300 кг!")
                return

        users[user_id][field] = value
        save_data(users, measurements, reminder_settings)
        await message.answer(f"{field.capitalize()} обновлено: {value}.", reply_markup=get_main_menu())
        await state.clear()
    except ValueError:
        await message.answer("Неверный формат! Введи корректное значение.")
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Начать диалог с ИИ
@dp.message(lambda message: message.text == "Начать диалог с ИИ")
async def start_ai_chat(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in users:
        await message.answer("Сначала зарегистрируйся! Напиши /start.")
        return
    try:
        await message.answer(
            "Здравствуйте! Я ваш личный кардиолог. Задавайте свои вопросы, и я отвечу с учётом ваших данных и истории давления. "
            "Когда закончите, нажмите 'Закончить диалог с ИИ'.",
            reply_markup=get_ai_chat_menu()
        )
        await state.set_state(ChatWithAI.active)
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Закончить диалог с ИИ
@dp.message(lambda message: message.text == "Закончить диалог с ИИ")
async def end_ai_chat(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        await message.answer("Диалог завершён. Если у вас появятся новые вопросы, я всегда здесь! Что делаем дальше? ❤️", reply_markup=get_main_menu())
        await state.clear()
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Обработка вопросов к ИИ
@dp.message(ChatWithAI.active)
async def handle_ai_chat(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    question = message.text

    # Пропускаем команды или кнопки, которые уже обработаны
    if question in ["Закончить диалог с ИИ"]:
        return

    try:
        # Генерируем промпт для ChatGPT
        prompt = generate_chat_prompt(user_id, question)
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=700
        )
        answer = response.choices[0].message.content.strip()

        # Сохраняем вопрос и ответ в историю
        if user_id not in chat_history:
            chat_history[user_id] = []
        chat_history[user_id].append({"question": question, "answer": answer})

        # Ограничиваем историю 10 записями
        if len(chat_history[user_id]) > 10:
            chat_history[user_id] = chat_history[user_id][-10:]

        await message.answer(answer, reply_markup=get_ai_chat_menu())
    except Exception as e:
        logging.error(f"Ошибка при обращении к ChatGPT: {e}")
        await message.answer("Произошла ошибка при обработке вопроса. Попробуйте позже.", reply_markup=get_ai_chat_menu())
    except TelegramForbiddenError:
        logging.warning(f"Bot was blocked by user {user_id}")

# Цикл для напоминаний
async def reminder_loop():
    logging.info("Starting reminder loop")
    while True:
        now = datetime.now(TIMEZONE)
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%d.%m.%Y")
        logging.info(f"Checking reminders at {current_time}")
        for user_id, settings in reminder_settings.items():
            logging.info(f"User {user_id} settings: {settings}")
            if not settings.get("active", False):
                logging.info(f"Reminders for user {user_id} are not active")
                continue
            times = settings.get("times", [])
            if current_time in times:
                logging.info(f"Found matching time {current_time} for user {user_id}")
                user_measurements = measurements.get(user_id, [])
                measured_today = any(
                    entry["date"].startswith(current_date)
                    for entry in user_measurements
                )
                if measured_today:
                    logging.info(f"User {user_id} already measured today")
                    continue
                try:
                    await bot.send_message(user_id, "⏰ Напоминание: пора измерить давление!")
                    logging.info(f"Sent reminder to user {user_id} at {current_time}")
                except TelegramForbiddenError:
                    logging.warning(f"Bot was blocked by user {user_id}")
                except Exception as e:
                    logging.warning(f"Failed to send reminder to user {user_id}: {e}")
        await asyncio.sleep(60)

# Запуск бота
async def main():
    logging.info("Starting bot")
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # Очищаем webhook и очередь обновлений
            await bot.delete_webhook(drop_pending_updates=True)
            logging.info("Webhook deleted")
            # Дополнительно очищаем очередь getUpdates
            updates = await bot.get_updates(offset=-1, limit=1)
            logging.info(f"Cleared getUpdates queue: {updates}")
            break
        except TelegramConflictError as e:
            logging.warning(f"Conflict error on attempt {attempt + 1}/{max_retries}: {e}")
            if attempt == max_retries - 1:
                logging.error("Max retries reached. Could not resolve conflict.")
                raise
            await asyncio.sleep(5)  # Ждём перед повторной попыткой
        except Exception as e:
            logging.warning(f"Failed to initialize on attempt {attempt + 1}/{max_retries}: {e}")
            if attempt == max_retries - 1:
                logging.error("Max retries reached. Could not initialize bot.")
                raise
            await asyncio.sleep(5)

    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
