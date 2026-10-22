# AdBot setup (Render)

1. Put these files (including .streamlit/ and static/) in a private GitHub repo.
2. Create a free MotherDuck account and copy a service token. The web app and the hourly job share this database.
3. In Render: New > Blueprint > pick the repo. Render reads render.yaml and creates
   "adbot-control-panel" (web) and "adbot-hourly" (cron).
4. Fill in the secret env vars Render asks for. Leave a channel's keys empty to skip it.
   - APP_PASSWORD: the password for the control panel.
   - Apple Search Ads: Settings > API > create an API user, upload the public key; copy client ID, team ID, key ID; org ID is in the account menu. Paste the private key PEM into ASA_PRIVATE_KEY.
   - Meta Ads: Business Settings > System users > generate a token with ads_read and ads_management; META_AD_ACCOUNT_IDS are the numbers after "act_".
   - Google Ads: a developer token (Tools > API Center), an OAuth client (Google Cloud console) and a refresh token for the ads account; customer IDs without dashes.
   - Telegram: talk to @BotFather to create a bot (TELEGRAM_BOT_TOKEN), send it a message, then read your chat ID from https://api.telegram.org/bot<TOKEN>/getUpdates.
5. Name campaigns so the bot can tell the apps apart: include RNR, CCRN or NurseBound in each campaign name (editable under app_match in the rules). For NurseBound, also add the target country code, e.g. "NurseBound | PH | Prospecting".
6. Open the control panel, press "Send test notification", then "Run bot now". Read the Bot log.
7. On your phone, open the control panel address Render gives you (https://adbot-control-panel.onrender.com or similar), sign in, then Share > Add to Home Screen (iPhone) or the ⋮ menu > Add to Home screen (Android).
8. DRY_RUN stays "true" until you set it to "false" on the adbot-hourly job. Until then the bot only reports what it would pause or change.

Rules edited in the dashboard: Export rules.json there, then "Import rules.json" in the control panel (or paste it into a RULES_JSON env var).

Local run: `pip install -r requirements.txt`, set APP_PASSWORD, then `python dlt_pipeline.py --days 14` and `streamlit run app.py`.
