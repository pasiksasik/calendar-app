from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_session import Session
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
    print("⚠️ WARNING: Google OAuth credentials not set. Google Calendar sync will be disabled.")
    GOOGLE_OAUTH_ENABLED = False
else:
    GOOGLE_OAUTH_ENABLED = True

SCOPES = ["https://www.googleapis.com/auth/calendar"]
EVENTS_DIR = "user_events"
SESSION_DIR = "flask_session"  # Директория для хранения сессий

# Google Calendar color mapping
GOOGLE_COLOR_MAP = {
    "#4285f4": "1",  # Blue
    "#dc2127": "11",  # Red
    "#f4b400": "5",  # Yellow
    "#0f9d58": "10",  # Green
    "#ff6d00": "6",  # Orange
    "#7986cb": "9",  # Lavender
    "#33b679": "2",  # Sage
    "#8e24aa": "3",  # Grape
    "#e67c73": "4",  # Flamingo
    "#616161": "8",  # Graphite
}

# ================== APP ==================

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = SESSION_DIR
app.config['SESSION_COOKIE_NAME'] = 'calendar_session'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = bool(os.getenv("RENDER"))  # True только на HTTPS
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # Сессия живет 30 дней

# Инициализируем Flask-Session
Session(app)

if not os.getenv("RENDER"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

os.makedirs(EVENTS_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)


# ================== MIDDLEWARE ==================

@app.before_request
def make_session_permanent():
    """Делаем сессию постоянной для залогиненных пользователей"""
    if "credentials" in session:
        session.permanent = True
    elif session.get("guest_mode"):
        session.permanent = False  # Гостевая сессия не постоянная


# ================== HELPERS ==================

def get_user_id():
    """Получить ID пользователя из сессии"""
    if "credentials" not in session:
        return None

    creds = session["credentials"]
    user_token = creds.get("token", "")
    if user_token:
        return user_token[:16]
    return None


def get_user_events_file():
    """Получить путь к файлу событий пользователя"""
    user_id = get_user_id()
    if not user_id:
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
    if not GOOGLE_OAUTH_ENABLED:
        return None

    if os.getenv("RENDER"):
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


def get_google_calendar_events(days_ahead=14):
    """Получить события из Google Calendar на ближайшие дни"""
    try:
        service = get_calendar_service()
        if not service:
            return []

        now = datetime.utcnow()
        time_min = now.isoformat() + 'Z'
        time_max = (now + timedelta(days=days_ahead)).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            maxResults=100,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        formatted_events = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))

            formatted_events.append({
                'title': event.get('summary', 'Bez tytułu'),
                'start': start,
                'end': end,
                'description': event.get('description', '')
            })

        return formatted_events
    except Exception as e:
        print(f"Error fetching Google Calendar events: {e}")
        return []


def parse_google_event_to_local(google_event):
    """Конвертировать событие из Google Calendar в локальный формат"""
    try:
        start = google_event['start']
        end = google_event['end']

        # Parse datetime
        if 'dateTime' in start:
            start_dt = datetime.fromisoformat(start['dateTime'].replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(end['dateTime'].replace('Z', '+00:00'))

            date = start_dt.strftime('%Y-%m-%d')
            time = start_dt.strftime('%H:%M')
            duration = int((end_dt - start_dt).total_seconds() / 60)
        else:
            # All-day event
            date = start['date']
            time = '00:00'
            duration = 1440  # Full day

        # Get color
        color_id = google_event.get('colorId', '1')
        color_map_reverse = {v: k for k, v in GOOGLE_COLOR_MAP.items()}
        color = color_map_reverse.get(color_id, '#4285f4')

        return {
            'title': google_event.get('summary', 'Bez tytułu'),
            'date': date,
            'time': time,
            'duration': duration,
            'description': google_event.get('description', ''),
            'color': color,
            'imported_from_google': True  # Маркер импортированного события
        }
    except Exception as e:
        print(f"Error parsing Google event: {e}")
        return None


# ================== ROUTES ==================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/guest")
def guest_mode():
    """Вход как гость"""
    session.clear()  # Очищаем любую предыдущую сессию
    session["guest_mode"] = True
    session.permanent = False  # Гостевая сессия НЕ постоянная (закроется при закрытии браузера)
    return redirect("/?mode=guest")


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

    # Добавляем информацию о дне недели
    weekday_names_pl = ['poniedziałek', 'wtorek', 'środa', 'czwartek', 'piątek', 'sobota', 'niedziela']
    weekday_names_ru = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
    current_weekday = now.weekday()  # 0 = Monday
    today_name_pl = weekday_names_pl[current_weekday]
    today_name_ru = weekday_names_ru[current_weekday]

    # Вычисляем ближайшую среду для примера
    days_until_wednesday = (2 - current_weekday) % 7
    if days_until_wednesday == 0:
        days_until_wednesday = 7
    next_wednesday = (now + timedelta(days=days_until_wednesday)).strftime('%Y-%m-%d')

    existing_events = load_events()
    google_events = get_google_calendar_events()

    events_context = "Istniejące wydarzenia użytkownika:\n"
    for evt in existing_events:
        events_context += f"- {evt['date']} {evt.get('time', '')} - {evt['title']} ({evt.get('duration', 60)} minut)\n"

    for gevt in google_events:
        events_context += f"- {gevt['start']} do {gevt['end']} - {gevt['title']}\n"

    # Генерируем список ближайших 14 дней
    next_days_list = []
    for i in range(1, 15):
        future_date = now + timedelta(days=i)
        future_weekday = future_date.weekday()
        date_str = future_date.strftime('%Y-%m-%d')
        next_days_list.append(f"{date_str} - {weekday_names_pl[future_weekday]} ({weekday_names_ru[future_weekday]})")

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
                "max_tokens": 1500,
                "messages": [
                    {
                        "role": "user",
                        "content": f"""
Jesteś inteligentnym asystentem kalendarza. Analizujesz opisy wydarzeń i proponujesz najlepsze terminy.

WAŻNE - INFORMACJE O DZISIEJSZEJ DACIE:
Dzisiaj: {today_str}
Dzień tygodnia: {today_name_pl} (po polsku) / {today_name_ru} (по-русски)
Aktualna godzina: {current_time}

Najbliższe dni (UŻYJ DOKŁADNIE TYCH DAT):
{chr(10).join(next_days_list)}

{events_context}

ZADANIE:
Użytkownik chce dodać: "{description}"

KRYTYCZNIE WAŻNE - DNI TYGODNIA:
- Jeśli użytkownik pisze "jutro" / "завтра" → użyj daty {(now + timedelta(days=1)).strftime('%Y-%m-%d')}
- Jeśli użytkownik pisze "pojutrze" / "послезавтра" → użyj daty {(now + timedelta(days=2)).strftime('%Y-%m-%d')}
- Jeśli użytkownik pisze dzień tygodnia (np. "w środę", "в среду"), KONIECZNIE znajdź NAJBLIŻSZĄ datę tego dnia z listy powyżej
- PRZYKŁAD KONKRETNY: Dziś jest {today_name_pl} {today_str}. Jeśli użytkownik napisze "w środę" lub "w środę o 7 rano", musisz użyć daty środy z listy powyżej (to jest {next_wednesday})

UWAGA: NIE wymyślaj dat! TYLKO daty z listy "Najbliższe dni"!

ZASADY:
1. Użytkownik może pisać po polsku lub po rosyjsku - zrozum obie języki
2. ZAWSZE używaj DOKŁADNYCH dat z listy "Najbliższe dni" powyżej - NIE wymyślaj własnych dat
3. Jeśli użytkownik podaje konkretny termin, sprawdź czy nie koliduje z istniejącymi wydarzeniami
4. Jeśli jest konflikt, zaproponuj 3 alternatywne terminy
5. Jeśli użytkownik NIE podaje terminu, zaproponuj 3 najlepsze wolne terminy w ciągu najbliższych 7 dni
6. Uwzględnij rozsądne godziny (8:00-20:00)
7. Unikaj weekendów dla wydarzeń zawodowych
8. Zostawiaj przerwy między wydarzeniami (min 30 minut)

ODPOWIEDŹ W FORMACIE JSON:
{{
  "requested_event": {{
    "title": "nazwa",
    "date": "YYYY-MM-DD",
    "time": "HH:MM",
    "duration": liczba_minut,
    "description": "opis",
    "has_conflict": true/false
  }},
  "suggestions": [
    {{
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "duration": liczba_minut,
      "reason": "dlaczego ten termin jest dobry"
    }},
    // ... 2 więcej sugestii
  ]
}}

Zwróć TYLKO JSON, bez dodatkowego tekstu.
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

        event_data = json.loads(match.group(0))

        if "requested_event" in event_data:
            evt = event_data["requested_event"]
            evt["time"] = evt.get("time") or "09:00"
            evt["duration"] = max(int(evt.get("duration", 60)), 15)

        if "suggestions" not in event_data:
            event_data["suggestions"] = []

        return jsonify(event_data)

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

    # Ensure color is saved
    if 'color' not in data:
        data['color'] = '#4285f4'

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


# ================== GOOGLE CALENDAR IMPORT ==================

@app.route("/google/import", methods=["GET"])
def import_google_calendar():
    """Получить события из Google Calendar (только для отображения)"""
    try:
        google_events = get_google_calendar_events(days_ahead=30)
        return jsonify({
            "success": True,
            "events": google_events,
            "count": len(google_events)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/google/import-to-local", methods=["POST"])
def import_google_to_local():
    """Импортировать события из Google Calendar в локальное хранилище"""
    try:
        service = get_calendar_service()
        if not service:
            return jsonify({"auth_required": True}), 401

        # Get events from Google Calendar (90 days ahead)
        now = datetime.utcnow()
        time_min = now.isoformat() + 'Z'
        time_max = (now + timedelta(days=90)).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            maxResults=100,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        google_events = events_result.get('items', [])

        # Load existing local events
        local_events = load_events()

        # Convert and add Google events
        imported_count = 0
        for g_event in google_events:
            local_event = parse_google_event_to_local(g_event)
            if local_event:
                # Check if event already exists
                exists = any(
                    e['title'] == local_event['title'] and
                    e['date'] == local_event['date'] and
                    e['time'] == local_event['time']
                    for e in local_events
                )

                if not exists:
                    local_events.append(local_event)
                    imported_count += 1

        save_events(local_events)

        return jsonify({
            "success": True,
            "count": imported_count
        })

    except Exception as e:
        print(f"Import to local error: {e}")
        return jsonify({"error": str(e)}), 500


# ================== GOOGLE OAUTH ==================

@app.route("/google/login")
def google_login():
    if not GOOGLE_OAUTH_ENABLED:
        return jsonify({"error": "Google OAuth not configured"}), 503

    flow = get_google_flow()

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    session["state"] = state
    session.permanent = True  # Устанавливаем постоянную сессию сразу
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    if not GOOGLE_OAUTH_ENABLED:
        return redirect("/?auth=error")

    try:
        flow = get_google_flow()

        if "state" not in session:
            print("🔥 No state in session")
            return redirect("/?auth=error")

        flow.state = session["state"]
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
        session["guest_mode"] = False
        session.permanent = True  # Делаем сессию постоянной для залогиненных пользователей

        return redirect("/?auth=success")

    except Exception as e:
        print(f"🔥 OAuth callback error: {e}")
        return redirect("/?auth=error")


@app.route("/google/sync", methods=["POST"])
def google_sync():
    if not GOOGLE_OAUTH_ENABLED:
        return jsonify({"error": "Google OAuth not configured"}), 503

    if "credentials" not in session:
        return jsonify({"auth_required": True}), 401

    try:
        service = get_calendar_service()
        if not service:
            return jsonify({"auth_required": True}), 401

        events = load_events()

        synced = 0
        skipped = 0

        for e in events:
            # Пропускаем события, которые были импортированы из Google Calendar
            if e.get('imported_from_google', False):
                skipped += 1
                print(f"Skipping imported event: {e['title']}")
                continue

            # Parse date and time in local timezone
            start = datetime.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %H:%M")
            end = start + timedelta(minutes=e["duration"])

            # Get Google Calendar color ID
            color = e.get('color', '#4285f4')
            color_id = GOOGLE_COLOR_MAP.get(color, '1')

            body = {
                "summary": e["title"],
                "description": e.get("description", ""),
                "start": {
                    "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "Europe/Warsaw"
                },
                "end": {
                    "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "Europe/Warsaw"
                },
                "colorId": color_id
            }

            service.events().insert(calendarId="primary", body=body).execute()
            synced += 1
            print(f"Synced event: {e['title']} at {start}")

        # Удаляем только НЕ импортированные события
        remaining_events = [e for e in events if e.get('imported_from_google', False)]
        save_events(remaining_events)

        return jsonify({
            "success": True,
            "synced": synced
        })

    except HttpError as e:
        print(f"Google API error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/google/status")
def google_status():
    return jsonify({
        "authenticated": "credentials" in session,
        "oauth_enabled": GOOGLE_OAUTH_ENABLED,
        "guest_mode": session.get("guest_mode", False)
    })


@app.route("/google/logout")
def google_logout():
    user_id = get_user_id()

    # Удаляем файл событий залогиненного пользователя (опционально)
    if user_id:
        user_file = os.path.join(EVENTS_DIR, f"user_{user_id}.json")
        if os.path.exists(user_file):
            try:
                # Раскомментируйте следующую строку, если хотите удалять данные при выходе
                # os.remove(user_file)
                pass
            except Exception as e:
                print(f"Error deleting user file: {e}")

    # Удаляем гостевой файл только если был гость
    if session.get("guest_mode") and "session_id" in session:
        guest_file = os.path.join(EVENTS_DIR, f"guest_{session['session_id']}.json")
        if os.path.exists(guest_file):
            try:
                os.remove(guest_file)
            except Exception as e:
                print(f"Error deleting guest file: {e}")

    session.clear()
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
            h1 { color: #4A90E2; }
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
        <p>W przypadku pytań dotyczących tej polityki prywatności, skontaktuj się: stanislavozhiltsov@gmail.com</p>
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
    print(f"🔑 Google OAuth: {'Enabled' if GOOGLE_OAUTH_ENABLED else 'Disabled'}")
    app.run(host="0.0.0.0", port=port, debug=False)