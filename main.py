# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 shuizhaodemumu

import asyncio
import copy
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import aiohttp
from astrbot.api import logger, sp
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    AstrMessageEvent,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.star import Star, register
from astrbot.core.utils.session_lock import session_lock_manager


class NativeWeChatEvent(AstrMessageEvent):
    def __init__(self, message, platform):
        super().__init__(
            message.message_str, message, platform.meta(), message.session_id
        )
        self.platform = platform
        self.delivery = 0

    async def send(self, message: MessageChain):
        text = "".join(part.text for part in message.chain if isinstance(part, Plain))
        if not text.strip():
            return
        key = hashlib.sha256(
            f"{self.message_obj.message_id}:{self.delivery}".encode()
        ).hexdigest()
        self.delivery += 1
        await self.platform.reply(self.message_obj, text, key)
        await super().send(message)

    async def send_streaming(self, generator, use_fallback=False):
        parts = []
        async for message in generator:
            parts.extend(part for part in message.chain if isinstance(part, Plain))
        if parts:
            await self.send(MessageChain(parts))


@register_platform_adapter(
    "native_wechat",
    "本机个人微信：指定群 @ 回复（文字）",
    default_config_tmpl={
        "server_url": "http://127.0.0.1:6174",
        "account_id": "",
        "token_file": "",
        "state_file": "",
        "allowed_groups": [],
        "chat_provider_id": "",
    },
    adapter_display_name="本机个人微信",
    support_streaming_message=False,
)
class NativeWeChatAdapter(Platform):
    def __init__(self, config, settings, event_queue):
        super().__init__(config, event_queue)
        self.allowed_groups = set(config.get("allowed_groups", []))
        if any(not group.endswith("@chatroom") for group in self.allowed_groups):
            raise ValueError("Only explicit WeChat group IDs are supported")
        self.account_id = config["account_id"]
        self.base = config["server_url"].rstrip("/")
        if self.base != "http://127.0.0.1:6174":
            raise ValueError("This adapter is restricted to the local bridge")
        self.prefix = "/v1/accounts/" + quote(self.account_id, safe="")
        self.state_file = Path(config["state_file"])
        self.http = None
        self.own_id = ""
        self.started = datetime.now(UTC)
        self.cursor = "0"
        self.seen = []
        self.health_file = self.state_file.with_suffix(".health.json")
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text())
            if state.get("account_id") != self.account_id:
                raise ValueError("State belongs to another account")
            self.cursor = str(state["cursor"])
            self.seen = state.get("seen", [])[-2048:]

    def meta(self):
        return PlatformMetadata(
            "native_wechat",
            "Local WeChat text adapter",
            self.config["id"],
            support_streaming_message=False,
            support_proactive_message=False,
        )

    async def request(self, method, path, **kwargs):
        async with self.http.request(
            method, self.base + self.prefix + path, **kwargs
        ) as response:
            if response.status >= 400:
                raise RuntimeError(f"WeChat bridge HTTP {response.status}")
            return await response.json()

    def convert(self, native, origin):
        """Admit only fresh, explicitly mentioned messages in approved groups.

        Args:
            native: Canonical native message from the authenticated bridge.
            origin: Event origin supplied by the bridge.

        Returns:
            An AstrBot message, or None when the message is outside the policy.
        """
        mentions = native.get("mentions") or {}
        if (
            origin != "live"
            or native.get("accountId") != self.account_id
            or native.get("threadId") not in self.allowed_groups
            or native.get("direction") != "incoming"
            or native.get("type") != 1
            or not native.get("senderId")
            or native.get("senderId") == self.own_id
            or not native.get("body")
            or native.get("recall")
            or mentions.get("state") != "known"
            or mentions.get("self") is not True
        ):
            return None
        try:
            created = datetime.fromisoformat(native["createdAt"])
            if (
                created < self.started
                or (datetime.now(UTC) - created).total_seconds() > 300
            ):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = self.own_id
        message.session_id = native["threadId"]
        message.group_id = native["threadId"]
        message.message_id = native["id"]
        message.sender = MessageMember(native["senderId"], native["senderId"])
        message.message_str = native["body"]
        message.message = [At(qq=self.own_id), Plain(native["body"])]
        message.timestamp = int(created.timestamp())
        message.raw_message = native
        return message

    async def run(self):
        token = Path(self.config["token_file"]).read_text().strip()
        self.http = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=False,
        )
        try:
            while True:
                try:
                    account = await self.request("GET", "")
                    if not account.get("authenticated") or not account.get(
                        "ownUsername"
                    ):
                        raise RuntimeError("WeChat account is not authenticated")
                    self.own_id = account["ownUsername"]
                    self.health_file.parent.mkdir(parents=True, exist_ok=True)
                    self.health_file.write_text(
                        json.dumps(
                            {
                                "updated_at": time.time(),
                                "connected": account.get("connection"),
                                "cursor": self.cursor,
                            }
                        )
                    )
                    self.health_file.chmod(0o600)
                    page = await self.request(
                        "GET", f"/events?after={self.cursor}&limit=100"
                    )
                    for event in page["items"]:
                        message = None
                        data = event.get("data", {})
                        if (
                            event["kind"] == "message.committed"
                            and data.get("threadId") in self.allowed_groups
                        ):
                            native = await self.request(
                                "GET", "/messages/" + quote(data["messageId"], safe="")
                            )
                            if (
                                native.get("id") != data["messageId"]
                                or native.get("threadId") != data["threadId"]
                            ):
                                raise RuntimeError("WeChat message identity mismatch")
                            if native["id"] not in self.seen:
                                message = self.convert(native, data.get("origin"))
                                if message:
                                    self.seen = (self.seen + [native["id"]])[-2048:]
                        self.cursor = event["cursor"]
                        # Persist before dispatch to avoid repeated replies after a crash.
                        state = {
                            "account_id": self.account_id,
                            "cursor": self.cursor,
                            "seen": self.seen,
                        }
                        self.state_file.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.state_file.with_suffix(".tmp")
                        temporary.write_text(json.dumps(state))
                        temporary.chmod(0o600)
                        temporary.replace(self.state_file)
                        if message:
                            self.commit_event(NativeWeChatEvent(message, self))
                            logger.info(
                                "Native WeChat admitted one mentioned group message"
                            )
                    await asyncio.sleep(1 if len(page["items"]) < 100 else 0)
                except asyncio.CancelledError:
                    raise
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    OSError,
                ) as exc:
                    logger.error("Native WeChat receive failed: %s", str(exc))
                    await asyncio.sleep(5)
        finally:
            await self.http.close()

    async def reply(self, source, text, key):
        if source.group_id not in self.allowed_groups:
            raise ValueError("Reply target is not approved")
        current = await self.request(
            "GET", "/messages/" + quote(source.message_id, safe="")
        )
        if not self.convert(current, "live"):
            raise ValueError("Original message is no longer eligible for a reply")
        intent = await self.request(
            "POST",
            "/sends",
            headers={"Idempotency-Key": key},
            json={"threadId": source.group_id, "text": text},
        )
        deadline = time.monotonic() + 65
        while (
            intent["state"] in ("queued", "submitting") and time.monotonic() < deadline
        ):
            await asyncio.sleep(1)
            intent = await self.request("GET", "/sends/" + quote(intent["id"], safe=""))
        if (
            intent.get("accountId", self.account_id) != self.account_id
            or intent.get("destination", source.group_id) != source.group_id
        ):
            raise RuntimeError("WeChat send receipt identity mismatch")
        if intent["state"] not in ("accepted", "sent", "server_accepted"):
            raise RuntimeError(
                "WeChat delivery is unconfirmed; do not automatically resend"
            )

    async def send_by_session(self, session, message_chain):
        raise ValueError("Proactive sending is disabled; reply to an admitted event")

    async def terminate(self):
        if self.http and not self.http.closed:
            await self.http.close()


@register(
    "astrbot_plugin_native_wechat", "shuizhaodemumu", "本机微信指定群文字接入", "0.2.0"
)
class NativeWeChatPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.config = config if config is not None else {}

    async def initialize(self):
        """Discover allowed groups and apply their independently selected personas."""
        manager = self.context.astrbot_config_mgr
        rules = self.config.setdefault("groups", [])
        indexed = {}
        for rule in rules:
            key = (rule.get("platform_id"), rule.get("group_id"))
            if key in indexed:
                raise ValueError(f"Duplicate WeChat group setting: {key}")
            indexed[key] = rule
        original_rules = copy.deepcopy(rules)
        for platform in manager.default_conf.get("platform", []):
            if platform.get("type") != "native_wechat" or not platform.get("enable"):
                continue
            for group in platform.get("allowed_groups", []):
                origin = f"{platform['id']}:GroupMessage:{group}"
                profile_id = manager.get_conf_info(origin)["id"]
                routes = manager.ucr.umop_to_conf_id
                if (
                    profile_id == "default"
                    or routes.get(origin) != profile_id
                    or any(k != origin and v == profile_id for k, v in routes.items())
                ):
                    profile = copy.deepcopy(dict(manager.get_conf(origin)))
                    profile["provider_settings"]["wake_prefix"] = ""
                    profile["provider_settings"]["streaming_response"] = False
                    model = profile["agent_runner"]["config"]["model"]
                    model["provider_id"] = platform["chat_provider_id"]
                    model["fallback_provider_ids"] = []
                    profile_id = await manager.create_conf(
                        profile, name=f"微信 · {group}"
                    )
                    Path(manager.confs[profile_id].config_path).chmod(0o600)
                    await manager.ucr.update_routing_data(
                        {
                            origin: profile_id,
                            **{k: v for k, v in routes.items() if k != origin},
                        }
                    )
                profile = manager.confs[profile_id]
                runner = profile["agent_runner"]
                if runner.get("runner_type", "local") != "local":
                    logger.warning("Skipping non-local WeChat profile: %s", origin)
                    continue
                persona_config = runner["config"].setdefault("persona", {})
                key = (platform["id"], group)
                rule = indexed.get(key)
                if rule is None:
                    rule = {
                        "__template_key": "group",
                        "platform_id": platform["id"],
                        "group_id": group,
                        "group_name": group,
                        "persona_id": persona_config.get("persona_id", "default"),
                        "new_conversation_on_change": True,
                    }
                    rules.append(rule)
                    indexed[key] = rule
                name = await self.fetch_group_name(platform, group)
                if name:
                    rule["group_name"] = name
                name = rule.get("group_name") or group
                if manager.get_conf_info(origin).get("name") != f"微信 · {name}":
                    await manager.update_conf_info(profile_id, name=f"微信 · {name}")
                await self.apply_group_persona(origin, profile, rule)
        if rules != original_rules and hasattr(self.config, "save_config"):
            self.config.save_config()

    async def fetch_group_name(self, platform, group):
        """Read a group label without changing membership or sending messages.

        Args:
            platform: Enabled local bridge platform configuration.
            group: An already allowed group ID.

        Returns:
            The bridge label, or None if unavailable.
        """
        if platform.get("server_url", "").rstrip("/") != "http://127.0.0.1:6174":
            return None
        try:
            token = Path(platform["token_file"]).read_text().strip()
            url = (
                "http://127.0.0.1:6174/v1/accounts/"
                + quote(platform["account_id"], safe="")
                + "/groups/"
                + quote(group, safe="")
            )
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=3),
                trust_env=False,
            ) as client:
                async with client.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
                    if data.get("id") != group:
                        return None
                    name = data.get("displayName") or data.get("name")
                    return name if isinstance(name, str) and name.strip() else None
        except (TimeoutError, OSError, aiohttp.ClientError, ValueError):
            logger.warning("Could not refresh WeChat group label: %s", group)
            return None

    async def apply_group_persona(self, origin, profile, rule):
        """Apply a selection under the same lock used by incoming model requests.

        Args:
            origin: Exact platform and group session identity.
            profile: The group's isolated AstrBot configuration.
            rule: Persona selection and conversation-switch preference.
        """
        selected = rule.get("persona_id")
        if not selected:
            return
        try:
            await self.context.persona_manager.get_persona(selected)
        except ValueError:
            logger.warning(
                "Unknown WeChat persona %r for %s; keeping current setting",
                selected,
                origin,
            )
            return
        async with session_lock_manager.acquire_lock(origin):
            conversations = self.context.conversation_manager
            cid = await conversations.get_curr_conversation_id(origin)
            conversation = (
                await conversations.get_conversation(origin, cid) if cid else None
            )
            service = (
                await sp.get_async("umo", origin, "session_service_config", {}) or {}
            )
            persona = profile["agent_runner"]["config"].setdefault("persona", {})
            previous = (
                service.get("persona_id")
                or (conversation.persona_id if conversation else None)
                or persona.get("persona_id", "default")
            )
            if previous != selected:
                if rule.get("new_conversation_on_change", True):
                    await conversations.new_conversation(
                        origin,
                        title=f"{rule.get('group_name') or origin} · {selected}",
                        persona_id=selected,
                    )
                elif cid:
                    await conversations.update_conversation(
                        origin,
                        conversation_id=cid,
                        persona_id=selected,
                    )
            if persona.get("persona_id") != selected:
                persona["persona_id"] = selected
                profile.save_config()
            if service.get("persona_id") and service["persona_id"] != selected:
                service["persona_id"] = selected
                await sp.put_async("umo", origin, "session_service_config", service)
            logger.info(
                "WeChat persona for %s (%s): %s",
                rule.get("group_name"),
                origin,
                selected,
            )
