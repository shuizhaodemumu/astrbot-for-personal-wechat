import asyncio
import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from main import (
    NativeWeChatAdapter,
    NativeWeChatEvent,
    NativeWeChatPlugin,
)


class Profile(dict):
    def __init__(self, data, path=None):
        super().__init__(data)
        self.config_path = str(path) if path else "unused"
        self.save_config = Mock()


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
            get_conf=lambda origin: default,
            update_conf_info=AsyncMock(),
            create_conf=AsyncMock(return_value="profile-id"),
            confs={"profile-id": Profile(copy.deepcopy(default), profile_path)},
            ucr=SimpleNamespace(
                umop_to_conf_id={"qq::": "qq-profile"}, update_routing_data=AsyncMock()
            ),
        )
        plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager))
        plugin.fetch_group_name = AsyncMock(return_value="Test group")
        plugin.apply_group_persona = AsyncMock()
        await plugin.initialize()
        self.assertEqual(plugin.config["groups"][0]["group_name"], "Test group")
        self.assertEqual(default, previous)
        supplied = manager.create_conf.await_args.args[0]
        self.assertEqual(supplied["provider_settings"]["wake_prefix"], "")
        self.assertEqual(
            supplied["agent_runner"]["config"]["model"]["provider_id"], "test-model"
        )
        routes = manager.ucr.update_routing_data.await_args.args[0]
        self.assertEqual(routes["wechat-test:GroupMessage:123@chatroom"], "profile-id")
        self.assertEqual(routes["qq::"], "qq-profile")


class PersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = Profile(
            {"agent_runner": {"config": {"persona": {"persona_id": "old"}}}}
        )
        self.conversation = SimpleNamespace(persona_id=None)
        self.cm = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="old-conversation"),
            get_conversation=AsyncMock(return_value=self.conversation),
            new_conversation=AsyncMock(),
            update_conversation=AsyncMock(),
        )
        self.pm = SimpleNamespace(get_persona=AsyncMock(return_value=object()))
        self.plugin = NativeWeChatPlugin(
            SimpleNamespace(
                conversation_manager=self.cm,
                persona_manager=self.pm,
            )
        )
        self.get = AsyncMock(return_value={})
        self.put = AsyncMock()
        self.patches = [
            patch("main.sp.get_async", self.get),
            patch("main.sp.put_async", self.put),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.rule = {
            "persona_id": "new",
            "group_name": "Test group",
            "new_conversation_on_change": True,
        }

    async def test_switch_preserves_history_and_only_changes_target(self):
        await self.plugin.apply_group_persona(
            "wechat:GroupMessage:1@chatroom", self.profile, self.rule
        )
        self.cm.new_conversation.assert_awaited_once_with(
            "wechat:GroupMessage:1@chatroom",
            title="Test group · new",
            persona_id="new",
        )
        self.cm.update_conversation.assert_not_awaited()
        self.profile.save_config.assert_called_once()
        self.assertEqual(
            self.profile["agent_runner"]["config"]["persona"]["persona_id"], "new"
        )

    async def test_repeated_save_does_not_start_another_conversation(self):
        self.conversation.persona_id = "new"
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.cm.new_conversation.assert_not_awaited()

    async def test_keep_history_updates_existing_conversation(self):
        self.rule["new_conversation_on_change"] = False
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.cm.new_conversation.assert_not_awaited()
        self.cm.update_conversation.assert_awaited_once_with(
            "group", conversation_id="old-conversation", persona_id="new"
        )

    async def test_invalid_persona_keeps_existing_settings(self):
        self.pm.get_persona.side_effect = ValueError("not found")
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.profile.save_config.assert_not_called()
        self.cm.new_conversation.assert_not_awaited()
        self.get.assert_not_awaited()

    async def test_session_override_is_changed_without_losing_other_options(self):
        self.get.return_value = {"persona_id": "override", "provider_id": "model"}
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.put.assert_awaited_once_with(
            "umo",
            "group",
            "session_service_config",
            {"persona_id": "new", "provider_id": "model"},
        )

    async def test_different_groups_use_different_personas(self):
        other = copy.deepcopy(self.profile)
        await self.plugin.apply_group_persona("group-a", self.profile, self.rule)
        await self.plugin.apply_group_persona(
            "group-b", other, self.rule | {"persona_id": "another"}
        )
        self.assertEqual(
            self.profile["agent_runner"]["config"]["persona"]["persona_id"], "new"
        )
        self.assertEqual(
            other["agent_runner"]["config"]["persona"]["persona_id"], "another"
        )

    async def test_duplicate_group_rows_are_rejected_before_changes(self):
        rule = {"platform_id": "wechat", "group_id": "1@chatroom"}
        plugin = NativeWeChatPlugin(
            SimpleNamespace(astrbot_config_mgr=object()), {"groups": [rule, rule]}
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            await plugin.initialize()

    async def test_group_name_refresh_and_outside_whitelist_ignored(self):
        profile = Profile(
            {
                "agent_runner": {
                    "runner_type": "local",
                    "config": {"persona": {"persona_id": "old"}},
                }
            }
        )
        origin = "wechat:GroupMessage:1@chatroom"
        manager = SimpleNamespace(
            default_conf={
                "platform": [
                    {
                        "id": "wechat",
                        "type": "native_wechat",
                        "enable": True,
                        "allowed_groups": ["1@chatroom"],
                    }
                ]
            },
            get_conf_info=lambda origin: {"id": "profile", "name": "old title"},
            ucr=SimpleNamespace(umop_to_conf_id={origin: "profile"}),
            confs={"profile": profile},
            update_conf_info=AsyncMock(),
        )
        config = Profile(
            {
                "groups": [
                    dict(self.rule, platform_id="wechat", group_id="1@chatroom"),
                    dict(self.rule, platform_id="wechat", group_id="outside@chatroom"),
                ]
            }
        )
        plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager), config)
        plugin.fetch_group_name = AsyncMock(return_value="Renamed group")
        plugin.apply_group_persona = AsyncMock()
        await plugin.initialize()
        self.assertEqual(config["groups"][0]["group_name"], "Renamed group")
        plugin.apply_group_persona.assert_awaited_once()
        self.assertEqual(plugin.apply_group_persona.await_args.args[0], origin)
        config.save_config.assert_called_once()
        plugin.fetch_group_name.return_value = None
        await plugin.initialize()
        self.assertEqual(config["groups"][0]["group_name"], "Renamed group")

    async def test_shared_profile_is_cloned_with_exact_route_first(self):
        with tempfile.TemporaryDirectory() as temp:
            old = Profile(
                {
                    "provider_settings": {"wake_prefix": "old"},
                    "agent_runner": {
                        "config": {
                            "model": {"provider_id": "model"},
                            "persona": {"persona_id": "old"},
                        }
                    },
                }
            )
            before = copy.deepcopy(dict(old))
            path = Path(temp) / "isolated.json"
            path.write_text("{}")
            isolated = Profile(copy.deepcopy(dict(old)), path)
            origin = "wechat:GroupMessage:1@chatroom"
            routes = {"wechat::": "shared", origin: "shared", "qq::": "shared"}
            manager = SimpleNamespace(
                default_conf={
                    "platform": [
                        {
                            "id": "wechat",
                            "type": "native_wechat",
                            "enable": True,
                            "allowed_groups": ["1@chatroom"],
                            "chat_provider_id": "model",
                        }
                    ]
                },
                get_conf_info=lambda _: {"id": "shared", "name": "Shared"},
                get_conf=lambda _: old,
                confs={"shared": old, "isolated": isolated},
                create_conf=AsyncMock(return_value="isolated"),
                update_conf_info=AsyncMock(),
                ucr=SimpleNamespace(
                    umop_to_conf_id=routes, update_routing_data=AsyncMock()
                ),
            )
            plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager))
            plugin.fetch_group_name = AsyncMock(return_value=None)
            plugin.apply_group_persona = AsyncMock()
            await plugin.initialize()
            updated = manager.ucr.update_routing_data.await_args.args[0]
            self.assertEqual(next(iter(updated)), origin)
            self.assertEqual(updated[origin], "isolated")
            self.assertEqual(updated["qq::"], "shared")
            self.assertEqual(updated["wechat::"], "shared")
            self.assertEqual(dict(old), before)

    async def test_group_name_fetch_rejects_nonlocal_host_before_reading_token(self):
        result = await self.plugin.fetch_group_name(
            {"server_url": "https://example.com"}, "1@chatroom"
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
