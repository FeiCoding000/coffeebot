# Coffee Bot Azure Function Deployment

## Required files

These files are needed for deployment:

- `function_app.py`
- `requirements.txt`
- `host.json`
- `firebase-service-account.json`

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
  GEMINI_MODEL="gemini-3.5-flash-lite" \
  BOT_NAME="Gemini 🤖" \
  DEFAULT_GUESS="90" \
  COFFEE_BOT_SCHEDULE="0 0 3 * * *" \
  WEBSITE_TIME_ZONE="Australia/Sydney"
```

`COFFEE_BOT_SCHEDULE=0 0 3 * * *` means daily at 03:00 if the Function App timezone is Sydney.

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
- `firebase-service-account.json` is deployed with the function because it is not excluded by `.funcignore`.
