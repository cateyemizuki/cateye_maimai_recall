"""麦麦撤回（maimai-recall）：为 MaiBot 提供消息撤回能力。

组件：
- Tool    ``recall_message``：LLM 工具，传入消息 ID 即可让机器人撤回指定消息；
- Command ``/撤回``：引用（回复）一条消息后发送「/撤回」，机器人尝试撤回被引用的消息。

撤回动作通过平台适配器的通用 action 接口（默认为 Napcat 适配器的
``adapter.napcat.action.call``）转发给协议端执行；插件侧负责权限与时间
限制判定，并把协议端返回的错误翻译成可读的失败原因，例如：

- 非管理员撤回他人消息 → "你不是管理员，不能撤回他人的消息。"
- 机器人自身的消息超过时限 → "这条消息已经发出超过 2 分钟，无法撤回了。"
"""

from __future__ import annotations

import re
import time
from typing import Any, ClassVar

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import CONFIG_RELOAD_SCOPE_SELF, ToolParamType, ToolParameterInfo

SUPPORTED_CONFIG_VERSION = "0.1.0"  # 与 _manifest.json 的 version 保持同步

# QQ 消息 ID 是带符号 int32，负数合法，不能做正整数校验
_NUMERIC_ID_RE = re.compile(r"-?\d+")

# 统一的结果文案（Command 与 Tool 共用，保证两边说法一致）
REASON_SUCCESS = "已撤回该消息。"
REASON_NO_TARGET = "请先引用一条消息再使用「/撤回」，或在命令后面附上消息 ID。"
REASON_NOT_FOUND = "没有找到这条消息，它可能不存在或已经被撤回了。"
REASON_NO_PERMISSION_OTHERS = "你不是管理员，不能撤回他人的消息。"
REASON_BOT_NOT_ADMIN = "机器人不是群管理员，无法撤回他人的消息。"
REASON_BOT_MSG_ADMIN_ONLY = "只有管理员才能让机器人撤回它自己的消息。"

_PERMISSION_KEYWORDS = ("权限", "管理员", "denied", "permission", "forbidden", "not admin", "authority")
_MISSING_KEYWORDS = ("找不到", "不存在", "已撤回", "not found", "no message", "invalid message", "not exist", "already recall")
_EXPIRED_KEYWORDS = ("超过", "过期", "时间限制", "time limit", "expire", "too old")


def plain_id(value: Any) -> str:
    """归一化用户 ID：去掉平台前缀（如 'qq:123456' -> '123456'）并转为字符串。"""
    text = str(value or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    return text


def parse_timestamp(value: Any) -> float | None:
    """把消息时间字段解析成秒级 UNIX 时间戳；解析失败返回 None。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e12:  # 毫秒级时间戳兜底
        ts /= 1000.0
    return ts


def napcat_ok(resp: dict[str, Any]) -> bool:
    """判断协议端原始响应是否成功（status==ok 或 retcode==0；async 视为已受理）。"""
    status = str(resp.get("status") or "").strip().lower()
    if status in ("ok", "async"):
        return True
    return str(resp.get("retcode")).strip() == "0"


def format_duration(limit_sec: int) -> str:
    if limit_sec % 60 == 0:
        return f"{limit_sec // 60} 分钟"
    return f"{limit_sec} 秒"


def too_old_text(limit_sec: int) -> str:
    return f"这条消息已经发出超过 {format_duration(limit_sec)}，无法撤回了。"


def translate_delete_error(wording: Any, *, fallback: str, target_is_bot_own: bool) -> str:
    """把协议端撤回失败的错误信息翻译成给用户看的原因。"""
    text = str(wording or "").strip()
    lower = text.lower()
    if any(k in text or k in lower for k in _PERMISSION_KEYWORDS):
        if target_is_bot_own:
            return "机器人似乎没有权限撤回这条消息，可能需要群管理员身份。"
        return REASON_BOT_NOT_ADMIN
    if any(k in text or k in lower for k in _MISSING_KEYWORDS):
        return REASON_NOT_FOUND
    if any(k in text or k in lower for k in _EXPIRED_KEYWORDS):
        return "这条消息已经超出可撤回的时间范围，无法撤回了。"
    return fallback if not text else f"撤回失败：{text}"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "undo"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class RecallRuleConfig(PluginConfigBase):
    """撤回权限规则。"""

    __ui_label__ = "撤回规则"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    admin_users: list[str] = Field(
        default_factory=list,
        description="管理员用户 ID 列表，可以撤回任何消息；支持 '123456' 或 'qq:123456' 两种写法",
    )
    users_can_recall_bot_messages: bool = Field(
        default=True,
        description="允许普通用户让机器人撤回机器人自己发出的消息（仍受时间限制约束）",
    )
    users_can_recall_own_messages: bool = Field(
        default=False,
        description="允许普通用户撤回自己发出的消息（需要机器人具备群管理员身份，否则协议端会拒绝）",
    )
    self_recall_time_limit_sec: int = Field(
        default=120,
        ge=1,
        le=86400,
        description="机器人撤回自己消息的时间限制（秒）；QQ 平台限制为 2 分钟，机器人是群管理员时不受限",
    )


class AdapterApiConfig(PluginConfigBase):
    """适配器接口设置。"""

    __ui_label__ = "适配器接口"
    __ui_icon__ = "cable"
    __ui_order__ = 2

    action_api: str = Field(
        default="adapter.napcat.action.call",
        description="适配器通用 action 调用 API 名；撤回与消息查询都通过它转发给协议端",
    )


class MaiRecallConfig(PluginConfigBase):
    """麦麦撤回完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    rules: RecallRuleConfig = Field(default_factory=RecallRuleConfig)
    adapter: AdapterApiConfig = Field(default_factory=AdapterApiConfig)


class MaiRecallPlugin(MaiBotPlugin):
    """麦麦撤回主类：提供 recall_message Tool 与 /撤回 Command。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = MaiRecallConfig

    _BOT_ROLE_CACHE_TTL_SEC = 90.0

    def __init__(self) -> None:
        super().__init__()
        self._self_id = ""
        self._bot_role_cache: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # 生命周期（1.2.3 硬性要求，缺一即加载失败）
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "麦麦撤回已加载：管理员 %d 人，自身消息撤回时限 %d 秒，适配器接口 %s",
            len(self.config.rules.admin_users),
            self.config.rules.self_recall_time_limit_sec,
            self.config.adapter.action_api,
        )

    async def on_unload(self) -> None:
        self._self_id = ""
        self._bot_role_cache.clear()
        self.ctx.logger.info("麦麦撤回已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._self_id = ""
            self._bot_role_cache.clear()
            self.ctx.logger.info("麦麦撤回配置已热更新（version=%s）", version)

    # ------------------------------------------------------------------
    # 组件：LLM 工具
    # ------------------------------------------------------------------

    @Tool(
        "recall_message",
        brief_description="撤回一条指定的消息",
        detailed_description=(
            "撤回指定消息 ID 对应的消息（QQ 平台消息 ID，可能是负数）。\n"
            "使用条件：仅在用户明确要求撤回/删除某条消息时才调用，不要主动撤回消息。\n"
            "参数：message_id（string，要撤回的消息 ID）；当用户引用/回复了某条消息并要求撤回"
            "但没有给出 ID 时，可以留空，插件会尝试撤回被引用的那条消息。\n"
            "返回：success 表示是否撤回成功，content 为结果说明；失败时 content 会给出具体原因"
            "（如权限不足、消息发出超过两分钟、消息不存在等），可直接转述给用户。\n"
            "如果用户想撤回你刚发出的某条消息但你不知道它的消息 ID，可以提示用户引用那条消息"
            "并发送「/撤回」。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id",
                param_type=ToolParamType.STRING,
                description="要撤回的消息 ID（QQ 平台消息 ID，支持负数）；用户引用消息要求撤回时可留空",
                required=False,
                default="",
            ),
        ],
        core_tool=True,
    )
    async def tool_recall_message(self, message_id: Any = "", **kwargs: Any) -> dict[str, Any]:
        target = str(message_id or "").strip()
        if not target:
            target = self._extract_reply_target(kwargs) or ""
        if not target:
            return {"content": REASON_NO_TARGET, "success": False}
        ok, reason = await self._do_recall(
            target,
            requester=self._extract_requester(kwargs),
            is_admin=self._is_admin(self._extract_requester(kwargs), kwargs),
        )
        self.ctx.logger.info("Tool recall_message(%s) -> %s", target, reason)
        return {"content": reason, "success": ok}

    # ------------------------------------------------------------------
    # 组件：/撤回 命令
    # ------------------------------------------------------------------

    @Command(
        "recall",
        description="撤回消息：引用一条消息发送「/撤回」，也可用「/撤回 <消息ID>」",
        pattern=r"(?i)(?<!\S)(?:/?撤回|/recall)(?:\s+(?P<target_id>-?\d+))?\s*$",
    )
    async def cmd_recall(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        # 引用回复时命令文本在被回复内容之后，explicit ID 优先，其次取回复段里的目标消息 ID
        target = str((kwargs.get("matched_groups") or {}).get("target_id") or "").strip()
        if not target:
            target = self._extract_reply_target(kwargs) or ""
        if not target:
            await self.ctx.send.text(REASON_NO_TARGET, stream_id)
            return True, "no target", True

        requester = self._extract_requester(kwargs)
        ok, reason = await self._do_recall(target, requester=requester, is_admin=self._is_admin(requester, kwargs))
        self.ctx.logger.info("Command /撤回(%s) by %s -> %s", target, requester or "unknown", reason)
        await self.ctx.send.text(reason, stream_id)
        return True, reason, True

    # ------------------------------------------------------------------
    # 撤回核心流程
    # ------------------------------------------------------------------

    async def _do_recall(self, target_raw: str, *, requester: str, is_admin: bool) -> tuple[bool, str]:
        """撤回核心流程：解析目标 → 权限判定 → 时间限制 → 调用协议端撤回。

        返回 (是否成功, 给用户看的文案)；失败时文案即失败原因。
        """
        target_raw = str(target_raw or "").strip()
        if not target_raw:
            return False, REASON_NO_TARGET

        pid_text, info, resolve_err = await self._resolve_target(target_raw)
        if info is None:
            return False, resolve_err or REASON_NOT_FOUND

        sender_id = plain_id(info.get("sender_id"))
        msg_time = parse_timestamp(info.get("time"))
        group_id = str(info.get("group_id") or "").strip()
        rules = self.config.rules

        self_id = plain_id(await self._get_self_id())
        is_bot_own = bool(self_id) and sender_id == self_id

        # 权限判定：管理员可撤回任何消息；普通用户默认只能撤回机器人自己的消息，
        # 也可按配置放开"撤回自己发的消息"。
        if is_bot_own:
            if not is_admin and not rules.users_can_recall_bot_messages:
                return False, REASON_BOT_MSG_ADMIN_ONLY
        else:
            own_message = bool(requester) and sender_id == plain_id(requester)
            if not is_admin and not (rules.users_can_recall_own_messages and own_message):
                return False, REASON_NO_PERMISSION_OTHERS

        # 时间限制：撤回机器人自己的消息时，若机器人不是群管理员/群主，
        # 平台只允许在限定时间（默认 2 分钟）内撤回；撤回他人消息没有时间限制，
        # 权限由协议端校验。
        if is_bot_own and msg_time is not None:
            bot_role = await self._get_bot_role(group_id) if group_id else None
            if bot_role not in ("owner", "admin"):
                limit = max(1, int(rules.self_recall_time_limit_sec))
                if time.time() - msg_time > limit:
                    return False, too_old_text(limit)

        if not pid_text:
            return False, REASON_NOT_FOUND
        return await self._delete_platform_message(pid_text, is_bot_own=is_bot_own)

    async def _delete_platform_message(self, pid_text: str, *, is_bot_own: bool) -> tuple[bool, str]:
        """调用适配器 delete_msg 执行撤回，并把失败翻译成可读原因。"""
        try:
            message_id = int(pid_text)
        except ValueError:
            return False, REASON_NOT_FOUND
        resp, host_err = await self._call_adapter("delete_msg", {"message_id": message_id})
        if resp is None:
            return False, self._adapter_error_text(host_err)
        if napcat_ok(resp):
            return True, REASON_SUCCESS
        self.ctx.logger.warning("delete_msg(%s) 被协议端拒绝：%s", pid_text, resp)
        fallback = f"撤回失败：协议端返回 retcode={resp.get('retcode')}。"
        wording = resp.get("wording") or resp.get("message") or resp.get("error")
        return False, translate_delete_error(wording, fallback=fallback, target_is_bot_own=is_bot_own)

    # ------------------------------------------------------------------
    # 目标消息解析
    # ------------------------------------------------------------------

    async def _resolve_target(self, target_raw: str) -> tuple[str, dict[str, Any] | None, str]:
        """把传入的消息 ID 解析成平台消息 ID 与消息信息。

        返回 (平台消息ID文本, 消息信息, 失败原因)；成功时失败原因为空字符串。
        消息信息优先取协议端 get_msg（权威），失败时回退宿主数据库。
        """
        text = target_raw.strip()
        if _NUMERIC_ID_RE.fullmatch(text):
            info, host_err = await self._get_platform_msg_info(int(text))
            if info is None:
                db_msg = await self._get_db_message(text)
                if db_msg:
                    info = self._info_from_db(db_msg)
            if info is None:
                return "", None, host_err  # 适配器不可用等宿主层错误，原样上报
            return text, info, ""

        # 非纯数字 ID（宿主内部消息 ID）：先查库换出平台消息 ID
        db_msg = await self._get_db_message(text)
        if not db_msg:
            return "", None, REASON_NOT_FOUND
        pid_text = self._numeric_pid_from_db(db_msg)
        info = None
        if pid_text:
            info, _ = await self._get_platform_msg_info(int(pid_text))
        if info is None:
            info = self._info_from_db(db_msg)
        return pid_text, info, ""

    async def _get_platform_msg_info(self, message_id: int) -> tuple[dict[str, Any] | None, str]:
        """通过适配器 get_msg 查询消息（发送者/时间/群号）。"""
        resp, host_err = await self._call_adapter("get_msg", {"message_id": message_id})
        if resp is None:
            return None, host_err
        if not napcat_ok(resp):
            self.ctx.logger.debug("get_msg(%s) 未命中：%s", message_id, resp)
            return None, ""
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        return {
            "sender_id": str(sender.get("user_id") or ""),
            "time": data.get("time"),
            "group_id": str(data.get("group_id") or ""),
        }, ""

    async def _get_db_message(self, message_id: str) -> dict[str, Any] | None:
        """从宿主数据库按消息 ID 查记录（兜底用）。"""
        try:
            resp = await self.ctx.message.get_by_id(message_id)
        except Exception as exc:
            self.ctx.logger.debug("message.get_by_id(%s) 查询失败：%s", message_id, exc)
            return None
        if not isinstance(resp, dict) or resp.get("success") is False:
            return None
        for key in ("message", "result", "data"):
            inner = resp.get(key)
            if isinstance(inner, dict) and ("user_info" in inner or "message_info" in inner):
                return inner
        if "user_info" in resp or "message_info" in resp:
            return resp
        return None

    @staticmethod
    def _numeric_pid_from_db(msg: dict[str, Any]) -> str:
        """从消息记录里取数字形态的平台消息 ID。"""
        for key in ("platform_message_id", "message_id"):
            value = str(msg.get(key) or "").strip()
            if value and _NUMERIC_ID_RE.fullmatch(value):
                return value
        return ""

    @staticmethod
    def _info_from_db(msg: dict[str, Any]) -> dict[str, Any]:
        """从宿主消息记录里提取撤回判定需要的字段（发送者/时间/群号）。"""
        message_info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
        user_info = msg.get("user_info") if isinstance(msg.get("user_info"), dict) else {}
        if not user_info and isinstance(message_info.get("user_info"), dict):
            user_info = message_info["user_info"]
        group_info = msg.get("group_info") if isinstance(msg.get("group_info"), dict) else {}
        if not group_info and isinstance(message_info.get("group_info"), dict):
            group_info = message_info["group_info"]
        group_id = group_info.get("group_id") or msg.get("group_id") or message_info.get("group_id") or ""
        ts = msg.get("time")
        if ts in (None, ""):
            ts = msg.get("timestamp")
        return {
            "sender_id": str(user_info.get("user_id") or ""),
            "time": ts,
            "group_id": str(group_id or ""),
        }

    # ------------------------------------------------------------------
    # 适配器调用与账号信息缓存
    # ------------------------------------------------------------------

    async def _call_adapter(self, action_name: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """调用适配器通用 action 入口。返回 (NapCat 原始响应, 宿主层错误)；resp 为 None 时读错误。"""
        api_name = str(self.config.adapter.action_api or "").strip()
        if not api_name:
            return None, "未配置适配器 action API 名"
        try:
            resp = await self.ctx.api.call(api_name, action_name=action_name, params=params)
        except Exception as exc:  # 能力被拒 / RPC 异常等
            self.ctx.logger.warning("调用适配器 API %s(%s) 异常：%s", api_name, action_name, exc)
            return None, str(exc)
        if isinstance(resp, dict) and resp.get("success") is False:
            error = str(resp.get("error") or "适配器 API 调用失败")
            self.ctx.logger.warning("适配器 API %s(%s) 返回失败：%s", api_name, action_name, error)
            return None, error
        if not isinstance(resp, dict):
            return None, f"适配器返回了无法识别的数据：{type(resp).__name__}"
        return resp, ""

    @staticmethod
    def _adapter_error_text(host_err: str) -> str:
        detail = str(host_err or "").strip() or "适配器调用失败"
        return f"撤回接口调用失败：{detail}。请确认平台适配器（如 Napcat 适配器）已安装并启用。"

    async def _get_self_id(self) -> str:
        """查询并缓存机器人自己的账号 ID（get_login_info）。"""
        if self._self_id:
            return self._self_id
        resp, host_err = await self._call_adapter("get_login_info", {})
        if resp is not None and napcat_ok(resp):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            user_id = data.get("user_id") or data.get("uin") or ""
            if user_id:
                self._self_id = str(user_id)
                self.ctx.logger.debug("机器人账号 self_id=%s", self._self_id)
                return self._self_id
        if host_err:
            self.ctx.logger.debug("get_login_info 失败：%s", host_err)
        return ""

    async def _get_bot_role(self, group_id: str) -> str | None:
        """查询机器人在群内的角色（owner/admin/member），带短 TTL 缓存。"""
        if not group_id:
            return None
        now = time.time()
        cached = self._bot_role_cache.get(group_id)
        if cached and now - cached[1] < self._BOT_ROLE_CACHE_TTL_SEC:
            return cached[0]
        self_id = await self._get_self_id()
        if not self_id:
            return None
        params: dict[str, Any] = {"user_id": int(self_id), "group_id": int(group_id) if _NUMERIC_ID_RE.fullmatch(group_id) else group_id}
        resp, host_err = await self._call_adapter("get_group_member_info", params)
        if resp is None or not napcat_ok(resp):
            if host_err:
                self.ctx.logger.debug("get_group_member_info(%s) 失败：%s", group_id, host_err)
            return None
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        role = str(data.get("role") or "") or None
        if role:
            self._bot_role_cache[group_id] = (role, now)
        return role

    # ------------------------------------------------------------------
    # 上下文提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_requester(kwargs: dict[str, Any]) -> str:
        """从组件 kwargs 里取触发者的用户 ID。"""
        message = kwargs.get("message") if isinstance(kwargs.get("message"), dict) else {}
        message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user_info = message.get("user_info") if isinstance(message.get("user_info"), dict) else {}
        if not user_info and isinstance(message_info.get("user_info"), dict):
            user_info = message_info["user_info"]
        user_id = kwargs.get("user_id") or user_info.get("user_id") or ""
        return plain_id(user_id)

    def _is_admin(self, requester: str, kwargs: dict[str, Any]) -> bool:
        """判定触发者是否管理员：本地控制台天然放行，其余比对配置的管理员列表。"""
        if kwargs.get("is_local_operator"):
            return True
        if not requester:
            return False
        admins = {plain_id(u) for u in self.config.rules.admin_users}
        admins.discard("")
        return requester in admins

    @staticmethod
    def _extract_reply_target(kwargs: dict[str, Any]) -> str | None:
        """从触发消息的 reply 段里取被引用消息的 ID。"""
        message = kwargs.get("message") if isinstance(kwargs.get("message"), dict) else {}
        segments = message.get("raw_message")
        if not isinstance(segments, list):
            return None
        for seg in segments:
            if not isinstance(seg, dict) or str(seg.get("type") or "") != "reply":
                continue
            data = seg.get("data")
            if isinstance(data, dict):
                for key in ("target_message_id", "message_id", "id"):
                    value = data.get(key)
                    if value not in (None, ""):
                        return str(value).strip()
            elif isinstance(data, (str, int)):
                return str(data).strip()
        return None


def create_plugin() -> MaiRecallPlugin:
    """Runner 加载入口。"""
    return MaiRecallPlugin()
