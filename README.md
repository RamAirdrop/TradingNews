# Daybreak (Streamlit, Python)

NSE news signals plus a paper-trading portfolio (starts at ₹1,00,000; stop/target/15:15 IST square-off; P/L compounds). Simulated only, not advice.

## Run
```
pip install -r requirements.txt
streamlit run app.py
python -m pytest
```
`ALLOW_OFF_HOURS=1` lets you paper-trade outside market hours (demo).

## Sign-in
- Username/password: built in (scrypt, lockout). Enable sign-up with `allow_registration = true` in secrets.
- Google: add an `[auth]` block (redirect_uri, cookie_secret, client_id, client_secret, server_metadata_url) to `.streamlit/secrets.toml`; optional `allowed_emails`.

## Hosting (Streamlit Community Cloud)
The app sleeps when idle and its disk is ephemeral, so SQLite data (`data/daybreak.db`, override with `DAYBREAK_DB`) can be lost. Stops/targets are replayed from 5-minute candles on each page load instead of a background job. For durable data use a hosted database. Password sessions are lost on browser refresh.
