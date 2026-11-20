import json
import os
import urllib.error
import urllib.request

def destination():
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_REPORT_CHAT_ID")

def deliver(text):
    token, chat = destination()
    data = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as reply:
            response = json.load(reply)
        if response.get("ok") is True:
            return "Sent", str(response["result"]["message_id"])
        return "Failed", ""
    except urllib.error.HTTPError as error:
        return ("Failed" if error.code < 500 else "Unconfirmed"), ""
    except (OSError, ValueError, KeyError):
        return "Unconfirmed", ""
