"""
sms_gateway.py
Pluggable SMS sending adapter. Pick a provider with the SMS_PROVIDER env var:

  SMS_PROVIDER=console   -> doesn't actually send; just logs (safe default)
  SMS_PROVIDER=textbee   -> TextBee (turns an Android phone into an SMS
                             gateway; https://textbee.dev, dashboard at
                             https://app.textbee.dev/dashboard — no per-SMS
                             fee, no DLT registration, sends from your own
                             SIM/number)
  SMS_PROVIDER=fast2sms  -> Fast2SMS (India, has a free-trial "quick" route)
  SMS_PROVIDER=msg91     -> MSG91 (India, free trial credits on signup)
  SMS_PROVIDER=twilio    -> Twilio (global, free trial credit but can only
                             text numbers you've verified while on trial)

Every adapter exposes the same signature:
    send(mobile: str, message: str) -> (success: bool, response_text: str)

IMPORTANT — regulatory note (India):
Sending bulk/promotional or transactional SMS to Indian mobile numbers is
governed by TRAI's DLT (Distributed Ledger Technology) framework. Providers
will usually reject or silently drop messages whose sender ID / template
isn't DLT-registered, even if your API key is valid and has credit. For a
real rollout, register a DLT entity + template that matches the message
format below, and put the approved DLT Template ID in your provider
dashboard/config. The "console" provider lets you build and test the whole
app before you have that sorted out.
"""

import os
import requests


class ConsoleProvider:
    """Default no-op provider: prints/logs what would be sent. Always
    'succeeds' so you can exercise the full app flow risk-free."""

    name = "console"

    def send(self, mobile, message):
        text = f"[DRY-RUN] would send to +91{mobile}: {message}"
        print(text)
        return True, text


class TextBeeProvider:
    """https://textbee.dev - turns an Android phone (with the TextBee app
    installed) into an SMS gateway. Get your API key and device ID from
    https://app.textbee.dev/dashboard. No per-message fee and no DLT
    registration needed since messages go out through your own phone's SIM."""

    name = "textbee"
    BASE_URL = "https://api.textbee.dev/api/v1/gateway"

    def __init__(self):
        self.api_key = os.environ.get("TEXTBEE_API_KEY", "")
        self.device_id = os.environ.get("TEXTBEE_DEVICE_ID", "")

    def send(self, mobile, message):
        if not (self.api_key and self.device_id):
            return False, "TEXTBEE_API_KEY / TEXTBEE_DEVICE_ID is not set (get both from https://app.textbee.dev/dashboard)"
        url = f"{self.BASE_URL}/devices/{self.device_id}/send-sms"
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        # Indian 10-digit mobiles need the country code for TextBee's E.164-style recipient format.
        recipient = mobile if mobile.startswith("+") else f"+91{mobile}"
        payload = {"recipients": [recipient], "message": message}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            ok = resp.status_code in (200, 201)
            return ok, resp.text
        except Exception as e:  # noqa: BLE001
            return False, f"error: {e}"


class Fast2SMSProvider:
    """https://docs.fast2sms.com/ - Quick/DLT route bulk SMS."""

    name = "fast2sms"
    URL = "https://www.fast2sms.com/dev/bulkV2"

    def __init__(self):
        self.api_key = os.environ.get("FAST2SMS_API_KEY", "")
        self.route = os.environ.get("FAST2SMS_ROUTE", "q")  # 'q' = quick/promo trial route
        self.sender_id = os.environ.get("FAST2SMS_SENDER_ID", "")
        self.dlt_template_id = os.environ.get("FAST2SMS_DLT_TEMPLATE_ID", "")

    def send(self, mobile, message):
        if not self.api_key:
            return False, "FAST2SMS_API_KEY is not set"
        headers = {"authorization": self.api_key}
        payload = {
            "route": self.route,
            "message": message,
            "language": "english",
            "flash": 0,
            "numbers": mobile,
        }
        if self.route == "dlt":
            payload["sender_id"] = self.sender_id
            payload["dlt_template_id"] = self.dlt_template_id
        try:
            resp = requests.post(self.URL, headers=headers, data=payload, timeout=15)
            ok = resp.status_code == 200 and resp.json().get("return") is True
            return ok, resp.text
        except Exception as e:  # noqa: BLE001
            return False, f"error: {e}"


class Msg91Provider:
    """https://docs.msg91.com/ - legacy sendhttp route (simplest to wire up)."""

    name = "msg91"
    URL = "https://api.msg91.com/api/sendhttp.php"

    def __init__(self):
        self.auth_key = os.environ.get("MSG91_AUTH_KEY", "")
        self.sender_id = os.environ.get("MSG91_SENDER_ID", "GCCTAX")
        self.route = os.environ.get("MSG91_ROUTE", "4")

    def send(self, mobile, message):
        if not self.auth_key:
            return False, "MSG91_AUTH_KEY is not set"
        params = {
            "authkey": self.auth_key,
            "mobiles": f"91{mobile}",
            "message": message,
            "sender": self.sender_id,
            "route": self.route,
            "country": "91",
        }
        try:
            resp = requests.get(self.URL, params=params, timeout=15)
            ok = resp.status_code == 200 and "error" not in resp.text.lower()
            return ok, resp.text
        except Exception as e:  # noqa: BLE001
            return False, f"error: {e}"


class TwilioProvider:
    """https://www.twilio.com/docs/sms - trial accounts can only message
    phone numbers you've verified in the Twilio console."""

    name = "twilio"

    def __init__(self):
        self.account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = os.environ.get("TWILIO_FROM_NUMBER", "")

    def send(self, mobile, message):
        if not (self.account_sid and self.auth_token and self.from_number):
            return False, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER not set"
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        try:
            resp = requests.post(
                url,
                auth=(self.account_sid, self.auth_token),
                data={"To": f"+91{mobile}", "From": self.from_number, "Body": message},
                timeout=15,
            )
            ok = resp.status_code in (200, 201)
            return ok, resp.text
        except Exception as e:  # noqa: BLE001
            return False, f"error: {e}"


PROVIDERS = {
    "console": ConsoleProvider,
    "textbee": TextBeeProvider,
    "fast2sms": Fast2SMSProvider,
    "msg91": Msg91Provider,
    "twilio": TwilioProvider,
}


def get_provider():
    name = os.environ.get("SMS_PROVIDER", "console").lower().strip()
    cls = PROVIDERS.get(name, ConsoleProvider)
    return cls()
