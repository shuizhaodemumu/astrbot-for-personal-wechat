import asyncio
import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from main import (
    NativeWeChatAdapter,
    NativeWeChatEvent,
    NativeWeChatPlugin,
)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
            "chat_provider_id": "test-model",
        }
        Path(self.config["token_file"]).write_text("test")
        self.adapter = NativeWeChatAdapter(self.config, {}, asyncio.Queue())
        self.adapter.own_id = "wxid_bot"
        self.native = {
            "id": "message1",
            "accountId": "account-test",
            "threadId": "123@chatroom",
            "senderId": "wxid_person",
            "type": 1,
            "direction": "incoming",
            "body": "@bot hello",
            "mentions": {"state": "known", "self": True, "all": False},
            "createdAt": datetime.now(UTC).isoformat(),
        }

    def test_only_explicit_mentions_in_target_group(self):
        message = self.adapter.convert(self.native, "live")
        self.assertIsNotNone(message)
        event = NativeWeChatEvent(message, self.adapter)
        self.assertEqual(event.get_group_id(), "123@chatroom")
        self.assertEqual(event.get_self_id(), "wxid_bot")
        variants = [
            {"threadId": "other@chatroom"},
            {"threadId": "wxid_person"},
            {"accountId": "another-account"},
            {"direction": "outgoing"},
            {"senderId": "wxid_bot"},
            {"type": 3},
            {"mentions": {"state": "unknown", "self": True}},
            {"mentions": {"state": "known", "all": True, "self": False}},
            {"createdAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
            {"recall": {"state": "recalled"}},
            {"createdAt": "invalid"},
        ]
        for change in variants:
            with self.subTest(change=change):
                self.assertIsNone(self.adapter.convert(self.native | change, "live"))
        self.assertIsNone(self.adapter.convert(self.native, "history"))

    async def test_poll_deduplicates_and_checkpoints(self):
        event = {
            "cursor": "1",
            "kind": "message.committed",
            "data": {
                "threadId": "123@chatroom",
                "messageId": "message1",
                "origin": "live",
            },
        }
        self.adapter.request = AsyncMock(
            side_effect=[
                {
                    "authenticated": True,
                    "ownUsername": "wxid_bot",
                    "connection": "online",
                },
                {"items": [event, event | {"cursor": "2"}]},
                self.native,
                self.native,
                asyncio.CancelledError(),
            ]
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.adapter.run()
        self.assertEqual(self.adapter._event_queue.qsize(), 1)
        state = json.loads(Path(self.config["state_file"]).read_text())
        self.assertEqual(state["cursor"], "2")
        self.assertEqual(state["seen"], ["message1"])
        self.assertTrue(self.adapter.http.closed)

    async def test_recall_before_reply_blocks_send(self):
        source = self.adapter.convert(self.native, "live")
        self.adapter.request = AsyncMock(
            return_value=self.native | {"recall": {"state": "recalled"}}
        )
        with self.assertRaises(ValueError):
            await self.adapter.reply(source, "reply", "key")
        self.adapter.request.assert_awaited_once()

    async def test_submit_once_and_check_receipt(self):
        source = self.adapter.convert(self.native, "live")
        self.adapter.request = AsyncMock(
            side_effect=[
                self.native,
                {"id": "send1", "state": "queued"},
                {
                    "id": "send1",
                    "state": "server_accepted",
                    "accountId": "account-test",
                    "destination": "123@chatroom",
                },
            ]
        )
        await self.adapter.reply(source, "reply", "stable-key")
        posts = [x for x in self.adapter.request.await_args_list if x.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].kwargs["headers"]["Idempotency-Key"], "stable-key")

    async def test_profile_does_not_change_global_settings(self):
        profile_path = Path(self.temp.name) / "profile.json"
        profile_path.write_text("{}")
        default = {
            "platform": [self.config],
            "provider_settings": {"wake_prefix": "咪咪", "streaming_response": True},
            "agent_runner": {"config": {"model": {"provider_id": "old-model"}}},
        }
        previous = copy.deepcopy(default)
        manager = SimpleNamespace(
            default_conf=default,
            get_conf_info=lambda origin: {"id": "default"},
            create_conf=AsyncMock(return_value="profile-id"),
            confs={"profile-id": SimpleNamespace(config_path=str(profile_path))},
            ucr=SimpleNamespace(
                umop_to_conf_id={"qq::": "qq-profile"}, update_routing_data=AsyncMock()
            ),
        )
        plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager))
        await plugin.initialize()
        self.assertEqual(default, previous)
        supplied = manager.create_conf.await_args.args[0]
        self.assertEqual(supplied["provider_settings"]["wake_prefix"], "")
        self.assertEqual(
            supplied["agent_runner"]["config"]["model"]["provider_id"], "test-model"
        )
        routes = manager.ucr.update_routing_data.await_args.args[0]
        self.assertEqual(routes["wechat-test:GroupMessage:123@chatroom"], "profile-id")
        self.assertEqual(routes["qq::"], "qq-profile")


if __name__ == "__main__":
    unittest.main()
