"""
Minimal client for posting messages to Slack via the Web API's
chat.postMessage.

Requires SLACK_BOT_TOKEN (a Bot User OAuth Token, "xoxb-...", from the
app's OAuth & Permissions page after installing it to the workspace with
the chat:write scope) and SLACK_CHANNEL (a channel ID/name to post to, or
a user ID to send a direct message instead -- useful for testing).
"""

import os
import sys

import requests

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
REQUEST_TIMEOUT = 10


class SlackError(Exception):
    """Raised when credentials are missing or posting fails."""


def credentials_present():
    return bool(os.environ.get("SLACK_BOT_TOKEN")) and bool(os.environ.get("SLACK_CHANNEL"))


def post_message(text, blocks=None):
    """
    text is always sent as the fallback (shown in notifications, screen
    readers, and if blocks fail to render); blocks is an optional Block Kit
    array for richer formatting (e.g. a table block).
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    if not token or not channel:
        raise SlackError("SLACK_BOT_TOKEN / SLACK_CHANNEL are not set")

    payload = {"channel": channel, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks

    try:
        response = requests.post(
            POST_MESSAGE_URL,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Scrub the bot token just in case it ends up in the exception text
        # somewhere -- same lesson as the Mouser API key leak.
        message = str(exc).replace(token, "***")
        raise SlackError(f"failed to post to Slack: {message}") from exc

    # chat.postMessage returns HTTP 200 even on a logical failure (bad
    # channel, missing scope, etc) -- the real result is in "ok".
    payload = response.json()
    if not payload.get("ok"):
        raise SlackError(f"Slack rejected the message: {payload.get('error')}")


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) or "Test message from slack_client.py"
    try:
        post_message(text)
    except SlackError as exc:
        sys.exit(f"Error: {exc}")
    print("Posted to Slack.")
