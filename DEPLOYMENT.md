# Coffee Bot Azure Function Deployment

## Required files

These files are needed for deployment:

- `function_app.py`
- `requirements.txt`
- `host.json`

Firebase credentials are provided through Azure app settings.

Do not commit secrets:

- `local.settings.json`
- `firebase-service-account.json`

They are already ignored by `.gitignore`.

## Azure app settings

Set these on the Function App:

```bash
az functionapp config appsettings set \
  --name <FUNCTION_APP_NAME> \
  --resource-group <RESOURCE_GROUP_NAME> \
  --settings \
  GEMINI_API_KEY="<YOUR_GEMINI_API_KEY>" \
  FIREBASE_SERVICE_ACCOUNT_BASE64="<BASE64_ENCODED_FIREBASE_SERVICE_ACCOUNT_JSON>" \
  GEMINI_MODEL="gemini-3.5-flash-lite" \
  BOT_NAME="Gemini 🤖" \
  DEFAULT_GUESS="90" \
  SKIP_PUBLIC_HOLIDAYS="true" \
  PUBLIC_HOLIDAY_COUNTRY="AU" \
  PUBLIC_HOLIDAY_SUBDIV="ACT" \
  COFFEE_BOT_SCHEDULE="0 0 3 * * 1-5" \
  WEBSITE_TIME_ZONE="Australia/Sydney"
```

`COFFEE_BOT_SCHEDULE=0 0 3 * * 1-5` means Monday to Friday at 03:00 if the Function App timezone is Sydney.

## Deploy

```bash
func azure functionapp publish <FUNCTION_APP_NAME>
```

## Logs

```bash
func azure functionapp logstream <FUNCTION_APP_NAME>
```

## Notes

- The bot creates today's `coffeeGuesses/{dateKey}` document if it does not exist.
- It only adds one `Gemini 🤖` entry per Sydney date.
- If order history is empty, it uses `DEFAULT_GUESS`.
- Firebase credentials are read from `FIREBASE_SERVICE_ACCOUNT_BASE64` first, then `FIREBASE_SERVICE_ACCOUNT_JSON`, then local `firebase-service-account.json` as a fallback.
- For GitHub/Azure deployments, set `FIREBASE_SERVICE_ACCOUNT_BASE64` in Azure Function App settings.
- Public holidays are skipped when `SKIP_PUBLIC_HOLIDAYS=true`. Defaults are Australia / ACT (`PUBLIC_HOLIDAY_COUNTRY=AU`, `PUBLIC_HOLIDAY_SUBDIV=ACT`).
