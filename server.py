import os
import time
import json
import requests
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
#                   ЗМІННІ СЕРЕДОВИЩА (ENVIRONMENT VARIABLES)
# ─────────────────────────────────────────────────────────────────────────────
CLIENT_ID        = os.getenv("AMO_CLIENT_ID")
CLIENT_SECRET    = os.getenv("AMO_CLIENT_SECRET")
REDIRECT_URI     = os.getenv("AMO_REDIRECT_URI")
AMO_DOMAIN       = os.getenv("AMO_DOMAIN")  # наприклад, "https://tcsavant.kommo.com"

CUSTOM_UTM_SOURCE    = int(os.getenv("AMO_CUSTOM_UTM_SOURCE"))
CUSTOM_UTM_MEDIUM    = int(os.getenv("AMO_CUSTOM_UTM_MEDIUM"))
CUSTOM_UTM_CAMPAIGN  = int(os.getenv("AMO_CUSTOM_UTM_CAMPAIGN"))
CUSTOM_UTM_CONTENT   = int(os.getenv("AMO_CUSTOM_UTM_CONTENT"))
CUSTOM_UTM_PLACEMENT = int(os.getenv("AMO_CUSTOM_UTM_PLACEMENT"))

TOKENS_PATH = os.getenv("TOKENS_PATH", "tokens.json")
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


def read_tokens():
    """
    Зчитує JSON-файл tokens.json, який має структуру:
    {
      "access_token": "довготривалий_токен"
    }
    (Поля refresh_token тепер не використовуються.)
    """
    if not os.path.exists(TOKENS_PATH):
        print(f"❌ Файл {TOKENS_PATH} не знайдено")
        raise FileNotFoundError(f"{TOKENS_PATH} not found")
    with open(TOKENS_PATH, "r") as f:
        data = json.load(f)

    access = data.get("access_token")
    if not access:
        raise KeyError("У tokens.json відсутнє поле access_token")
    return access


def find_lead_id_by_email_or_phone(email, phone, access_token):
    """
    Шукає контакт у Kommo за email чи phone, та повертає перший lead_id, якщо він є.
    Якщо повернувся 204 → означає “контакт/лід ще не створені” – повертаємо (None, 204).
    Якщо 401 → токен невалідний → повертаємо (None, 401).
    Якщо інший ≠200 і ≠204 → помилка (повертаємо (None, status_code)).
    """
    url = f"{AMO_DOMAIN}/api/v4/contacts"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    query_str = email if email else phone
    params = {
        "query": query_str,
        "with":  "leads"
    }
    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code == 401:
        return None, 401

    if resp.status_code == 204:
        # Контакт (чи лід) ще не створений у Kommo
        return None, 204

    if resp.status_code != 200:
        print(f"❌ Помилка пошуку контакту: {resp.status_code} {resp.text}")
        return None, resp.status_code

    data = resp.json()
    contacts = data.get("_embedded", {}).get("contacts", [])
    if not contacts:
        return None, 204

    first = contacts[0]
    leads = first.get("_embedded", {}).get("leads", [])
    if not leads:
        return None, 204

    return leads[0]["id"], 200


def update_lead_utms(lead_id, hidden_fields, access_token):
    """
    Оновлює кастомні UTM-поля для ліда.
    Повертає (True, status_code) або (False, status_code).
    """
    # Невелика затримка перед фактичним PATCH, щоб точно лід “відобразився”
    time.sleep(1)

    url = f"{AMO_DOMAIN}/api/v4/leads/{lead_id}"
    headers = {
        "Authorization":   f"Bearer {access_token}",
        "Content-Type":    "application/json"
    }
    payload = {
        "custom_fields_values": [
            {"field_id": CUSTOM_UTM_SOURCE,    "values": [{"value": hidden_fields.get("utm_source", "")}]},
            {"field_id": CUSTOM_UTM_MEDIUM,    "values": [{"value": hidden_fields.get("utm_medium", "")}]},
            {"field_id": CUSTOM_UTM_CAMPAIGN,  "values": [{"value": hidden_fields.get("utm_campaign", "")}]},
            {"field_id": CUSTOM_UTM_CONTENT,   "values": [{"value": hidden_fields.get("utm_content", "")}]},
            {"field_id": CUSTOM_UTM_PLACEMENT, "values": [{"value": hidden_fields.get("utm_placement", "")}]}
        ]
    }
    resp = requests.patch(url, json=payload, headers=headers)

    if resp.status_code == 401:
        # токен недійсний (може бути відкликаний) → повертаємо, щоб можна було зрозуміти причину
        return False, 401

    if resp.status_code not in (200, 204):
        print(f"❌ Помилка оновлення ліда ({lead_id}): {resp.status_code} {resp.text}")
        return False, resp.status_code

    return True, resp.status_code


@app.route("/webhook/typeform", methods=["POST"])
def webhook_typeform():
    """
    Обробник, у який Typeform надсилає Webhook.
    З payload витягуємо:
      - form_response.hidden  (утм‐поля)
      - form_response.answers (email та/або phone_number)
    Далі чекаємо (max 10 спроб по 2 секунди), поки Kommo створить лід → patch UTM.
    (Без жодного refresh_token: використовуємо лише довготривалий access_token.)
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return "❌ Неправильний JSON", 400

    form_resp = payload.get("form_response", {})
    if not form_resp:
        return "❌ Нема form_response", 400

    hidden  = form_resp.get("hidden", {}) or {}
    answers = form_resp.get("answers", []) or []

    # Витягнути email та телефон (якщо заповнив користувач)
    email = None
    phone = None
    for ans in answers:
        if ans.get("type") == "email":
            email = ans.get("email")
        if ans.get("type") == "phone_number":
            phone = ans.get("phone_number")

    if not email and not phone:
        return "❌ Не знайдено ні email, ні phone", 400

    # Зчитаємо access_token (long-lived) з файлу
    try:
        access_token = read_tokens()
    except Exception as e:
        print(f"❌ Помилка читання токена: {e}")
        return f"❌ Не знайдено або невірний access_token: {e}", 500

    # ▪ Polling: чекатимемо, поки Kommo створить контакт + лід (до 10 спроб, 2 с кожна)
    lead_id = None
    for attempt in range(10):
        lead_id, status_code = find_lead_id_by_email_or_phone(email, phone, access_token)

        if status_code == 401:
            # access_token недійсний (можливо, відкликаний) – повертаємо 401
            return "❌ Access token недійсний або відкликаний", 401

        if status_code not in (200, 204):
            return f"❌ Помилка пошуку контакту/ліда: {status_code}", 500

        if lead_id:
            break

        # якщо status_code == 204 → лід ще не створено, чекаємо
        time.sleep(2)

    if not lead_id:
        return "❌ Лід не знайдено після очікування", 404

    # ▪ Оновити знайдений лід, додаємо UTM
    success, upd_status = update_lead_utms(lead_id, hidden, access_token)
    if not success:
        if upd_status == 401:
            return "❌ Access token недійсний під час оновлення ліда", 401
        return f"❌ Не вдалося оновити ліда (status={upd_status})", 500

    return "✅ Lead успішно оновлено", 200


if __name__ == "__main__":
    # Локальний запуск на порту 3000
    app.run(host="0.0.0.0", port=3000)