from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import AppConfig, Settings
from app.domain.delegate import Delegate
from app.domain.enums import Channel
from app.integrations.fake import FakeMail
from app.integrations.http import IntegrationError
from app.integrations.notify.base import DelegationMessage
from app.integrations.notify.registry import build_notifiers
from app.integrations.notify.slack import SlackNotifier, slack_client
from app.integrations.notify.teams import TeamsNotifier
from app.integrations.notify.telegram import TelegramApi, TelegramNotifier, bot_client
from app.integrations.registry import Adapters

TG = "https://api.telegram.org/botTOKEN/"
TG_FILES = "https://api.telegram.org/file/botTOKEN/"
SLACK = "https://slack.com/api/"
HOOK = "https://example.webhook.office.com/webhookb2/abc"

RAHUL = Delegate(
    id="rahul",
    full_name="Rahul Kumar",
    email="rahul@example.com",
    telegram_chat_id="100",
    slack_user_id="U1",
    teams_webhook=HOOK,
)


def message(delegate: Delegate = RAHUL) -> DelegationMessage:
    return DelegationMessage("fu1", delegate, "Delegated: Vendor review [ref fu1]", "Body", "<p/>")


def ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


# --- Telegram ------------------------------------------------------------------------


def test_telegram_notifier(mock_http: respx.MockRouter) -> None:
    send = mock_http.post(f"{TG}sendMessage").mock(return_value=ok({"message_id": 7}))
    api = TelegramApi(bot_client("TOKEN", sleep=lambda _s: None))
    notifier = TelegramNotifier(api, "999")
    assert notifier.can_reach(RAHUL)
    assert not notifier.can_reach(RAHUL.model_copy(update={"telegram_chat_id": None}))
    delivery = notifier.send_delegation(message())
    assert delivery.channel == Channel.TELEGRAM
    assert delivery.ref == {"chat_id": "100", "message_id": 7}
    body = json.loads(send.calls[0].request.content)
    assert body["chat_id"] == "100" and body["text"].startswith("Delegated: Vendor review")
    buttons = body["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["ack:fu1", "q:fu1"]
    notifier.send_nudge(RAHUL, "Reminder", {"chat_id": "100"})
    assert json.loads(send.calls[1].request.content) == {
        "chat_id": "100",
        "text": "Reminder",
        "disable_web_page_preview": True,
    }
    assert notifier.notify_ceo("Hi")
    assert json.loads(send.calls[2].request.content)["chat_id"] == "999"
    assert not TelegramNotifier(api).notify_ceo("Hi")


def test_telegram_api_calls(mock_http: respx.MockRouter) -> None:
    api = TelegramApi(
        bot_client("TOKEN", sleep=lambda _s: None),
        bot_client("TOKEN", files=True, sleep=lambda _s: None),
    )
    mock_http.post(f"{TG}answerCallbackQuery").mock(return_value=ok(True))
    edit = mock_http.post(f"{TG}editMessageReplyMarkup").mock(return_value=ok(True))
    updates = mock_http.post(f"{TG}getUpdates").mock(return_value=ok([{"update_id": 1}]))
    mock_http.post(f"{TG}getFile").mock(return_value=ok({"file_path": "voice/f.oga"}))
    mock_http.get(f"{TG_FILES}voice/f.oga").mock(return_value=httpx.Response(200, content=b"OGG"))
    voice = mock_http.post(f"{TG}sendVoice").mock(return_value=ok({}))

    api.answer_callback("cb", "Done.")
    api.edit_buttons("100", 5)
    assert json.loads(edit.calls[0].request.content)["reply_markup"] == {"inline_keyboard": []}
    assert api.get_updates(None, 1) == [{"update_id": 1}]
    api.get_updates(3, 1)
    assert json.loads(updates.calls[1].request.content)["offset"] == 3
    assert api.download_file("abc") == b"OGG"
    api.send_voice("999", b"OGG")
    assert b"audio/ogg" in voice.calls[0].request.content

    mock_http.post(f"{TG}sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"})
    )
    with pytest.raises(IntegrationError, match="chat not found"):
        api.send_message("1", "x")
    mock_http.post(f"{TG}sendVoice").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "bad audio"})
    )
    with pytest.raises(IntegrationError, match="bad audio"):
        api.send_voice("1", b"")
    no_files = TelegramApi(bot_client("TOKEN", sleep=lambda _s: None))
    with pytest.raises(IntegrationError, match="not available"):
        no_files.download_file("abc")


# --- Slack ---------------------------------------------------------------------------


def slack_ok(**data: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **data})


def test_slack_notifier(mock_http: respx.MockRouter) -> None:
    mock_http.post(f"{SLACK}conversations.open").mock(return_value=slack_ok(channel={"id": "D1"}))
    post = mock_http.post(f"{SLACK}chat.postMessage").mock(return_value=slack_ok(ts="1.0"))
    notifier = SlackNotifier(slack_client("xoxb", sleep=lambda _s: None), "UCEO")
    assert notifier.can_reach(RAHUL)
    delivery = notifier.send_delegation(message())
    assert delivery.ref == {"channel": "D1", "ts": "1.0", "user": "U1"}
    sent = json.loads(post.calls[0].request.content)
    assert sent["channel"] == "D1" and sent["blocks"][0]["type"] == "header"
    assert post.calls[0].request.headers["Authorization"] == "Bearer xoxb"
    notifier.send_nudge(RAHUL, "Reminder", delivery.ref)
    assert json.loads(post.calls[1].request.content)["thread_ts"] == "1.0"
    notifier.send_nudge(RAHUL, "Reminder", {"channel": "D1"})
    assert "thread_ts" not in json.loads(post.calls[2].request.content)
    assert notifier.notify_ceo("Hello")
    assert not SlackNotifier(slack_client("xoxb")).notify_ceo("Hello")

    mock_http.post(f"{SLACK}chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    with pytest.raises(IntegrationError, match="channel_not_found"):
        notifier.notify_ceo("x")


def test_slack_poll_ack(mock_http: respx.MockRouter) -> None:
    notifier = SlackNotifier(slack_client("xoxb", sleep=lambda _s: None))
    ref = {"channel": "D1", "ts": "1.0", "user": "U1"}
    assert notifier.poll_ack({}, set()) == []
    reactions = mock_http.get(f"{SLACK}reactions.get").mock(
        return_value=slack_ok(message={"reactions": [{"name": "+1", "users": ["U1"]}]})
    )
    acks = notifier.poll_ack(ref, set())
    assert [(a.signal, a.reply_id) for a in acks] == [("acknowledged", "reaction:1.0")]

    reactions.mock(return_value=slack_ok(message={"reactions": [{"name": "+1", "users": ["U9"]}]}))
    mock_http.get(f"{SLACK}conversations.history").mock(
        return_value=slack_ok(
            messages=[
                {"ts": "1.0", "user": "B", "text": "the brief"},
                {"ts": "2.0", "user": "U1", "text": "On it, thanks"},
                {"ts": "3.0", "user": "U1", "text": "Which vendor?"},
                {"ts": "4.0", "user": "U7", "text": "ok"},
                {"ts": "5.0", "user": "U1", "text": "seen already"},
            ]
        )
    )
    events = notifier.poll_ack(ref, {"5.0"})
    assert [(e.signal, e.reply_id) for e in events] == [
        ("acknowledged", "2.0"),
        ("question", "3.0"),
    ]
    assert reactions.call_count == 2
    notifier.poll_ack(ref, {"reaction:1.0"})
    assert reactions.call_count == 2


# --- Teams ---------------------------------------------------------------------------


def test_teams_notifier(mock_http: respx.MockRouter) -> None:
    hook = mock_http.post(HOOK).mock(return_value=httpx.Response(200, text="1"))
    ceo = mock_http.post("https://example.webhook.office.com/ceo").mock(
        return_value=httpx.Response(200, text="1")
    )
    notifier = TeamsNotifier("https://example.webhook.office.com/ceo", sleep=lambda _s: None)
    assert notifier.can_reach(RAHUL)
    assert notifier.send_delegation(message()).channel == Channel.TEAMS
    card = json.loads(hook.calls[0].request.content)
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard"
    assert content["body"][0]["text"].startswith("Delegated: Vendor review")
    assert "Accepted" in content["body"][1]["text"]
    notifier.send_nudge(RAHUL, "Reminder", {})
    assert hook.call_count == 2
    assert notifier.notify_ceo("Hi") and ceo.call_count == 1
    assert not TeamsNotifier().notify_ceo("Hi")


# --- registry ------------------------------------------------------------------------


def test_registry_builds_what_is_configured() -> None:
    config = AppConfig.model_validate(
        {"adapters": {"notify": ["email", "telegram", "slack", "teams"]}}
    )
    full = Settings(
        telegram_bot_token=SecretStr("t"),
        telegram_ceo_chat_id="999",
        slack_bot_token=SecretStr("s"),
        teams_webhook_url=SecretStr(HOOK),
    )
    built = build_notifiers(full, config, Adapters(mail=[FakeMail()]))
    assert [n.channel for n in built] == [
        Channel.EMAIL,
        Channel.TELEGRAM,
        Channel.SLACK,
        Channel.TEAMS,
    ]
    bare = build_notifiers(Settings(), config, Adapters())
    assert [n.channel for n in bare] == [Channel.TEAMS]
    assert full.secret_values() == ["t", "s", HOOK]
