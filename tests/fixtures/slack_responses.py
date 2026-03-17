"""Canned Slack API response payloads for unit tests."""

CONVERSATIONS_LIST_RESPONSE = {
    "ok": True,
    "channels": [
        {
            "id": "C001",
            "name": "general",
            "is_channel": True,
            "is_archived": False,
            "num_members": 42,
        },
        {
            "id": "C002",
            "name": "engineering",
            "is_channel": True,
            "is_archived": False,
            "num_members": 15,
        },
    ],
    "response_metadata": {"next_cursor": ""},
}

CONVERSATIONS_HISTORY_RESPONSE = {
    "ok": True,
    "messages": [
        {
            "type": "message",
            "user": "U100",
            "text": "Hey team, the deployment is ready for review.",
            "ts": "1710000001.000001",
            "reply_count": 2,
            "thread_ts": "1710000001.000001",
        },
        {
            "type": "message",
            "user": "U101",
            "text": "Sounds good, I'll take a look.",
            "ts": "1710000002.000002",
        },
        {
            # This message has a subtype and should be skipped by the connector
            "type": "message",
            "subtype": "channel_join",
            "user": "U102",
            "text": "<@U102> has joined the channel",
            "ts": "1710000003.000003",
        },
    ],
    "has_more": False,
    "response_metadata": {"next_cursor": ""},
}

CONVERSATIONS_REPLIES_RESPONSE = {
    "ok": True,
    "messages": [
        {
            # The first message is the parent — connector should skip it
            "type": "message",
            "user": "U100",
            "text": "Hey team, the deployment is ready for review.",
            "ts": "1710000001.000001",
            "thread_ts": "1710000001.000001",
        },
        {
            "type": "message",
            "user": "U103",
            "text": "LGTM, ship it!",
            "ts": "1710000001.000010",
            "thread_ts": "1710000001.000001",
        },
        {
            "type": "message",
            "user": "U104",
            "text": "One minor nit on the config file.",
            "ts": "1710000001.000020",
            "thread_ts": "1710000001.000001",
        },
    ],
    "has_more": False,
    "response_metadata": {"next_cursor": ""},
}

CONVERSATIONS_INFO_RESPONSE = {
    "ok": True,
    "channel": {
        "id": "C001",
        "name": "general",
        "is_channel": True,
    },
}

AUTH_TEST_RESPONSE = {
    "ok": True,
    "url": "https://myteam.slack.com/",
    "team": "My Team",
    "user": "bot_user",
    "team_id": "T001",
    "user_id": "U999",
}

SLACK_EVENT_MESSAGE = {
    "type": "event_callback",
    "token": "verification_token_abc",
    "team_id": "T001",
    "event": {
        "type": "message",
        "channel": "C001",
        "channel_name": "general",
        "user": "U100",
        "text": "A new message via webhook",
        "ts": "1710000099.000099",
    },
}

SLACK_EVENT_THREAD_REPLY = {
    "type": "event_callback",
    "token": "verification_token_abc",
    "team_id": "T001",
    "event": {
        "type": "message",
        "channel": "C001",
        "channel_name": "general",
        "user": "U103",
        "text": "Replying in thread",
        "ts": "1710000100.000100",
        "thread_ts": "1710000099.000099",
    },
}

SLACK_EVENT_BOT_MESSAGE = {
    "type": "event_callback",
    "token": "verification_token_abc",
    "team_id": "T001",
    "event": {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C001",
        "text": "Automated bot message",
        "ts": "1710000200.000200",
    },
}
