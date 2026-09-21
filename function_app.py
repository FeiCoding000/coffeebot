import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import azure.functions as func
import firebase_admin
import holidays
from firebase_admin import credentials, firestore
from google import genai
from google.cloud import firestore as google_firestore

logger = logging.getLogger(__name__)

app = func.FunctionApp()

# Firebase
FIREBASE_SERVICE_ACCOUNT_FILE = "firebase-service-account.json"


def get_firebase_credential():
    service_account_base64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_BASE64")
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

    if service_account_base64:
        service_account_info = json.loads(
            base64.b64decode(service_account_base64).decode("utf-8")
        )
        return credentials.Certificate(service_account_info)

    if service_account_json:
        return credentials.Certificate(json.loads(service_account_json))

    return credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)


_db = None
_gemini_client = None


def get_db():
    global _db

    if _db is None:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(get_firebase_credential())
        _db = firestore.client()

    return _db

# Time zone
SYDNEY_TZ = ZoneInfo("Australia/Sydney")


# Gemini
def get_gemini_client():
    global _gemini_client

    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    return _gemini_client


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
BOT_NAME = os.getenv("BOT_NAME", "Gemini 🤖")
DEFAULT_GUESS = int(os.getenv("DEFAULT_GUESS", "90"))
SKIP_PUBLIC_HOLIDAYS = os.getenv("SKIP_PUBLIC_HOLIDAYS", "true").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
PUBLIC_HOLIDAY_COUNTRY = os.getenv("PUBLIC_HOLIDAY_COUNTRY", "AU")
PUBLIC_HOLIDAY_SUBDIV = os.getenv("PUBLIC_HOLIDAY_SUBDIV", "ACT")


def is_public_holiday(target_date):
    if not SKIP_PUBLIC_HOLIDAYS:
        return False

    holiday_calendar = holidays.country_holidays(
        PUBLIC_HOLIDAY_COUNTRY,
        subdiv=PUBLIC_HOLIDAY_SUBDIV,
        years=[target_date.year],
    )
    holiday_name = holiday_calendar.get(target_date)

    if holiday_name:
        logger.info(
            "Skipping coffee bot on public holiday %s: %s",
            target_date.isoformat(),
            holiday_name,
        )
        return True

    return False


def process_order(doc):
    order = doc.to_dict()

    # Only count completed orders
    if not order.get("isCompleted"):
        return None

    created_at = order.get("createdAt")

    if not created_at:
        return None

    # Convert UTC timestamp to Sydney local date
    local_date = created_at.astimezone(SYDNEY_TZ).date()

    return local_date


def get_daily_order_counts():
    daily_counts = {}

    orders = get_db().collection("orders").stream()

    for doc in orders:
        order_date = process_order(doc)

        if order_date:
            daily_counts[order_date] = (
                daily_counts.get(order_date, 0) + 1
            )

    # Convert dates into AI-friendly JSON format
    historical_data = [
        {
            "date": date.isoformat(),
            "orders": count,
        }
        for date, count in sorted(daily_counts.items())
    ]

    return historical_data


def get_week_key(target_date):
    iso_year, iso_week, _ = target_date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def get_today_round_ref():
    today = datetime.now(SYDNEY_TZ).date()
    return today, get_db().collection("coffeeGuesses").document(today.isoformat())


def ensure_daily_round_open():
    today, doc_ref = get_today_round_ref()
    date_key = today.isoformat()
    transaction = get_db().transaction()

    @google_firestore.transactional
    def update_in_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)

        if snapshot.exists:
            data = snapshot.to_dict() or {}
            if data.get("status") is None:
                transaction.update(doc_ref, {"status": "open"})
            return False

        transaction.set(doc_ref, {
            "dateKey": date_key,
            "weekKey": get_week_key(today),
            "status": "open",
            "openedAt": datetime.now(timezone.utc),
            "entries": [],
        })
        return True

    return update_in_transaction(transaction, doc_ref)


def has_bot_entry_for_today():
    _, doc_ref = get_today_round_ref()
    snapshot = doc_ref.get()

    if not snapshot.exists:
        return False

    data = snapshot.to_dict() or {}
    entries = data.get("entries", [])
    return any(item.get("name") == BOT_NAME for item in entries)


def add_ai_guess_entry(guess):
    today, doc_ref = get_today_round_ref()
    date_key = today.isoformat()
    transaction = get_db().transaction()

    @google_firestore.transactional
    def update_in_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)

        entry = {
            "name": BOT_NAME,
            "guess": guess,
            "id": str(uuid4()),
            "createdAt": datetime.now(timezone.utc),
        }

        if snapshot.exists:
            data = snapshot.to_dict() or {}

            if data.get("status") and data.get("status") != "open":
                logger.info("Guess document %s is not open. Skipping bot entry.", date_key)
                return False

            entries = data.get("entries", [])

            if any(item.get("name") == BOT_NAME for item in entries):
                logger.info(
                    "%s already has an entry for %s. Skipping duplicate.",
                    BOT_NAME,
                    date_key,
                )
                return False

            transaction.update(doc_ref, {
                "status": "open",
                "entries": entries + [entry],
            })
        else:
            transaction.set(doc_ref, {
                "dateKey": date_key,
                "weekKey": get_week_key(today),
                "status": "open",
                "openedAt": datetime.now(timezone.utc),
                "entries": [entry],
            })

        return True

    return update_in_transaction(transaction, doc_ref)


def get_ai_guess(historical_data):
    today = datetime.now(SYDNEY_TZ).date()
    today_string = today.isoformat()

    # Do not give today's partial data to the AI
    historical_data = [
        item
        for item in historical_data
        if item["date"] < today_string
    ]

    if not historical_data:
        logger.info(
            "No historical data available. Using default guess: %s",
            DEFAULT_GUESS,
        )
        return DEFAULT_GUESS

    historical_context = json.dumps(historical_data, indent=2)

    prompt = f"""
You are playing the Scyne Coffee daily order prediction game.

Here is the historical number of completed coffee orders for each day:

{historical_context}

Today is {today_string}.

Predict the final total number of completed coffee orders for today.
Return ONLY one integer.
Do not include explanations.
Do not include words.
Do not use markdown.
"""

    logger.info("Sending prediction request to Gemini.")

    response = get_gemini_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    result = response.text.strip()

    logger.info("AI raw response: %s", result)

    match = re.search(r"\d+", result)

    if match:
        return int(match.group())

    logger.error("AI returned an invalid guess: %s", result)
    return DEFAULT_GUESS


@app.timer_trigger(
    # Runs once per day. Configure WEBSITE_TIME_ZONE=Australia/Sydney in Azure for Sydney local time.
    schedule="%COFFEE_BOT_SCHEDULE%",
    arg_name="timer",
)
def coffee_bot(timer: func.TimerRequest):
    logger.info("Coffee bot triggered.")

    try:
        today = datetime.now(SYDNEY_TZ).date()

        if is_public_holiday(today):
            return

        # 1. Ensure today's prediction round exists
        created = ensure_daily_round_open()
        logger.info("Daily round created: %s", created)

        if has_bot_entry_for_today():
            logger.info("%s already entered today. Skipping prediction.", BOT_NAME)
            return

        # 2. Read orders from Firebase
        historical_data = get_daily_order_counts()

        logger.info("Historical data: %s", historical_data)

        # 3. Ask Gemini for today's prediction
        ai_guess = get_ai_guess(historical_data)

        logger.info("AI coffee order guess: %s", ai_guess)

        if ai_guess is not None:
            added = add_ai_guess_entry(ai_guess)
            logger.info("AI guess entry added: %s", added)
    except Exception:
        logger.exception("Coffee bot failed.")
        raise