from flask import Flask, request, jsonify, render_template, send_from_directory
import os, random, json
from datetime import datetime, date
from supabase import create_client, Client

app = Flask(__name__, static_folder="static", template_folder="templates")

# ─── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Вспомогательные функции ───────────────────────────────────────────────────

def get_all_words():
    res = supabase.table("words").select("*").execute()
    return res.data or []

def get_hard_words(user_id):
    res = (supabase.table("user_progress")
           .select("hard_words")
           .eq("user_id", user_id)
           .single()
           .execute())
    if res.data:
        return res.data.get("hard_words", [])
    return []

def get_or_create_user(user_id):
    res = (supabase.table("user_progress")
           .select("*")
           .eq("user_id", user_id)
           .execute())
    if res.data:
        return res.data[0]
    new_user = {
        "user_id": user_id,
        "hard_words": [],
        "current_word_id": None,
        "session_word_ids": [],
        "current_index": 0,
        "mode": "menu",
        "total_correct": 0,
        "total_incorrect": 0,
        "sessions": 0,
        "streak": 0,
        "last_session_date": None,
    }
    supabase.table("user_progress").insert(new_user).execute()
    return new_user

def update_user(user_id, data):
    supabase.table("user_progress").update(data).eq("user_id", user_id).execute()

def get_session_words(user_data, count=10):
    all_words = get_all_words()
    hard_ids = user_data.get("hard_words", [])

    hard_words = [w for w in all_words if w["id"] in hard_ids]
    easy_words = [w for w in all_words if w["id"] not in hard_ids]

    hard_count = min(len(hard_words), count // 3)
    easy_count = count - hard_count

    session = []
    if hard_count:
        session += random.sample(hard_words, hard_count)
    if easy_words:
        session += random.sample(easy_words, min(easy_count, len(easy_words)))

    random.shuffle(session)
    return session[:count]

def normalize(text):
    return text.lower().strip().replace("ё", "е")

def check_answer(user_answer, correct_ru):
    u = normalize(user_answer)
    c = normalize(correct_ru)
    if u == c:
        return True
    variants = [v.strip() for v in c.split("/")]
    if u in variants:
        return True
    if len(u) > 3 and len(c) > 3:
        matches = sum(a == b for a, b in zip(u, c))
        if matches / max(len(u), len(c)) >= 0.82:
            return True
    return False

def alice_resp(text, session, end_session=False, buttons=None):
    resp = {
        "version": "1.0",
        "session": session,
        "response": {
            "text": text,
            "tts": text,
            "end_session": end_session,
        },
    }
    if buttons:
        resp["response"]["buttons"] = [{"title": b, "hide": True} for b in buttons]
    return jsonify(resp)

# ─── Алиса Webhook ─────────────────────────────────────────────────────────────

@app.route("/alice", methods=["POST"])
def alice():
    data = request.json
    session = data.get("session", {})
    user_id = session.get("user", {}).get("user_id", session.get("session_id", "anon"))
    command = data.get("request", {}).get("command", "").lower().strip()
    is_new = session.get("new", False)

    user = get_or_create_user(user_id)
    mode = user.get("mode", "menu")

    # Обновить streak
    today = str(date.today())
    last = user.get("last_session_date")
    if last != today:
        update_user(user_id, {"last_session_date": today,
                               "sessions": user.get("sessions", 0) + 1})

    # ── Меню ──
    if is_new or command in ["помощь", "меню", "стоп", "выход", "начало"]:
        update_user(user_id, {"mode": "menu"})
        hard_count = len(user.get("hard_words", []))
        hard_msg = f" Сложных слов для повторения: {hard_count}." if hard_count else ""
        text = (f"Привет! Я тренажёр английских слов.{hard_msg} "
                "Скажите «тренировка» или «повторить».")
        return alice_resp(text, session, buttons=["Тренировка", "Повторить", "Статистика"])

    # ── Статистика ──
    if "статистик" in command:
        c = user.get("total_correct", 0)
        i = user.get("total_incorrect", 0)
        total = c + i
        pct = round(c / total * 100) if total else 0
        hard = len(user.get("hard_words", []))
        text = (f"Правильных ответов: {c}, неправильных: {i}. "
                f"Точность {pct}%. Сложных слов: {hard}.")
        return alice_resp(text, session, buttons=["Тренировка", "Повторить"])

    # ── Старт тренировки ──
    if command in ["тренировка", "начать", "учить слова", "начать тренировку"]:
        words = get_session_words(user)
        if not words:
            return alice_resp("Слов нет. Добавьте слова в веб-дашборде!", session)
        ids = [w["id"] for w in words]
        first = words[0]
        update_user(user_id, {
            "mode": "training",
            "session_word_ids": ids,
            "current_index": 0,
            "current_word_id": first["id"],
        })
        is_hard = first["id"] in user.get("hard_words", [])
        prefix = "Это слово было сложным. " if is_hard else ""
        return alice_resp(f"Начинаем! {prefix}Слово 1 из {len(words)}: {first['en']}.", session)

    # ── Старт повторения сложных ──
    if command in ["повторить", "повторить слова", "сложные слова"]:
        hard_ids = user.get("hard_words", [])
        if not hard_ids:
            return alice_resp("Сложных слов нет. Пройдите тренировку!", session,
                              buttons=["Тренировка"])
        all_words = get_all_words()
        hard_words = [w for w in all_words if w["id"] in hard_ids]
        random.shuffle(hard_words)
        hard_words = hard_words[:10]
        ids = [w["id"] for w in hard_words]
        first = hard_words[0]
        update_user(user_id, {
            "mode": "repeat_hard",
            "session_word_ids": ids,
            "current_index": 0,
            "current_word_id": first["id"],
        })
        return alice_resp(f"Повторяем сложные слова. {len(ids)} штук. Первое: {first['en']}.", session)

    # ── Проверка ответа ──
    if mode in ["training", "repeat_hard"]:
        if not command:
            return alice_resp("Повторите ответ, пожалуйста.", session)

        all_words = get_all_words()
        word_map = {w["id"]: w for w in all_words}
        current_id = user.get("current_word_id")
        current = word_map.get(current_id)

        if not current:
            update_user(user_id, {"mode": "menu"})
            return alice_resp("Что-то пошло не так. Начнём заново?", session,
                              buttons=["Тренировка"])

        correct = check_answer(command, current["ru"])
        hard_words = list(user.get("hard_words", []))

        if correct:
            if current_id in hard_words:
                hard_words.remove(current_id)
            update_user(user_id, {
                "total_correct": user.get("total_correct", 0) + 1,
                "hard_words": hard_words,
            })
            # Записать в статистику слова
            supabase.table("word_stats").upsert({
                "word_id": current_id,
                "correct": (current.get("correct", 0) or 0) + 1,
            }, on_conflict="word_id").execute()
            feedback = random.choice(["Верно! Отлично!", "Правильно! Молодец!", "Так точно!"])
        else:
            if current_id not in hard_words:
                hard_words.append(current_id)
            update_user(user_id, {
                "total_incorrect": user.get("total_incorrect", 0) + 1,
                "hard_words": hard_words,
            })
            feedback = f"Неверно. «{current['en']}» — это «{current['ru']}». Запомним!"

        # Следующее слово
        ids = user.get("session_word_ids", [])
        next_idx = user.get("current_index", 0) + 1

        if next_idx >= len(ids):
            update_user(user_id, {"mode": "menu", "hard_words": hard_words})
            hard_count = len(hard_words)
            msg = f" Сложных слов накопилось: {hard_count}." if hard_count else " Сложных слов нет — отлично!"
            return alice_resp(f"{feedback} Тренировка завершена!{msg}",
                              session, buttons=["Тренировка", "Повторить", "Статистика"])

        next_word = word_map.get(ids[next_idx])
        is_hard = ids[next_idx] in hard_words
        hard_prefix = "Это слово было сложным. " if is_hard else ""
        update_user(user_id, {
            "current_index": next_idx,
            "current_word_id": ids[next_idx],
            "hard_words": hard_words,
        })
        return alice_resp(f"{feedback} Слово {next_idx + 1} из {len(ids)}: {hard_prefix}{next_word['en']}.", session)

    return alice_resp("Не понял. Скажите «тренировка» или «повторить».", session,
                      buttons=["Тренировка", "Повторить"])


# ─── Веб-дашборд API ───────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return send_from_directory("static", "index.html")

@app.route("/api/words", methods=["GET"])
def api_get_words():
    words = get_all_words()
    return jsonify(words)

@app.route("/api/words", methods=["POST"])
def api_add_word():
    body = request.json
    en = body.get("en", "").strip()
    ru = body.get("ru", "").strip()
    if not en or not ru:
        return jsonify({"error": "Нужны оба поля"}), 400
    res = supabase.table("words").insert({"en": en, "ru": ru}).execute()
    return jsonify(res.data[0]), 201

@app.route("/api/words/<int:word_id>", methods=["DELETE"])
def api_delete_word(word_id):
    supabase.table("words").delete().eq("id", word_id).execute()
    return jsonify({"ok": True})

@app.route("/api/words/<int:word_id>", methods=["PUT"])
def api_update_word(word_id):
    body = request.json
    supabase.table("words").update({
        "en": body.get("en"),
        "ru": body.get("ru"),
    }).eq("id", word_id).execute()
    return jsonify({"ok": True})

@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Общая статистика по всем словам + прогресс пользователей."""
    words = get_all_words()
    users = supabase.table("user_progress").select("*").execute().data or []

    total_correct = sum(u.get("total_correct", 0) for u in users)
    total_incorrect = sum(u.get("total_incorrect", 0) for u in users)
    total_sessions = sum(u.get("sessions", 0) for u in users)

    # Топ сложных слов (по количеству пользователей у которых оно в hard_words)
    hard_count = {}
    for u in users:
        for wid in u.get("hard_words", []):
            hard_count[wid] = hard_count.get(wid, 0) + 1

    word_map = {w["id"]: w for w in words}
    hard_words_list = sorted(
        [{"word": word_map[wid], "count": cnt}
         for wid, cnt in hard_count.items() if wid in word_map],
        key=lambda x: -x["count"]
    )[:10]

    return jsonify({
        "total_words": len(words),
        "total_correct": total_correct,
        "total_incorrect": total_incorrect,
        "total_sessions": total_sessions,
        "accuracy": round(total_correct / (total_correct + total_incorrect) * 100)
                    if (total_correct + total_incorrect) else 0,
        "hard_words": hard_words_list,
        "users_count": len(users),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
