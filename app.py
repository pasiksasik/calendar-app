from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import requests
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import json
import re

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ================== CONFIG ==================

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "change-me")

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY is not set")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise RuntimeError("Google OAuth credentials not set")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
EVENTS_DIR = "user_events"

# ================== APP ==================

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# OAuth - только для локальной разработки
if not os.getenv("RENDER"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Создаем папку для событий пользователей
os.makedirs(EVENTS_DIR, exist_ok=True)


# ================== HELPERS ==================

def get_user_id():
    """Получить ID пользователя из сессии"""
    if "credentials" not in session:
        return None

    # Используем часть токена как уникальный ID
    creds = session["credentials"]
    user_token = creds.get("token", "")
    if user_token:
        # Берем первые 16 символов токена как ID
        return user_token[:16]
    return None


def get_user_events_file():
    """Получить путь к файлу событий пользователя"""
    user_id = get_user_id()
    if not user_id:
        # Если пользователь не авторизован, используем временный файл сессии
        session_id = session.get("session_id")
        if not session_id:
            import secrets
            session_id = secrets.token_hex(8)
            session["session_id"] = session_id
        return os.path.join(EVENTS_DIR, f"guest_{session_id}.json")

    return os.path.join(EVENTS_DIR, f"user_{user_id}.json")


def load_events():
    """Загрузить события текущего пользователя"""
    events_file = get_user_events_file()
    if os.path.exists(events_file):
        try:
            with open(events_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Load events error for {events_file}:", e)
    return []


def save_events(data):
    """Сохранить события текущего пользователя"""
    events_file = get_user_events_file()
    try:
        with open(events_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Save events error for {events_file}:", e)


def get_google_flow():
    # Автоматически определяем окружение
    if os.getenv("RENDER"):
        # ВАЖНО: Должно совпадать с Google Console
        redirect_uri = "https://calendar-app-slle.onrender.com/oauth2callback"
    else:
        redirect_uri = "http://127.0.0.1:5000/oauth2callback"

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }

    return Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def get_calendar_service():
    if "credentials" not in session:
        return None
    creds = Credentials(**session["credentials"])
    return build("calendar", "v3", credentials=creds)


# ================== ROUTES ==================

@app.route("/")
def home():
    return render_template("index.html")


# ================== AI ANALYZE ==================

@app.route("/analyze", methods=["POST"])
def analyze_event():
    data = request.json or {}
    description = data.get("description", "").strip()

    if not description:
        return jsonify({"error": "Opis nie może być pusty"}), 400

    now = datetime.now()
    current_time = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "messages": [
                    {
                        "role": "user",
                        "content": f"""
Jesteś asystentem kalendarza.
Dzisiaj: {today_str}
Godzina: {current_time}

Zwróć TYLKO JSON:
{{"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration": liczba_minut, "description": "..."}}

Opis wydarzenia:
"{description}"
"""
                    }
                ],
            },
            timeout=30,
        )

        result = response.json()
        text = result["content"][0]["text"]

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return jsonify({"error": "Invalid JSON from AI"}), 500

        event = json.loads(match.group(0))

        # safety
        event["time"] = event.get("time") or "09:00"
        event["duration"] = max(int(event.get("duration", 60)), 15)

        return jsonify(event)

    except Exception as e:
        print(f"AI analyze error: {e}")
        return jsonify({"error": str(e)}), 500


# ================== EVENTS ==================

@app.route("/events", methods=["GET"])
def get_events():
    events = load_events()
    return jsonify(events)


@app.route("/events", methods=["POST"])
def add_event():
    events = load_events()
    data = request.json
    events.append(data)
    save_events(events)
    return jsonify({"success": True})


@app.route("/events/<int:index>", methods=["DELETE"])
def delete_event(index):
    events = load_events()
    if 0 <= index < len(events):
        events.pop(index)
        save_events(events)
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404


# ================== GOOGLE OAUTH ==================

@app.route("/google/login")
def google_login():
    flow = get_google_flow()

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    session["state"] = state
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    try:
        flow = get_google_flow()

        # Проверяем state
        if "state" not in session:
            print("🔥 No state in session")
            return redirect("/?auth=error")

        flow.state = session["state"]

        # Получаем токены
        flow.fetch_token(authorization_response=request.url)

        creds = flow.credentials
        session["credentials"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }

        return redirect("/?auth=success")

    except Exception as e:
        print(f"🔥 OAuth callback error: {e}")
        return redirect("/?auth=error")


@app.route("/google/sync", methods=["POST"])
def google_sync():
    if "credentials" not in session:
        return jsonify({"auth_required": True}), 401

    try:
        service = get_calendar_service()
        if not service:
            return jsonify({"auth_required": True}), 401

        # Загружаем события текущего пользователя
        events = load_events()
        synced = 0

        for e in events:
            start = datetime.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %H:%M")
            end = start + timedelta(minutes=e["duration"])

            body = {
                "summary": e["title"],
                "description": e.get("description", ""),
                "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Warsaw"},
                "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Warsaw"},
            }

            service.events().insert(calendarId="primary", body=body).execute()
            synced += 1

        return jsonify({"success": True, "synced": synced})

    except HttpError as e:
        print(f"Google API error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/google/status")
def google_status():
    return jsonify({"authenticated": "credentials" in session})


@app.route("/google/logout")
def google_logout():
    # Удаляем файл событий гостевой сессии если был
    if "session_id" in session:
        guest_file = os.path.join(EVENTS_DIR, f"guest_{session['session_id']}.json")
        if os.path.exists(guest_file):
            try:
                os.remove(guest_file)
            except Exception as e:
                print(f"Error deleting guest file: {e}")

    session.pop("credentials", None)
    session.pop("state", None)
    session.pop("session_id", None)
    return redirect("/")


@app.route("/privacy")
def privacy():
    return """
    <!DOCTYPE html>
    <html lang="pl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Polityka Prywatności - Calendar Assistant AI</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                line-height: 1.6;
            }
            h1 { color: #667eea; }
            h2 { color: #495057; margin-top: 30px; }
        </style>
    </head>
    <body>
        <h1>Polityka Prywatności</h1>
        <p><strong>Ostatnia aktualizacja: 18 stycznia 2026</strong></p>

        <h2>Jakie dane zbieramy</h2>
        <p>Calendar Assistant AI ma dostęp do Twojego Google Calendar tylko w celu dodawania wydarzeń, które sam utworzysz za pośrednictwem naszej usługi.</p>

        <h2>Jak wykorzystujemy Twoje dane</h2>
        <p>Używamy Google Calendar API do:</p>
        <ul>
            <li>Dodawania wydarzeń do Google Calendar, które tworzysz w naszej aplikacji</li>
            <li>Żadne dane nie są przechowywane na naszych serwerach</li>
            <li>Żadne dane nie są udostępniane osobom trzecim</li>
        </ul>

        <h2>Przechowywanie danych</h2>
        <p>Nie przechowujemy żadnych danych z Twojego Google Calendar. Wszystkie operacje odbywają się w czasie rzeczywistym.</p>

        <h2>Kontakt</h2>
        <p>W przypadku pytań dotyczących tej polityki prywatności, skontaktuj się: stasikjeschkov@gmail.com</p>
    </body>
    </html>
    """


# ================== RUN ==================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Calendar Assistant AI")
    print(f"🌍 Running on port {port}")
    print(f"🔐 HTTPS only: {os.getenv('RENDER') is not None}")
    print(f"📁 Events directory: {EVENTS_DIR}")
    app.run(host="0.0.0.0", port=port, debug=False)