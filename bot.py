import os
import telebot
from telebot import types
from gigachat import GigaChat
import sqlite3
from datetime import datetime
import threading

# =========================
# DATABASE / STATS
# =========================

conn = sqlite3.connect("bot_stats.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA busy_timeout = 5000;")
db_lock = threading.Lock()

with db_lock:
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TEXT,
        last_seen TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        event_name TEXT,
        event_value TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        username TEXT,
        mode TEXT,
        step TEXT,
        answer_text TEXT,
        created_at TEXT
    )
    """)
    conn.commit()


def now():
    return datetime.now().isoformat()


def save_user_obj(user):
    telegram_id = user.id
    username = user.username

    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE users
                SET username = ?, last_seen = ?
                WHERE telegram_id = ?
            """, (username, now(), telegram_id))
        else:
            cur.execute("""
                INSERT INTO users (telegram_id, username, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
            """, (telegram_id, username, now(), now()))

        conn.commit()


def save_user_message(message):
    save_user_obj(message.from_user)


def save_user_call(call):
    save_user_obj(call.from_user)


def log_event(telegram_id, event_name, event_value=None):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO events (telegram_id, event_name, event_value, created_at)
            VALUES (?, ?, ?, ?)
        """, (telegram_id, event_name, event_value, now()))
        conn.commit()


def save_user_answer(telegram_id, username, mode, step, answer_text):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_answers (
                telegram_id, username, mode, step, answer_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            telegram_id,
            username,
            mode,
            step,
            answer_text,
            now()
        ))
        conn.commit()


def get_stats_text():
    with db_lock:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'start'")
        start_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'click_discuss'")
        discuss_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'click_match'")
        match_click_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'match_book_entered'")
        match_book_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'match_request_created'")
        match_request_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'match_found'")
        match_found_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE event_name = 'contact_exchange_confirmed'")
        confirmed_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM user_answers")
        answers_count = cur.fetchone()[0]

    return (
        f"Статистика бота:\n\n"
        f"Уникальных пользователей: {users_count}\n"
        f"Команда /start: {start_count}\n"
        f"Нажали «Обсудить книгу»: {discuss_count}\n"
        f"Нажали «Найти собеседника»: {match_click_count}\n"
        f"Ввели книгу для мэтчинга: {match_book_count}\n"
        f"Создали заявку на мэтчинг: {match_request_count}\n"
        f"Найдено мэтчей: {match_found_count}\n"
        f"Подтверждений обмена контактами: {confirmed_count}\n"
        f"Всего сохранённых ответов: {answers_count}"
    )


def get_user_answers_text(telegram_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""
            SELECT mode, step, answer_text, created_at
            FROM user_answers
            WHERE telegram_id = ?
            ORDER BY id DESC
        """, (telegram_id,))
        rows = cur.fetchall()

    if not rows:
        return "У этого пользователя пока нет сохранённых ответов."

    text = "Твои сохранённые ответы:\n\n"
    for mode, step, answer_text, created_at in rows[:20]:
        text += f"[{created_at}]\n{mode} / {step}: {answer_text}\n\n"
    return text


# =========================
# BOT SETUP
# =========================

telegram_token = os.getenv("TG_TOKEN", "").strip()
gigachat_credentials = os.getenv("GIGACHAT_CREDENTIALS", "").strip()

if not telegram_token:
    raise ValueError("Переменная окружения TG_TOKEN не задана")

bot = telebot.TeleBot(telegram_token, threaded=False)

CHANNEL_USERNAME = "@libbuddy"

user_states = {}
match_requests = []
pending_matches = {}


# =========================
# GIGACHAT
# =========================

SYSTEM_PROMPT = """
Ты — дружелюбный собеседник для обсуждения книг в телеграм-боте.
Отвечай тепло, естественно и по-человечески.
Не повторяй один и тот же шаблон.
Отвечай кратко: 2–5 предложений.
Поддерживай разговор по книге, впечатлениям, героям, сюжету и атмосфере.
В конце можешь задать один уместный уточняющий вопрос, если это помогает продолжить диалог.
Не упоминай, что ты нейросеть или ИИ, если тебя об этом не спрашивали.
""".strip()


def answer(text):
    if not gigachat_credentials:
        return "Сейчас не настроен GigaChat. Добавь переменную GIGACHAT_CREDENTIALS в Railway."

    prompt = f"{SYSTEM_PROMPT}\n\nСообщение пользователя:\n{text}"

    try:
        with GigaChat(
            credentials=gigachat_credentials,
            verify_ssl_certs=False
        ) as giga:
            response = giga.chat(prompt)
            return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Не получилось обратиться к GigaChat: {e}"


# =========================
# HELPERS
# =========================

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        types.KeyboardButton("Обсудить книгу"),
        types.KeyboardButton("Найти собеседника")
    )
    return markup


def normalize(text):
    return str(text).strip().lower()


def make_inline(options, prefix):
    markup = types.InlineKeyboardMarkup()
    for key, label in options:
        markup.add(
            types.InlineKeyboardButton(
                label,
                callback_data=f"{prefix}:{key}"
            )
        )
    return markup


def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status not in ["left", "kicked"]
    except Exception:
        return False


def subscribe_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "Подписаться на канал",
            url="https://t.me/libbuddy"
        )
    )
    markup.add(
        types.InlineKeyboardButton(
            "Подписался",
            callback_data="check_subscription"
        )
    )
    return markup


def require_subscription(message, next_action):
    user_id = message.from_user.id

    if is_subscribed(user_id):
        return True

    user_states[message.chat.id] = {
        "mode": "waiting_subscription",
        "next_action": next_action
    }

    bot.send_message(
        message.chat.id,
        "Сначала нужно подписаться на наш канал, а потом я смогу продолжить ✨",
        reply_markup=subscribe_markup()
    )
    return False


def find_match(current_user):
    for req in match_requests:
        if req["telegram_id"] == current_user["telegram_id"]:
            continue
        if req.get("status") != "active":
            continue
        if normalize(req["book"]) != normalize(current_user["book"]):
            continue
        if normalize(req["format"]) != normalize(current_user["format"]):
            continue
        if normalize(req["spoilers"]) != normalize(current_user["spoilers"]):
            continue
        if normalize(req["stage"]) != normalize(current_user["stage"]):
            continue
        return req
    return None


def send_contact_offer(user1, user2):
    match_id = f"{user1['telegram_id']}_{user2['telegram_id']}"

    pending_matches[match_id] = {
        "user1": user1,
        "user2": user2,
        "user1_confirmed": False,
        "user2_confirmed": False
    }

    markup1 = types.InlineKeyboardMarkup()
    markup1.add(
        types.InlineKeyboardButton(
            "Открыть контакт",
            callback_data=f"accept_{match_id}_1"
        )
    )
    markup1.add(
        types.InlineKeyboardButton(
            "Пока не хочу",
            callback_data=f"decline_{match_id}_1"
        )
    )

    markup2 = types.InlineKeyboardMarkup()
    markup2.add(
        types.InlineKeyboardButton(
            "Открыть контакт",
            callback_data=f"accept_{match_id}_2"
        )
    )
    markup2.add(
        types.InlineKeyboardButton(
            "Пока не хочу",
            callback_data=f"decline_{match_id}_2"
        )
    )

    bot.send_message(
        user1["telegram_id"],
        "Нашёлся человек с похожим запросом. Хочешь обменяться контактами?",
        reply_markup=markup1
    )

    bot.send_message(
        user2["telegram_id"],
        "Нашёлся человек с похожим запросом. Хочешь обменяться контактами?",
        reply_markup=markup2
    )


# =========================
# HANDLERS
# =========================

@bot.message_handler(commands=['start'])
def start(message):
    save_user_message(message)
    log_event(message.from_user.id, "start")

    bot.send_message(
        message.chat.id,
        "Привет! Здесь можно спокойно обсудить книгу со мной или попробовать найти собеседника для чтения.\n\nВыбери, что тебе сейчас ближе:",
        reply_markup=main_menu()
    )


@bot.message_handler(commands=['stats'])
def stats(message):
    save_user_message(message)
    bot.send_message(message.chat.id, get_stats_text())


@bot.message_handler(commands=['myanswers'])
def my_answers(message):
    save_user_message(message)
    bot.send_message(message.chat.id, get_user_answers_text(message.from_user.id))


@bot.message_handler(func=lambda message: message.text == "Обсудить книгу")
def discuss_mode(message):
    save_user_message(message)
    log_event(message.from_user.id, "click_discuss")

    if not require_subscription(message, "discuss"):
        return

    user_states[message.chat.id] = {"mode": "discuss"}
    bot.send_message(
        message.chat.id,
        "Давай. Напиши, что ты сейчас читаешь или какая книга у тебя сейчас не выходит из головы."
    )


@bot.message_handler(func=lambda message: message.text == "Найти собеседника")
def match_mode_start(message):
    save_user_message(message)
    log_event(message.from_user.id, "click_match")

    if not require_subscription(message, "match"):
        return

    user_states[message.chat.id] = {
        "mode": "match",
        "step": "book"
    }
    bot.send_message(
        message.chat.id,
        "Напиши книгу, для которой ты хочешь найти собеседника."
    )


@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get("mode") == "discuss")
def handle_discussion(message):
    save_user_message(message)

    save_user_answer(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        mode="discuss",
        step="free_text",
        answer_text=message.text
    )

    try:
        ans = answer(message.text)
        bot.send_message(message.chat.id, ans, reply_markup=main_menu())
    except Exception:
        bot.send_message(
            message.chat.id,
            "Что-то пошло не так. Попробуй ещё раз написать, что тебя зацепило в книге.",
            reply_markup=main_menu()
        )


@bot.message_handler(
    func=lambda message: user_states.get(message.chat.id, {}).get("mode") == "match"
    and user_states.get(message.chat.id, {}).get("step") == "book"
)
def handle_match_book(message):
    save_user_message(message)

    chat_id = message.chat.id
    state = user_states.get(chat_id, {})
    state["book"] = message.text
    log_event(chat_id, "match_book_entered", state["book"])

    save_user_answer(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        mode="match",
        step="book",
        answer_text=message.text
    )

    state["step"] = "stage"
    user_states[chat_id] = state

    stage_options = [
        ("start", "Только начал(а)"),
        ("middle", "В середине"),
        ("almost_done", "Почти дочитал(а)"),
        ("finished", "Уже дочитал(а)")
    ]

    bot.send_message(
        chat_id,
        f"Книга: {state['book']}\n\nНа каком ты этапе чтения?",
        reply_markup=make_inline(stage_options, "stage")
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def check_subscription_callback(call):
    save_user_call(call)

    user_id = call.from_user.id
    chat_id = call.message.chat.id

    if is_subscribed(user_id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")

        state = user_states.get(chat_id, {})
        next_action = state.get("next_action")

        if next_action == "discuss":
            user_states[chat_id] = {"mode": "discuss"}
            bot.send_message(
                chat_id,
                "Спасибо за подписку ✨ Теперь можем обсудить книгу.\n\nНапиши, что ты сейчас читаешь или какая книга у тебя не выходит из головы.",
                reply_markup=main_menu()
            )

        elif next_action == "match":
            user_states[chat_id] = {
                "mode": "match",
                "step": "book"
            }
            bot.send_message(
                chat_id,
                "Спасибо за подписку ✨ Теперь можно искать собеседника.\n\nНапиши книгу, для которой ты хочешь найти собеседника.",
                reply_markup=main_menu()
            )

        else:
            bot.send_message(
                chat_id,
                "Подписка подтверждена ✅",
                reply_markup=main_menu()
            )
    else:
        bot.answer_callback_query(call.id, "Я пока не вижу подписку")
        bot.send_message(
            chat_id,
            "Похоже, подписки пока нет. Подпишись на канал и потом снова нажми «Подписался».",
            reply_markup=subscribe_markup()
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("stage:"))
def handle_stage_callback(call):
    save_user_call(call)

    chat_id = call.message.chat.id
    state = user_states.get(chat_id, {})
    if state.get("mode") != "match":
        bot.answer_callback_query(call.id)
        return

    mapping = {
        "start": "только начал(а)",
        "middle": "в середине",
        "almost_done": "почти дочитал(а)",
        "finished": "уже дочитал(а)"
    }

    value = call.data.split(":")[1]
    state["stage"] = mapping[value]

    save_user_answer(
        telegram_id=call.from_user.id,
        username=call.from_user.username,
        mode="match",
        step="stage",
        answer_text=state["stage"]
    )

    state["step"] = "spoilers"
    user_states[chat_id] = state

    spoiler_options = [
        ("no", "Без спойлеров"),
        ("yes", "Можно со спойлерами")
    ]

    bot.answer_callback_query(call.id, "Этап чтения сохранён.")
    bot.send_message(
        chat_id,
        "Как тебе комфортнее обсуждать книгу?",
        reply_markup=make_inline(spoiler_options, "spoilers")
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("spoilers:"))
def handle_spoilers_callback(call):
    save_user_call(call)

    chat_id = call.message.chat.id
    state = user_states.get(chat_id, {})
    if state.get("mode") != "match":
        bot.answer_callback_query(call.id)
        return

    mapping = {
        "no": "без спойлеров",
        "yes": "можно со спойлерами"
    }

    value = call.data.split(":")[1]
    state["spoilers"] = mapping[value]

    save_user_answer(
        telegram_id=call.from_user.id,
        username=call.from_user.username,
        mode="match",
        step="spoilers",
        answer_text=state["spoilers"]
    )

    state["step"] = "format"
    user_states[chat_id] = state

    format_options = [
        ("solo", "Один на один"),
        ("group", "Мини-группа")
    ]

    bot.answer_callback_query(call.id, "Отношение к спойлерам сохранено.")
    bot.send_message(
        chat_id,
        "Какой формат общения тебе ближе?",
        reply_markup=make_inline(format_options, "format")
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("format:"))
def handle_format_callback(call):
    save_user_call(call)

    chat_id = call.message.chat.id
    state = user_states.get(chat_id, {})
    if state.get("mode") != "match":
        bot.answer_callback_query(call.id)
        return

    mapping = {
        "solo": "один на один",
        "group": "мини-группа"
    }

    value = call.data.split(":")[1]
    state["format"] = mapping[value]
    state["telegram_id"] = chat_id
    state["username"] = call.from_user.username
    state["status"] = "active"

    save_user_answer(
        telegram_id=call.from_user.id,
        username=call.from_user.username,
        mode="match",
        step="format",
        answer_text=state["format"]
    )

    user_states[chat_id] = state
    match_requests.append(state.copy())
    log_event(chat_id, "match_request_created", state["book"])

    found = find_match(state)

    bot.answer_callback_query(call.id, "Формат общения сохранён.")

    if found:
        log_event(chat_id, "match_found", state["book"])
        log_event(found["telegram_id"], "match_found", found["book"])

        bot.send_message(
            chat_id,
            f"Я нашёл тебе совпадение по книге «{state['book']}». Сейчас предложу вам обменяться контактами.",
            reply_markup=main_menu()
        )
        bot.send_message(
            found["telegram_id"],
            f"Я нашёл тебе совпадение по книге «{found['book']}». Сейчас предложу вам обменяться контактами.",
            reply_markup=main_menu()
        )
        send_contact_offer(state, found)
    else:
        bot.send_message(
            chat_id,
            "Пока подходящего собеседника не нашлось, но я сохранил(а) твою заявку. Когда появится совпадение, я напишу.",
            reply_markup=main_menu()
        )

    user_states[chat_id] = {"mode": None}


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("accept_") or call.data.startswith("decline_")
)
def callback_handler(call):
    save_user_call(call)

    parts = call.data.split("_")
    action = parts[0]
    match_id = f"{parts[1]}_{parts[2]}"
    user_num = parts[3]

    match = pending_matches.get(match_id)
    if not match:
        bot.answer_callback_query(call.id, "Этот мэтч уже недоступен.")
        return

    if action == "decline":
        save_user_answer(
            telegram_id=call.from_user.id,
            username=call.from_user.username,
            mode="match",
            step="contact_exchange",
            answer_text="decline"
        )

        bot.answer_callback_query(call.id, "Хорошо, контакт не открою.")
        bot.send_message(
            call.message.chat.id,
            "Окей, не открываю контакт.",
            reply_markup=main_menu()
        )
        return

    if user_num == "1":
        match["user1_confirmed"] = True
    else:
        match["user2_confirmed"] = True

    save_user_answer(
        telegram_id=call.from_user.id,
        username=call.from_user.username,
        mode="match",
        step="contact_exchange",
        answer_text="accept"
    )

    log_event(call.from_user.id, "contact_exchange_confirmed")
    bot.answer_callback_query(call.id, "Отлично, зафиксировал.")

    if match["user1_confirmed"] and match["user2_confirmed"]:
        user1 = match["user1"]
        user2 = match["user2"]

        contact1 = f"@{user1['username']}" if user1.get("username") else "username не указан"
        contact2 = f"@{user2['username']}" if user2.get("username") else "username не указан"

        bot.send_message(
            user1["telegram_id"],
            f"Мэтч подтверждён с обеих сторон.\nВот контакт собеседника: {contact2}",
            reply_markup=main_menu()
        )
        bot.send_message(
            user2["telegram_id"],
            f"Мэтч подтверждён с обеих сторон.\nВот контакт собеседника: {contact1}",
            reply_markup=main_menu()
        )


print("Бот готов к работе")
bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
