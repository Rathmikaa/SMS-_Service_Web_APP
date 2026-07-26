# GCC Property Tax SMS Console

A small web app that turns GCC (Greater Chennai Corporation) property-tax-due
PDF reports into a dashboard you can use to send reminder SMS to property
owners — upload the PDF, review the extracted records, edit the message
template, select who to message, and send (or dry-run first).

Built and tested against the two report formats you provided:
- `Vadivelu-TC__Rs_25K_to_Rs_50K_.pdf` (158 rows)
- `B_172-Vadivelu_Rs_10k_to_Rs_50k_.pdf` (132 rows, 52 overlap with the file
  above and get merged automatically by bill number — 238 unique records)

## What it does

1. **Upload** one or more PDFs → parses every row using the table headers
   (not fixed column positions), so it keeps working even if a future
   report adds/reorders columns.
2. **Cleans the data**: strips label text ("New No :12" → "12"), normalizes
   phone numbers to 10 digits, and — importantly — detects and fixes a
   real artifact in these specific PDFs where a handful of rows that
   straddle a page break get their text rendered twice
   (`"A RAJI"` → `"AA RRAAJJII"`, `46654` → `4466665544`). Without this fix
   the total balance summed to a nonsense ₹24 billion; with it, it's a
   correct ₹6.85M across both files. A few rows still come out with a
   slightly truncated bill number — those get a ⚠ "needs review" flag in
   the dashboard so you can eyeball them before sending rather than
   silently trusting bad data.
3. **Dedupes** by bill number across multiple uploaded files.
4. **Dashboard**: filter by has/missing mobile number, status, or search;
   select rows; edit the SMS template live with a preview.
5. **Send**: dry-run by default (nothing actually goes out, just logged).
   Flip the checkbox off to send for real once a provider is configured.
6. **Send log** and **CSV export** of all records.

## Quick start (local)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # edit as needed
python3 app.py
```

Open http://localhost:5000, upload the PDFs, and try a dry-run send.
Nothing here talks to a real SMS provider until you configure one (below).

## Message template

Default template (matches your sample exactly):

```
GCC, Zone-13, Div-{dn} Property tax due for bill no {bill_number} sl no {sl_no}
Name: {owner_name}, New Door No {new_door_no}, Street: {street}.
Half Yearly Tax: {current_tax_due} Arrear: {arrear_due} Total Balance: Rs {balance_amount}
Pay immediately via https://chennaicorporation.gov.in/gcc/online-payment/ or GCC Mobile App
to avoid interest/penalty.
```

Editable from the dashboard. Available placeholders: `{dn}` `{bill_number}`
`{sl_no}` `{owner_name}` `{new_door_no}` `{old_door_no}` `{street}` `{mobile}`
`{property_type}` `{property_usage}` `{current_tax_due}` `{arrear_due}`
`{balance_amount}` `{remarks}`.

## Connecting a real (free-tier) SMS provider

Set `SMS_PROVIDER` and the matching credentials as environment variables
(locally in `.env`, or in your host's dashboard). Three are wired up:

| Provider | Env var | Notes |
|---|---|---|
| **TextBee** | `SMS_PROVIDER=textbee` + `TEXTBEE_API_KEY`/`TEXTBEE_DEVICE_ID` | Turns an Android phone into the SMS gateway — messages send from your own phone's SIM via the TextBee app. No per-message fee and no DLT registration hoop, since it isn't going through a bulk-SMS operator route. Good fit for a small in-house rollout. |
| **Fast2SMS** | `SMS_PROVIDER=fast2sms` + `FAST2SMS_API_KEY` | India-focused, free trial credits on signup. The "quick" route (`FAST2SMS_ROUTE=q`, default) works without DLT registration but is meant for OTP/testing, not bulk custom text at scale. |
| **MSG91** | `SMS_PROVIDER=msg91` + `MSG91_AUTH_KEY` | India-focused, free trial credits on signup. |
| **Twilio** | `SMS_PROVIDER=twilio` + `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM_NUMBER` | Global. **Trial accounts can only text phone numbers you've manually verified in the Twilio console** — not usable for messaging arbitrary taxpayers until you upgrade out of trial. |

#### Setting up TextBee

1. Install the TextBee Android app on a phone with an active SIM (the phone
   that will actually send the texts).
2. Sign in / register the device from the app, then open
   [app.textbee.dev/dashboard](https://app.textbee.dev/dashboard) — the
   dashboard shows your **API key** and **device ID**.
3. Set `SMS_PROVIDER=textbee`, `TEXTBEE_API_KEY=...`, and
   `TEXTBEE_DEVICE_ID=...` (locally in `.env`, or in your host's environment
   variables).
4. Keep the phone powered on and connected to the internet — TextBee relays
   the send request from the API to the app on the phone, which then sends
   the actual SMS over the carrier network.

### ⚠️ Important: India's DLT rule

Sending bulk SMS to Indian mobile numbers is regulated by TRAI's DLT
(Distributed Ledger Technology) framework. Even with a valid API key and
credit, a provider will typically **reject or silently drop** any message
whose sender ID and template text aren't pre-registered and approved on
DLT. Before doing a real send run:

1. Register as a DLT "Principal Entity" with your telecom operator's DLT
   platform (this is a one-time registration, usually done through the
   SMS provider's dashboard, e.g. Fast2SMS/MSG91 have guided flows for it).
2. Register the exact message template (with `{variable}` placeholders
   marked as such) and get it approved — approval can take 1–2 days.
3. Put the approved Sender ID / Template ID into the matching env vars
   (e.g. `FAST2SMS_SENDER_ID`, `FAST2SMS_DLT_TEMPLATE_ID`).

Use the `console` provider (the default) to build/test your whole workflow
in the meantime — it never actually sends, just logs what would go out.

## Deploying to Render (free tier)

1. Push this folder to a new GitHub repo.
2. In Render: **New → Blueprint**, point it at the repo — `render.yaml` is
   already set up (it will read `Procfile`/`requirements.txt` automatically).
3. Render's **free web service plan does not include a persistent disk**
   in most regions — the SQLite file may reset on redeploy/restart. For a
   short campaign this is usually fine (re-upload the PDFs after a
   redeploy); for anything longer-lived, either upgrade to a paid Render
   plan with a persistent disk, or swap `DB_PATH` for a hosted Postgres/
   external SQLite (e.g. Render's free Postgres, or Railway's volumes).
4. Once deployed, go to the service's **Environment** tab and set
   `SMS_PROVIDER` + your provider's credentials from the table above.

## Deploying to Railway

1. `railway init` in this folder (or connect the GitHub repo in the
   Railway dashboard).
2. Railway auto-detects the `Procfile`. Add a **Volume** mounted at
   `/data` and set `DB_PATH=/data/app.db` so the database survives
   redeploys.
3. Set the same `SMS_PROVIDER` + credential env vars in Railway's
   **Variables** tab.

## Files

```
app.py            Flask routes: upload, dashboard, send, logs, CSV export
parser.py         PDF → structured records (header-driven, handles the
                   character-doubling artifact described above)
db.py             SQLite schema + queries
sms_gateway.py    Pluggable SMS provider adapters
templates/        Dashboard, send log pages
requirements.txt, Procfile, render.yaml, .env.example
```

## A note on scope

This is an internal tool for a legitimate revenue-collection workflow
(reminding property owners of tax already assessed as due against their
own property, linking to the official GCC payment portal). It doesn't do
anything on your behalf without you clicking Send, and dry-run is the
default. Treat the extracted phone numbers and financial data as
sensitive — don't commit `data/app.db` or the uploaded PDFs to a public
repo (both are already in `.gitignore`).
