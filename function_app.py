import azure.functions as func
import logging
import os
import json
import re
import firebase_admin
from firebase_admin import firestore, credentials
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from uuid import uuid4

from google import genai


app = func.FunctionApp()

# Firebase
cred = credentials.Certificate("firebase-service-account.json")
firebase_admin.initialize_app(cred)

db = firestore.client()

# Time zone
SYDNEY_TZ = ZoneInfo("Australia/Sydney")


# Gemini
gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
BOT_NAME = os.getenv("BOT_NAME", "Gemini 🤖")
DEFAULT_GUESS = int(os.getenv("DEFAULT_GUESS", "90"))


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

    orders = db.collection("orders").stream()

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
    return today, db.collection("coffeeGuesses").document(today.isoformat())


def ensure_daily_round_open():
    today, doc_ref = get_today_round_ref()
    date_key = today.isoformat()
    transaction = db.transaction()

    @firestore.transactional
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
    transaction = db.transaction()

    @firestore.transactional
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
                logging.info(f"Guess document {date_key} is not open. Skipping bot entry.")
                return False

            entries = data.get("entries", [])

            if any(item.get("name") == BOT_NAME for item in entries):
                logging.info(f"{BOT_NAME} already has an entry for {date_key}. Skipping duplicate.")
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
        logging.info(f"No historical data available. Using default guess: {DEFAULT_GUESS}")
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

    logging.info("Sending prediction request to Gemini.")

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    result = response.text.strip()

    logging.info(f"AI raw response: {result}")

    match = re.search(r"\d+", result)

    if match:
        return int(match.group())

    logging.error(f"AI returned an invalid guess: {result}")
    return DEFAULT_GUESS


@app.timer_trigger(
    # Runs once per day. Configure WEBSITE_TIME_ZONE=Australia/Sydney in Azure for Sydney local time.
    schedule="%COFFEE_BOT_SCHEDULE%",
    arg_name="timer",
)
def coffee_bot(timer: func.TimerRequest):
    logging.info("Coffee bot triggered.")

    try:
        # 1. Ensure today's prediction round exists
        created = ensure_daily_round_open()
        logging.info(f"Daily round created: {created}")

        if has_bot_entry_for_today():
            logging.info(f"{BOT_NAME} already entered today. Skipping prediction.")
            return

        # 2. Read orders from Firebase
        historical_data = get_daily_order_counts()

        logging.info(
            f"Historical data: {historical_data}"
        )

        # 3. Ask Gemini for today's prediction
        ai_guess = get_ai_guess(historical_data)

        logging.info(
            f"AI coffee order guess: {ai_guess}"
        )

        if ai_guess is not None:
            added = add_ai_guess_entry(ai_guess)
            logging.info(f"AI guess entry added: {added}")
    except Exception:
        logging.exception("Coffee bot failed.")