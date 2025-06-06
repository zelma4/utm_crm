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
    Зчитує JSON‐файл tokens.json, який має структуру:
    {
      "access_token": "xxx",
      "refresh_token": "yyy"
    }
    """
    if not os.path.exists(TOKENS_PATH):
        print(f"❌ Файл {TOKENS_PATH} не знайдено")
        raise FileNotFoundError(f"{TOKENS_PATH} not found")
    with open(TOKENS_PATH, "r") as f:
        return json.load(f)


def write_tokens(new_access, new_refresh):
    """
    Перезаписує токени у файл tokens.json.
    """
    data = {
        "access_token":  new_access,
        "refresh_token": new_refresh
    }
    with open(TOKENS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def refresh_access_token():
    """
    Оновлює access_token через refresh_token.
    Повертає новий access_token.
    """
    tokens = read_tokens()
    payload = {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "redirect_uri":  REDIRECT_URI
    }
    url = f"{AMO_DOMAIN}/oauth2/access_token"
    resp = requests.post(url, json=payload)
    if resp.status_code != 200:
        print(f"❌ Помилка при оновленні токена: {resp.status_code} {resp.text}")
        raise Exception("Не вдалося оновити access_token")
    data = resp.json()
    new_access  = data["access_token"]
    new_refresh = data["refresh_token"]
    write_tokens(new_access, new_refresh)
    print("✅ Access token успішно оновлено")
    return new_access


def find_lead_id_by_email_or_phone(email, phone, access_token):
    """
    Шукає контакт у Kommo за email чи phone, та повертає перший lead_id, якщо він є.
    Якщо повернувся 204 → означає “контакт/лід ще не створені” – повертаємо (None, 204).
    Якщо 401 → токен протух.
    Якщо інший ≠200 і ≠204 → помилка.
    """
    url = f"{AMO_DOMAIN}/api/v4/contacts"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    # Якщо є email – шукаємо по email, інакше – по телефону
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
        # Хоча статус 200, але масив контактів порожній → лід поки що не створено
        return None, 204

    first = contacts[0]
    leads = first.get("_embedded", {}).get("leads", [])
    if not leads:
        # Контакт є, але лід ще не створено
        return None, 204

    # Повертаємо ID першого ліда
    return leads[0]["id"], 200


def update_lead_utms(lead_id, hidden_fields, access_token):
    """
    Оновлює кастомні UTM-поля для ліда.
    Повертає (True, статус) або (False, статус).
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

    # Зчитаємо поточний access_token
    tokens = read_tokens()
    access_token = tokens.get("access_token")

    # ▪ Polling: чекатимемо, поки Kommo створить контакт + лід (до 10 спроб, кожна з 2-секундною паузою)
    lead_id = None
    for attempt in range(10):
        lead_id, status_code = find_lead_id_by_email_or_phone(email, phone, access_token)

        if status_code == 401:
            # токен протух – оновимо й одразу продовжимо спроби з новим токеном
            access_token = refresh_access_token()
            continue

        if status_code not in (200, 204):
            # будь‐яка інша помилка – повернемо 500 Internal Server Error
            return f"❌ Помилка пошуку контакту/ліда: {status_code}", 500

        if lead_id:
            # знайшли lead_id – можемо вийти з циклу
            break

        # якщо status_code == 204 → лід ще не створено, робимо паузу перед наступною спробою
        time.sleep(2)

    if not lead_id:
        # Після 10 спроб нічого не знайшли
        return "❌ Лід не знайдено після очікування", 404

    # ▪ Оновити знайдений лід, додаємо UTM
    success, upd_status = update_lead_utms(lead_id, hidden, access_token)
    if not success and upd_status == 401:
        # access_token протух під час PATCH → оновлюємо токен і повторюємо оновлення
        access_token = refresh_access_token()
        success, upd_status = update_lead_utms(lead_id, hidden, access_token)

    if not success:
        return f"❌ Не вдалося оновити ліда (status={upd_status})", 500

    return "✅ Lead успішно оновлено", 200


if __name__ == "__main__":
    # Локальний запуск на порту 3000
    app.run(host="0.0.0.0", port=3000)