"""麦麦撤回（maimai-recall）：为 MaiBot 提供消息撤回能力。

组件：
- Tool    ``recall_message``：撤回指定消息（受管理员/普通用户开关、会话作用域与 bot 群身份约束）；
- Command ``/撤回``：引用（回复）一条消息后发送「/撤回」，机器人尝试撤回被引用的消息；
- Command ``/撤回调试``：输出机器人与目标用户的群身份，并判断机器人能否撤回该消息。

撤回动作通过平台适配器的通用 action 接口（默认为 Napcat 适配器的
``adapter.napcat.action.call``）转发给协议端执行。实际删除可通过 ``/撤回`` 或
``recall_message`` 工具执行；两者共用同一套权限：MaiBot 管理员或插件设置的管理员
始终可用；普通用户默认不可用，开启 ``users_can_recall_bot_messages`` /
``users_can_recall_own_messages`` 后可分别撤回**本会话内**机器人自己的消息/
自己发出的消息。工具是否注册由 ``enable_llm_tool`` 控制（默认开启）。
进入撤回流程后，按 **bot 的 QQ 群身份** 与目标发送者身份判定平台能力：

- 群主：可撤回群内任何消息；
- 管理员：可撤回除群主消息外的消息；
- 普通成员：只能撤回自己 2 分钟内的消息；
- 私聊：只能撤回机器人自己 2 分钟内的消息，不能撤回对方的消息。

管理员名单 = 宿主 ``plugin.permission`` ∪ 插件 ``admin_users``，按纯 ID 去重；
宿主读取失败时只保留显式配置的插件管理员，两者都为空则无管理员（绝不放行所有人）。
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, ClassVar, Dict

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import CONFIG_RELOAD_SCOPE_SELF, ToolParamType, ToolParameterInfo
from pydantic import field_validator

try:  # 管理员判定统一模块（复制自 cateye_common，随插件分发）
    from .admin_util import merge_admins
except ImportError:  # 平铺目录加载时无包上下文
    from admin_util import merge_admins


def _ui_i18n(en_label: str, en_hint: str = "") -> Dict[str, Dict[str, str]]:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}

SUPPORTED_CONFIG_VERSION = "0.2.0"  # 与 _manifest.json 的 version 保持同步

# QQ 消息 ID 是带符号 int32，负数合法，不能做正整数校验
_NUMERIC_ID_RE = re.compile(r"-?\d+")

# 统一的结果文案（Command 与 Tool 共用，保证两边说法一致）
REASON_SUCCESS = "已撤回该消息。"
REASON_ASYNC_PENDING = "撤回请求已提交处理，是否成功待确认。"
REASON_NO_TARGET = "请先引用一条消息再使用「/撤回」，或在命令后面附上消息 ID。"
REASON_NOT_FOUND = "没有找到这条消息，它可能不存在或已经被撤回了。"
REASON_ADMIN_ONLY_COMMAND = "仅 MaiBot 管理员或插件设置的管理员可以使用该指令。"
REASON_TOO_FREQUENT = "操作太频繁了，请稍等片刻再试。"
REASON_BOT_MSG_ADMIN_ONLY = "未开启普通用户撤回机器人消息，只有管理员可以撤回。"
REASON_OWN_MSG_ADMIN_ONLY = "未开启普通用户撤回自己的消息，只有管理员可以撤回。"
REASON_NO_PERMISSION_OTHERS = "普通用户只能撤回自己的消息（或按配置撤回机器人消息），不能撤回他人的消息。"
REASON_SCOPE_MISMATCH = "这条消息不属于当前会话，只能撤回本会话内的消息。"
REASON_BOT_NOT_ADMIN = "机器人不是群管理员，无法撤回他人的消息。"
REASON_ADAPTER_FAILED = "撤回接口调用失败，请确认平台适配器（如 Napcat 适配器）已安装并启用。"
REASON_REJECTED_GENERIC = "撤回失败：协议端拒绝了这次撤回。"

_PERMISSION_KEYWORDS = (
    "权限",
    "管理员",
    "禁止",
    "不允许",
    "无权",
    "denied",
    "permission",
    "forbidden",
    "not admin",
    "not allowed",
    "no permission",
    "authority",
)
_MISSING_KEYWORDS = (
    "找不到",
    "不存在",
    "已撤回",
    "已被",
    "已删除",
    "not found",
    "no message",
    "invalid message",
    "not exist",
    "already recall",
)
_EXPIRED_KEYWORDS = ("超过", "过期", "时间限制", "time limit", "expire", "too old")


def plain_id(value: Any) -> str:
    """归一化用户 ID：去掉平台前缀（如 'qq:123456' -> '123456'）并转为字符串。"""
    text = str(value or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    return text


def _platform_value(value: Any) -> int | str:
    """数字 ID 转 int（NapCat 常用形态），非数字/超长数字保留字符串。"""
    text = str(value or "").strip()
    if not _NUMERIC_ID_RE.fullmatch(text) or len(text) > 20:
        return text
    try:
        return int(text)
    except ValueError:
        return text


def normalize_group_id(value: Any) -> str:
    """归一化群号：空值/'0'/'none'/'null' 视为私聊（无群身份）。"""
    text = str(value or "").strip()
    if text.lower() in ("", "0", "none", "null"):
        return ""
    return text


def parse_timestamp(value: Any) -> float | None:
    """把消息时间字段解析成秒级 UNIX 时间戳；解析失败返回 None。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    if ts > 1e12:  # 毫秒级时间戳兜底
        ts /= 1000.0
    # 限定在 2000-01-01 ~ 2100-01-01，避免异常时间戳导致本地时间格式化溢出
    if ts < 946684800 or ts > 4102444800:
        return None
    return ts


def napcat_ok(resp: dict[str, Any]) -> bool:
    """判断协议端原始响应是否明确成功（status==ok 或 retcode==0；async 不算成功）。"""
    status = str(resp.get("status") or "").strip().lower()
    if status == "ok":
        return True
    if status == "async":  # 仅表示请求已受理，结果未知，不能当成功
        return False
    return str(resp.get("retcode")).strip() == "0"


def napcat_pending(resp: dict[str, Any]) -> bool:
    """判断协议端响应是否为「已受理、结果未知」（status==async）。"""
    return str(resp.get("status") or "").strip().lower() == "async"


def format_duration(limit_sec: int) -> str:
    if limit_sec % 60 == 0:
        return f"{limit_sec // 60} 分钟"
    return f"{limit_sec} 秒"


_ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
_ROLE_VERDICT_YES = "yes"
_ROLE_VERDICT_NO = "no"
_ROLE_VERDICT_UNKNOWN = "unknown"


def normalize_role(value: Any) -> str:
    """把各平台的群身份值归一为 owner/admin/member；无法识别时返回原值小写。"""
    text = str(value or "").strip().lower()
    if text in ("owner", "creator"):
        return "owner"
    if text in ("admin", "administrator"):
        return "admin"
    if text in ("member", "normal"):
        return "member"
    return text


def role_text(role: str) -> str:
    """群身份的中文展示；空值表示查询失败。"""
    role = str(role or "").strip().lower()
    if not role:
        return "未知（查询失败）"
    return f"{_ROLE_LABELS.get(role, role)}（{role}）"


def format_message_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(keyword in text or keyword in lower for keyword in keywords)


def _is_capability_error(detail: str) -> bool:
    lower = detail.lower()
    return "e_capability" in lower or "capability" in lower or "能力调用" in detail or "能力被拒" in detail


def _is_timeout_error(detail: str) -> bool:
    lower = detail.lower()
    return "timeout" in lower or "timed out" in lower or "超时" in detail


def _is_async_status(detail: str) -> bool:
    """只匹配明确的 async 状态，避免把 asyncio.TimeoutError 误判为 async。"""
    lower = detail.lower()
    return any(
        token in lower
        for token in ("status=async", "status: async", '"status": "async"', "'status': 'async'")
    )


def translate_delete_error(
    wording: Any,
    *,
    fallback: str,
    target_is_bot_own: bool,
    bot_role: str = "",
    target_role: str = "",
) -> str:
    """把协议端撤回失败的错误信息翻译成给用户看的原因。

    只输出分类原因，不回显协议端原始文案（原文由调用方写入日志）。
    """
    text = str(wording or "").strip()
    if _is_capability_error(text):
        return "撤回接口调用失败：能力调用被拒绝，请检查插件与适配器配置。"
    if _has_any(text, _MISSING_KEYWORDS):
        return REASON_NOT_FOUND
    if _has_any(text, _EXPIRED_KEYWORDS):
        return "这条消息已经超出可撤回的时间范围，无法撤回了。"
    if _has_any(text, _PERMISSION_KEYWORDS):
        if target_is_bot_own:
            return "机器人似乎没有权限撤回这条消息，可能需要群管理员身份。"
        if bot_role == "owner":
            return "机器人是群主，但协议端仍拒绝撤回，可能是消息类型或平台限制。"
        if bot_role == "admin":
            if target_role == "owner":
                return "机器人是管理员，不能撤回群主发送的消息。"
            return "机器人是管理员，但协议端仍拒绝撤回，可能超出平台限制或消息类型不支持。"
        if bot_role == "member":
            return REASON_BOT_NOT_ADMIN
        return "协议端拒绝了撤回，且无法确认机器人的群身份，请用「/撤回调试」检查。"
    return fallback


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（plugin 配置节）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "undo"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "hint": "插件总开关",
            **_ui_i18n("Enable plugin", "Master switch for the plugin."),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "hidden": True,
            "disabled": True,
            "label": "配置版本",
            "hint": "配置版本，勿改",
            **_ui_i18n("Config version", "Do not edit."),
        },
    )


class RecallRuleConfig(PluginConfigBase):
    """撤回权限规则（rules 配置节）。"""

    __ui_label__ = "撤回规则"
    __ui_icon__ = "shield"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Recall rules", "description": "Who is allowed to recall which messages."}
    }

    admin_users: list[str] = Field(
        default_factory=list,
        description="可选：插件管理员 ID 列表；留空自动跟随宿主 plugin.permission，填写后与宿主名单合并并按纯 ID 去重",
        json_schema_extra={
            "label": "插件管理员（可选）",
            "hint": "留空自动跟随宿主；填写后与宿主名单合并去重",
            **_ui_i18n("Plugin admins (optional)", "Leave empty to follow the host; entries merge with host admins."),
        },
    )

    @field_validator("admin_users", mode="before")
    @classmethod
    def _coerce_admin_users(cls, value: Any) -> list[str]:
        """兼容旧配置里的数字/单值写法，统一转成字符串列表。"""
        if value is None:
            return []
        if isinstance(value, (str, int)):
            return [str(value)]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if item is not None]
        return []

    enable_llm_tool: bool = Field(
        default=True,
        description="是否允许 LLM 使用 recall_message 工具撤回消息；关闭后不再注册该工具",
        json_schema_extra={
            "label": "启用 LLM 撤回工具",
            "hint": "默认开启；关闭后不再注册该工具（重载插件生效）",
            **_ui_i18n(
                "Enable LLM recall tool",
                "Default on; when off the tool is not registered until plugin reload.",
            ),
        },
    )
    users_can_recall_bot_messages: bool = Field(
        default=False,
        description="允许普通用户撤回本会话内机器人自己发出的消息（仍受 bot 群身份与 2 分钟时限约束）",
        json_schema_extra={
            "label": "允许普通用户撤回机器人消息",
            "hint": "默认关闭；开启后仅限本会话内机器人自己的消息",
            **_ui_i18n(
                "Allow users to recall bot messages",
                "Default off; only in the current chat and still subject to the bot's group role.",
            ),
        },
    )
    users_can_recall_own_messages: bool = Field(
        default=False,
        description="允许普通用户撤回本会话内自己发出的消息（需要机器人具备群主/管理员身份，且仍受 bot 群身份限制）",
        json_schema_extra={
            "label": "允许普通用户撤回自己的消息",
            "hint": "默认关闭；开启后仅限本会话内自己的消息",
            **_ui_i18n(
                "Allow users to recall own messages",
                "Default off; only in the current chat and still subject to the bot's group role.",
            ),
        },
    )
    self_recall_time_limit_sec: int = Field(
        default=120,
        ge=1,
        le=86400,
        description="机器人以普通成员身份撤回自己消息的时间限制（秒）；QQ 平台为 2 分钟，机器人是群主/管理员时不受限",
        json_schema_extra={
            "label": "撤回时限（秒）",
            "hint": "普通成员撤回自身消息的时限（秒）",
            **_ui_i18n("Self-recall time limit (sec)", "Time window for a regular-member bot to recall its own messages."),
        },
    )


class AdapterApiConfig(PluginConfigBase):
    """适配器接口设置（adapter 配置节）。"""

    __ui_label__ = "适配器接口"
    __ui_icon__ = "cable"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Adapter API", "description": "Which adapter action endpoint to call."}
    }

    action_api: str = Field(
        default="adapter.napcat.action.call",
        description="适配器通用 action 调用 API 名；撤回与消息查询都通过它转发给协议端",
        json_schema_extra={
            "label": "适配器 action API",
            "hint": "适配器 action 接口",
            **_ui_i18n("Adapter action API", "Generic adapter action endpoint."),
        },
    )


class RecallLimitConfig(PluginConfigBase):
    """频率限制（limits 配置节）。"""

    __ui_label__ = "频率限制"
    __ui_icon__ = "timer"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Rate limit", "description": "Cooldown between recall attempts."}
    }

    cooldown_sec: int = Field(
        default=10,
        ge=0,
        le=3600,
        description="同一会话两次撤回操作之间的最小间隔（秒），0 表示不限制",
        json_schema_extra={
            "label": "冷却间隔（秒）",
            "hint": "每会话冷却秒数",
            **_ui_i18n("Cooldown (sec)", "Minimum interval per chat; 0 disables the limit."),
        },
    )


class MaiRecallConfig(PluginConfigBase):
    """麦麦撤回完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    rules: RecallRuleConfig = Field(default_factory=RecallRuleConfig)
    adapter: AdapterApiConfig = Field(default_factory=AdapterApiConfig)
    limits: RecallLimitConfig = Field(default_factory=RecallLimitConfig)


class MaiRecallPlugin(MaiBotPlugin):
    """麦麦撤回主类：提供 recall_message Tool、/撤回 与 /撤回调试 Command。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = MaiRecallConfig

    def __init__(self) -> None:
        super().__init__()
        self._self_id = ""
        self._last_recall_at: dict[str, float] = {}

    def get_components(self) -> list[dict[str, Any]]:
        """按配置注册组件：关闭 LLM 工具开关时不注册 recall_message。"""
        components = super().get_components()
        if isinstance(components, dict):
            components = list(components.values())
        else:
            components = list(components or [])
        try:
            config = self.config
        except Exception:
            config = None
        if config is not None and not config.rules.enable_llm_tool:
            components = [c for c in components if str(c.get("name") or "") != "recall_message"]
        return components

    # ------------------------------------------------------------------
    # 生命周期（1.2.3 硬性要求，缺一即加载失败）
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        admins = await self._collect_admins()
        self.ctx.logger.info(
            "麦麦撤回已加载：管理员（宿主 plugin.permission + 插件 admin_users，去重后）%d 人，"
            "LLM 撤回工具 %s，自身消息撤回时限 %d 秒，每会话冷却 %d 秒，适配器接口 %s",
            len(admins),
            "开启" if self.config.rules.enable_llm_tool else "关闭（不注册）",
            self.config.rules.self_recall_time_limit_sec,
            max(0, int(self.config.limits.cooldown_sec)),
            self.config.adapter.action_api,
        )
        if not admins:
            self.ctx.logger.warning(
                "麦麦撤回：未读取到任何管理员（plugin.permission 为空或不可读），普通聊天用户均按非管理员处理。"
            )

    async def on_unload(self) -> None:
        self._self_id = ""
        self._last_recall_at.clear()
        self.ctx.logger.info("麦麦撤回已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._self_id = ""
            self._last_recall_at.clear()
            admins = await self._collect_admins()
            self.ctx.logger.info("麦麦撤回配置已热更新（version=%s，管理员 %d 人）", version, len(admins))

    # ------------------------------------------------------------------
    # 组件：LLM 工具
    # ------------------------------------------------------------------

    @Tool(
        "recall_message",
        brief_description="撤回一条指定的消息（含权限校验）",
        detailed_description=(
            "撤回指定消息 ID 对应的消息（QQ 平台消息 ID，可能是负数）。\n"
            "使用条件：仅在用户明确要求撤回/删除某条消息时才调用，不要主动撤回。\n"
            "权限：发起者需是 MaiBot 管理员或插件设置的管理员，或插件开关允许的普通用户"
            "（仅限本会话内机器人自己的消息/自己的消息）。\n"
            "限制：撤回前会解析目标并比较 bot 与目标发送者的 QQ 群身份（群主可撤任何消息，"
            "管理员不能撤群主消息，普通成员只能撤自己 2 分钟内的消息）；私聊中只能撤回机器人自己"
            "2 分钟内的消息。\n"
            "参数：message_id（string，要撤回的消息 ID）；当用户引用/回复了某条消息但没有给出 ID 时"
            "可以留空，插件会尝试撤回被引用的那条消息。\n"
            "返回：success 表示是否撤回成功；content 为结果说明；失败时 content 会给出具体原因"
            "（如无权限、消息发出超过两分钟、消息不存在等），可直接转述给用户。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id",
                param_type=ToolParamType.STRING,
                description="要撤回的消息 ID（QQ 平台消息 ID，支持负数）；用户引用消息时可留空",
                required=False,
                default="",
            ),
        ],
        core_tool=True,
    )
    async def tool_recall_message(self, message_id: Any = "", **kwargs: Any) -> dict[str, Any]:
        if not self.config.rules.enable_llm_tool:
            reason = "LLM 撤回工具已关闭。"
            return {"content": reason, "success": False, "error": reason}

        target = str(message_id or "").strip()
        if not target:
            target = self._extract_reply_target(kwargs) or ""

        # Tool 参数由 LLM 生成，不能信任其中的 user_id/is_local_operator/platform；
        # 发起者只从当前流最近的人类消息恢复，并用该消息校准会话上下文。
        recent = await self._recent_trigger_message(kwargs)
        requester = self._extract_requester({"message": recent}) if recent is not None else ""
        if not target and recent is not None:
            target = self._extract_reply_target({"message": recent}) or ""
        if not target:
            return {"content": REASON_NO_TARGET, "success": False, "error": REASON_NO_TARGET}

        tool_context = self._tool_context_from_recent(kwargs, recent)
        if not await self._is_admin(requester, tool_context):
            allowed, reason = await self._user_recall_allowed(requester, target, tool_context)
            if not allowed:
                self.ctx.logger.info(
                    "Tool recall_message 被拒绝：%s -> %s", requester or "unknown", reason
                )
                return {"content": reason, "success": False, "error": reason}

        chat_key = self._chat_key(tool_context)
        if self._cooldown_hit(chat_key):
            return {"content": REASON_TOO_FREQUENT, "success": False, "error": REASON_TOO_FREQUENT}

        ok, reason = await self._do_recall(target)
        self.ctx.logger.info("Tool recall_message(%s) by %s -> %s", target, requester or "unknown", reason)
        return {"content": reason, "success": ok, "error": "" if ok else reason}

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
        requester = self._extract_requester(kwargs)
        # 引用回复时命令文本在被回复内容之后，explicit ID 优先，其次取回复段里的目标消息 ID
        target = str((kwargs.get("matched_groups") or {}).get("target_id") or "").strip()
        if not target:
            target = self._extract_reply_target(kwargs) or ""
        if not target:
            await self.ctx.send.text(REASON_NO_TARGET, stream_id)
            return True, "no target", True

        if not await self._is_admin(requester, kwargs):
            allowed, deny_reason = await self._user_recall_allowed(requester, target, kwargs)
            if not allowed:
                reason = deny_reason or REASON_NO_PERMISSION_OTHERS
                if self._cooldown_hit(f"denied:{requester or stream_id or 'unknown'}"):
                    return True, reason, True
                self.ctx.logger.info("Command /撤回 被拒绝：%s -> %s", requester or "unknown", reason)
                await self.ctx.send.text(reason, stream_id)
                return True, reason, True

        chat_key = self._chat_key(kwargs)
        if self._cooldown_hit(chat_key):
            reason = REASON_TOO_FREQUENT
        else:
            ok, reason = await self._do_recall(target)
        self.ctx.logger.info("Command /撤回(%s) by %s -> %s", target, requester or "unknown", reason)
        await self.ctx.send.text(reason, stream_id)
        return True, reason, True

    # ------------------------------------------------------------------
    # 组件：/撤回调试 命令
    # ------------------------------------------------------------------

    @Command(
        "recall_debug",
        description="撤回调试：查看机器人与目标用户的群身份，并判断机器人能否撤回该消息",
        pattern=r"(?i)(?<!\S)(?:/?撤回调试|/?撤回debug|/recall_debug)(?:\s+(?P<target_id>-?\d+))?\s*$",
    )
    async def cmd_recall_debug(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        requester = self._extract_requester(kwargs)
        if not await self._is_admin(requester, kwargs):
            reason = REASON_ADMIN_ONLY_COMMAND
            if self._cooldown_hit(f"denied:{requester or stream_id or 'unknown'}"):
                return True, reason, True
            self.ctx.logger.info("Command /撤回调试 被拒绝：%s 不是管理员", requester or "unknown")
            await self.ctx.send.text(reason, stream_id)
            return True, reason, True
        target = str((kwargs.get("matched_groups") or {}).get("target_id") or "").strip()
        if not target:
            target = self._extract_reply_target(kwargs) or ""
        report = await self._build_recall_debug_report(target, kwargs)
        await self.ctx.send.text(report, stream_id)
        self.ctx.logger.info(
            "Command /撤回调试 by %s（目标=%s）-> %s",
            requester or "unknown",
            target or "未引用",
            report.replace("\n", " | "),
        )
        return True, "recall debug", True

    # ------------------------------------------------------------------
    # 撤回核心流程
    # ------------------------------------------------------------------

    async def _do_recall(self, target_raw: str) -> tuple[bool, str]:
        """撤回核心流程：解析目标 → bot/目标发送者群身份判定 → 调用协议端撤回。

        调用方（Command）已完成发起者权限校验（管理员，或开关允许的普通用户且
        目标在本会话内）；这里只按 QQ 群身份关系与私聊规则判断机器人平台能力。
        """
        target_raw = str(target_raw or "").strip()
        if not target_raw:
            return False, REASON_NO_TARGET

        pid_text, info, resolve_err = await self._resolve_target(target_raw)
        if info is None:
            return False, resolve_err or REASON_NOT_FOUND

        verdict, capability_reason, bot_role, target_role, is_bot_own = await self._evaluate_target(info)
        if verdict == _ROLE_VERDICT_NO:
            self.ctx.logger.info("撤回 %s 被群身份规则拒绝：%s", target_raw, capability_reason)
            return False, capability_reason
        if verdict == _ROLE_VERDICT_UNKNOWN:
            self.ctx.logger.warning(
                "撤回 %s 的群身份能力无法确认，仍交由协议端校验：%s", target_raw, capability_reason
            )

        if not pid_text:
            return False, REASON_NOT_FOUND
        return await self._delete_platform_message(
            pid_text,
            is_bot_own=is_bot_own,
            bot_role=bot_role,
            target_role=target_role,
        )

    async def _user_recall_allowed(
        self,
        requester: str,
        target_raw: str,
        kwargs: dict[str, Any],
    ) -> tuple[bool, str]:
        """普通用户撤回权限：仅按插件开关放行机器人消息/自己的消息，并限制本会话。"""
        if not requester:
            return False, REASON_NO_PERMISSION_OTHERS
        platform = str(kwargs.get("platform") or "").strip().lower()
        if platform and platform != "qq":
            return False, REASON_NO_PERMISSION_OTHERS
        pid_text, info, resolve_err = await self._resolve_target(target_raw)
        if info is None:
            return False, resolve_err or REASON_NOT_FOUND
        sender_id = plain_id(info.get("sender_id"))
        self_id = plain_id(await self._get_self_id()) or plain_id(info.get("self_id"))
        rules = self.config.rules
        if self_id and sender_id == self_id:
            if not rules.users_can_recall_bot_messages:
                return False, REASON_BOT_MSG_ADMIN_ONLY
        elif sender_id == plain_id(requester):
            if not rules.users_can_recall_own_messages:
                return False, REASON_OWN_MSG_ADMIN_ONLY
        else:
            return False, REASON_NO_PERMISSION_OTHERS
        if not await self._scope_allowed(
            target_raw,
            pid_text=pid_text,
            target_group=info.get("group_id"),
            stream_id=str(kwargs.get("stream_id") or ""),
            trigger_group=self._extract_trigger_group(kwargs),
        ):
            return False, REASON_SCOPE_MISMATCH
        return True, ""

    async def _scope_allowed(
        self,
        target_text: str,
        *,
        pid_text: str,
        target_group: str,
        stream_id: str,
        trigger_group: str,
    ) -> bool:
        """校验普通用户只能撤回当前会话内的消息（群聊比群号，私聊比会话流）。"""
        target_group = normalize_group_id(target_group)
        trigger_group = normalize_group_id(trigger_group)
        if trigger_group:
            return target_group == trigger_group
        if target_group or not stream_id:
            return False
        for mid in (target_text, pid_text):
            if not mid:
                continue
            target_stream = await self._get_db_stream_id(mid)
            if target_stream:
                return target_stream == stream_id
        return False

    async def _get_db_stream_id(self, message_id: str) -> str:
        """从宿主消息记录里取该消息所属会话流 ID；取不到返回空字符串。"""
        msg = await self._get_db_message(message_id)
        if not msg:
            return ""
        message_info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
        for source in (msg, message_info):
            for key in ("session_id", "stream_id", "chat_id", "chat_stream_id"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    async def _member_role_for_info(self, info: dict[str, Any], user_id: str) -> str:
        group_id = normalize_group_id(info.get("group_id"))
        if not group_id or not user_id:
            return ""
        return await self._get_member_role(group_id, user_id)

    async def _evaluate_target(self, info: dict[str, Any]) -> tuple[str, str, str, str, bool]:
        """按目标消息信息计算 (verdict, 说明, bot角色, 目标角色, 是否 bot 自己)。"""
        sender_id = plain_id(info.get("sender_id"))
        msg_time = parse_timestamp(info.get("time"))
        target_group = normalize_group_id(info.get("group_id"))
        is_group = bool(target_group) or bool(info.get("is_group"))
        limit = max(1, int(self.config.rules.self_recall_time_limit_sec))
        self_id = plain_id(await self._get_self_id()) or plain_id(info.get("self_id"))
        is_bot_own = bool(self_id) and sender_id == self_id
        bot_role = await self._member_role_for_info(info, self_id) if (is_group and target_group and self_id) else ""
        if is_bot_own:
            target_role = bot_role
        else:
            target_role = await self._member_role_for_info(info, sender_id) if (is_group and target_group and sender_id) else ""
        verdict, reason = self._recall_capability(
            group_id=target_group,
            bot_role=bot_role,
            target_role=target_role,
            is_bot_own=is_bot_own,
            msg_time=msg_time,
            limit_sec=limit,
            is_group=is_group,
            bot_identity_known=bool(self_id),
        )
        return verdict, reason, bot_role, target_role, is_bot_own

    @staticmethod
    def _recall_capability(
        *,
        group_id: str,
        bot_role: str,
        target_role: str,
        is_bot_own: bool,
        msg_time: float | None,
        limit_sec: int,
        is_group: bool | None = None,
        bot_identity_known: bool = True,
    ) -> tuple[str, str]:
        """按 bot 的 QQ 群身份与目标发送者群身份判断能否撤回。

        私聊（is_group=False，默认由 group_id 推断）单独处理：bot 只能撤回自己
        2 分钟内的消息。返回 (verdict, 说明)，verdict 为 yes/no/unknown。
        """
        if is_group is None:
            is_group = bool(group_id)
        limit_text = format_duration(limit_sec)
        if not is_group:
            if not is_bot_own and not bot_identity_known:
                return (
                    _ROLE_VERDICT_UNKNOWN,
                    "无法确认机器人账号，不能判断该私聊消息是否为机器人自己发送；需由协议端校验。",
                )
            if not is_bot_own:
                return (
                    _ROLE_VERDICT_NO,
                    f"私聊中没有群身份；机器人只能撤回自己 {limit_text}内的消息，无法撤回对方的消息。",
                )
            if msg_time is None:
                return (
                    _ROLE_VERDICT_UNKNOWN,
                    f"私聊中机器人只能撤回自己 {limit_text}内的消息；无法读取消息时间，需由协议端校验。",
                )
            if time.time() - msg_time >= limit_sec:
                return (
                    _ROLE_VERDICT_NO,
                    f"私聊中机器人只能撤回自己 {limit_text}内的消息；该消息已发出超过 {limit_text}，无法撤回。",
                )
            return _ROLE_VERDICT_YES, f"私聊中机器人只能撤回自己 {limit_text}内的消息；该消息在时限内。"

        if bot_role == "owner":
            return _ROLE_VERDICT_YES, "机器人是群主，可撤回群内任何消息。"
        if bot_role == "admin":
            if is_bot_own:
                return _ROLE_VERDICT_YES, "机器人是管理员，可以撤回自己的消息。"
            if target_role == "owner":
                return _ROLE_VERDICT_NO, "机器人是管理员，不能撤回群主发送的消息。"
            if target_role in ("admin", "member"):
                return _ROLE_VERDICT_YES, "机器人是管理员，可撤回除群主外成员的消息。"
            return (
                _ROLE_VERDICT_UNKNOWN,
                "机器人是管理员，但无法获取目标用户的群身份，不能确认目标是否为群主。",
            )
        if bot_role == "member":
            if not is_bot_own:
                who = {"owner": "群主", "admin": "管理员", "member": "普通成员"}.get(target_role, "他人")
                return (
                    _ROLE_VERDICT_NO,
                    f"机器人是普通成员，只能撤回自己 {limit_text}内的消息，不能撤回{who}发送的消息。",
                )
            if msg_time is None:
                return (
                    _ROLE_VERDICT_UNKNOWN,
                    f"机器人是普通成员，只能撤回自己 {limit_text}内的消息；无法读取消息时间，需由协议端校验。",
                )
            if time.time() - msg_time >= limit_sec:
                return (
                    _ROLE_VERDICT_NO,
                    f"机器人是普通成员，只能撤回自己 {limit_text}内的消息；该消息已发出超过 {limit_text}，无法撤回。",
                )
            return _ROLE_VERDICT_YES, f"机器人是普通成员，只能撤回自己 {limit_text}内的消息；该消息在时限内。"

        return _ROLE_VERDICT_UNKNOWN, "无法获取机器人的群身份，不能确认撤回权限。"

    async def _delete_platform_message(
        self,
        pid_text: str,
        *,
        is_bot_own: bool,
        bot_role: str = "",
        target_role: str = "",
    ) -> tuple[bool, str]:
        """调用适配器 delete_msg 执行撤回，并把失败翻译成可读原因（不回显原始错误）。"""
        try:
            message_id = int(pid_text)
        except ValueError:
            return False, REASON_NOT_FOUND
        resp, host_err = await self._call_adapter("delete_msg", {"message_id": message_id})
        if resp is None:
            return False, self._delete_error_text(
                host_err,
                target_is_bot_own=is_bot_own,
                bot_role=bot_role,
                target_role=target_role,
            )
        if napcat_ok(resp):
            return True, REASON_SUCCESS
        if napcat_pending(resp):
            # status=async 仅表示协议端已受理、结果未知，不做成功保证
            self.ctx.logger.info("delete_msg(%s) 已被协议端受理（status=async），结果待确认", pid_text)
            return False, REASON_ASYNC_PENDING
        self.ctx.logger.warning("delete_msg(%s) 被协议端拒绝：%s", pid_text, resp)
        wording = resp.get("wording") or resp.get("message") or resp.get("error")
        return False, translate_delete_error(
            wording,
            fallback=REASON_REJECTED_GENERIC,
            target_is_bot_own=is_bot_own,
            bot_role=bot_role,
            target_role=target_role,
        )

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
            if len(text) > 20:  # QQ 消息 ID 是 int64，超长串不可能是有效 ID
                return "", None, REASON_NOT_FOUND
            info, host_err = await self._get_platform_msg_info(int(text))
            need_db = (
                info is None
                or not plain_id(info.get("sender_id"))
                or not normalize_group_id(info.get("group_id"))
            )
            db_info: dict[str, Any] | None = None
            if need_db:
                db_msg = await self._get_db_message(text)
                if db_msg:
                    db_info = self._info_from_db(db_msg)
            info = self._merge_msg_info(info, db_info)
            if info is None:
                # 适配器异常时按查询失败处理；响应非 ok 但无异常时按消息不存在处理
                if host_err:
                    return "", None, self._query_error_text(host_err)
                return "", None, REASON_NOT_FOUND
            return text, info, ""

        # 非纯数字 ID（宿主内部消息 ID）：先查库换出平台消息 ID
        db_msg = await self._get_db_message(text)
        if not db_msg:
            return "", None, REASON_NOT_FOUND
        db_info = self._info_from_db(db_msg)
        pid_text = self._numeric_pid_from_db(db_msg)
        info = None
        if pid_text and len(pid_text) <= 20:
            info, _ = await self._get_platform_msg_info(int(pid_text))
        info = self._merge_msg_info(info, db_info)
        return pid_text, info, ""

    @staticmethod
    def _merge_msg_info(
        primary: dict[str, Any] | None,
        fallback: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """用数据库兜底补齐 get_msg 缺失的发送者/时间/群号/self_id 字段。"""
        if primary is None:
            return fallback
        merged = dict(primary)
        if isinstance(fallback, dict):
            for key in ("sender_id", "time", "group_id", "self_id"):
                if not merged.get(key) and fallback.get(key):
                    merged[key] = fallback[key]
            merged["is_group"] = bool(merged.get("is_group")) or bool(fallback.get("is_group"))
        return merged

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
        group_id = normalize_group_id(data.get("group_id"))
        message_type = str(data.get("message_type") or data.get("chat_type") or "").strip().lower()
        return {
            "sender_id": str(sender.get("user_id") or data.get("user_id") or ""),
            "time": data.get("time"),
            "group_id": group_id,
            "self_id": str(data.get("self_id") or ""),
            "is_group": bool(group_id) or message_type in ("group", "guild"),
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
        additional = msg.get("additional_config") if isinstance(msg.get("additional_config"), dict) else {}
        if not additional and isinstance(message_info.get("additional_config"), dict):
            additional = message_info["additional_config"]
        message_type = str(
            message_info.get("message_type")
            or msg.get("message_type")
            or additional.get("message_type")
            or additional.get("chat_type")
            or ""
        ).strip().lower()
        ts = msg.get("time")
        if ts in (None, ""):
            ts = msg.get("timestamp")
        return {
            "sender_id": str(user_info.get("user_id") or ""),
            "time": ts,
            "group_id": normalize_group_id(group_id),
            "self_id": str(additional.get("self_id") or ""),
            "is_group": bool(normalize_group_id(group_id)) or bool(group_info) or message_type in ("group", "guild"),
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
            return None, "适配器返回了无法识别的数据"
        return resp, ""

    @staticmethod
    def _query_error_text(host_err: str) -> str:
        """消息详情查询失败文案；区分能力/超时/API 不可用/未命中。"""
        detail = str(host_err or "").strip()
        lower = detail.lower()
        if _is_capability_error(detail):
            return "消息查询失败：能力调用被拒绝，请检查插件与适配器配置。"
        if _is_timeout_error(detail):
            return "消息查询失败：适配器响应超时，请稍后再试。"
        if any(
            token in lower
            for token in (
                "api not found",
                "action not found",
                "no handler",
                "未找到适配器",
                "未加载",
                "not loaded",
                "未找到 api",
                "api 不存在",
                "未找到目标插件",
                "not installed",
            )
        ):
            return "消息查询失败：适配器或 API 不可用，请确认适配器已安装并启用。"
        if _has_any(detail, _MISSING_KEYWORDS):
            return REASON_NOT_FOUND
        if _has_any(detail, _PERMISSION_KEYWORDS):
            return "消息查询失败：协议端拒绝查询（权限不足）。"
        return "没有找到这条消息，或消息查询失败。"

    @staticmethod
    def _delete_error_text(
        host_err: str,
        *,
        target_is_bot_own: bool = False,
        bot_role: str = "",
        target_role: str = "",
    ) -> str:
        """delete_msg 异常文案；优先按协议端权限/超时/不存在分类。"""
        detail = str(host_err or "").strip()
        if _is_async_status(detail):
            return REASON_ASYNC_PENDING
        if _is_capability_error(detail):
            return "撤回接口调用失败：能力调用被拒绝，请检查插件与适配器配置。"
        if _has_any(detail, _MISSING_KEYWORDS):
            return REASON_NOT_FOUND
        if _has_any(detail, _EXPIRED_KEYWORDS):
            return "这条消息已经超出可撤回的时间范围，无法撤回了。"
        if _has_any(detail, _PERMISSION_KEYWORDS):
            return translate_delete_error(
                detail,
                fallback=REASON_REJECTED_GENERIC,
                target_is_bot_own=target_is_bot_own,
                bot_role=bot_role,
                target_role=target_role,
            )
        if _is_timeout_error(detail):
            return "撤回接口调用失败：适配器响应超时，请稍后再试。"
        return REASON_ADAPTER_FAILED

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

    async def _get_member_role(self, group_id: str, user_id: str) -> str:
        """查询指定用户在群内的角色（owner/admin/member）；失败返回空字符串。

        每次实时查询（no_cache），避免机器人刚被提升/降权时沿用旧身份。
        """
        if not group_id or not user_id:
            return ""
        params: dict[str, Any] = {
            "group_id": _platform_value(group_id),
            "user_id": _platform_value(user_id),
            "no_cache": True,
        }
        resp, host_err = await self._call_adapter("get_group_member_info", params)
        if resp is None or not napcat_ok(resp):
            if host_err:
                self.ctx.logger.debug(
                    "get_group_member_info(group=%s, user=%s) 失败：%s", group_id, user_id, host_err
                )
            else:
                self.ctx.logger.debug(
                    "get_group_member_info(group=%s, user=%s) 未命中：%s", group_id, user_id, resp
                )
            return ""
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        return normalize_role(data.get("role"))

    # ------------------------------------------------------------------
    # 上下文提取与频控
    # ------------------------------------------------------------------

    def _chat_key(self, kwargs: dict[str, Any]) -> str:
        """取频控用的会话键：优先会话流 ID，其次群号，再次触发者。"""
        return (
            str(kwargs.get("stream_id") or "").strip()
            or self._extract_trigger_group(kwargs)
            or self._extract_requester(kwargs)
            or "unknown"
        )

    def _cooldown_hit(self, chat_key: str) -> bool:
        """每会话冷却：间隔内再次触发返回 True，并记录本次放行时间。"""
        limit = max(0, int(self.config.limits.cooldown_sec))
        if limit <= 0:
            return False
        now = time.time()
        if now - self._last_recall_at.get(chat_key, 0.0) < limit:
            return True
        self._last_recall_at[chat_key] = now
        if len(self._last_recall_at) > 512:
            cutoff = now - max(limit * 10, 600)
            self._last_recall_at = {key: ts for key, ts in self._last_recall_at.items() if ts >= cutoff}
        return False

    @staticmethod
    def _extract_trigger_group(kwargs: dict[str, Any]) -> str:
        """从组件 kwargs 里取触发会话的群号（私聊为空字符串）。

        命令路径的 kwargs 直接带 ``group_id``；Tool 路径也由宿主注入同名字段，
        但未必带完整 ``message``，因此必须优先读 ``group_id``。
        """
        direct = normalize_group_id(kwargs.get("group_id"))
        if direct:
            return direct
        message = kwargs.get("message") if isinstance(kwargs.get("message"), dict) else {}
        message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = message.get("group_info") if isinstance(message.get("group_info"), dict) else {}
        if not group_info and isinstance(message_info.get("group_info"), dict):
            group_info = message_info["group_info"]
        return normalize_group_id(group_info.get("group_id"))

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

    @staticmethod
    def _tool_context_from_recent(kwargs: dict[str, Any], recent: dict[str, Any] | None) -> dict[str, Any]:
        """构造 Tool 权限上下文：忽略 LLM 可伪造的身份/本地标记，平台固定 QQ。

        若恢复了最近人类消息，则用它校准 stream_id/group_id/message，避免直接采信
        LLM 传入的会话字段。
        """
        context = {
            key: value
            for key, value in kwargs.items()
            if key not in ("user_id", "is_local_operator", "platform")
        }
        context["platform"] = "qq"
        if isinstance(recent, dict):
            info = recent.get("message_info") if isinstance(recent.get("message_info"), dict) else {}
            group_info = recent.get("group_info") if isinstance(recent.get("group_info"), dict) else {}
            if not group_info and isinstance(info.get("group_info"), dict):
                group_info = info["group_info"]
            context["group_id"] = normalize_group_id(group_info.get("group_id"))
            session_id = str(recent.get("session_id") or info.get("session_id") or "").strip()
            if session_id:
                context["stream_id"] = session_id
            message_platform = str(recent.get("platform") or info.get("platform") or "").strip().lower()
            if message_platform:
                context["platform"] = message_platform
            context["message"] = recent
        return context

    async def _is_admin(self, requester: str, kwargs: dict[str, Any]) -> bool:
        """判定触发者是否管理员：本地控制台/CLI 天然放行。

        其余触发者比对宿主 `plugin.permission` 与插件 `admin_users` 的并集，
        经 plain_id 归一去重，'qq:10001' 与 '10001' 视为同一人。
        """
        platform = str(kwargs.get("platform") or "").strip().lower()
        if platform and platform not in ("qq", "bot_console", "maisaka_cli"):
            # 本插件按 QQ 群身份判定；其他平台的同号用户不应继承 QQ 管理员身份
            return False
        if kwargs.get("is_local_operator") or platform in ("bot_console", "maisaka_cli"):
            return True
        if not requester:
            return False
        # 触发者 ID 兜底归一（正常路径已在 _extract_requester 去前缀）
        return plain_id(requester) in await self._collect_admins()

    async def _collect_admins(self) -> list[str]:
        """管理员名单 = 宿主 `plugin.permission` ∪ 插件 `admin_users`，按纯 ID 去重。

        宿主名单读取失败时只保留显式配置的插件管理员；插件列表也为空则无管理员
        （fail-closed，绝不放行所有人）。宿主名单为空时同样只按插件列表处理。
        """
        plugin_admins = merge_admins(self.config.rules.admin_users)
        try:
            perms = await self.ctx.config.get("plugin.permission", None)
        except Exception as exc:
            self.ctx.logger.warning(
                "读取宿主 plugin.permission 失败，仅使用插件管理员名单（%d 人）：%s", len(plugin_admins), exc
            )
            return plugin_admins
        if isinstance(perms, dict) and perms.get("success") is False:
            self.ctx.logger.warning(
                "读取宿主 plugin.permission 失败，仅使用插件管理员名单（%d 人）：%s",
                len(plugin_admins),
                perms.get("error") or "未知错误",
            )
            return plugin_admins
        return merge_admins(perms, plugin_admins)

    async def _recent_trigger_message(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """从当前流最近消息中恢复被引用目标（Tool 上下文缺少完整 message 时使用）。

        取最近 10 分钟内最新的一条人类消息（跳过机器人、通知与命令消息），
        用于读取其 reply 段里的被引用消息 ID。
        """
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return None
        try:
            messages = await self.ctx.message.get_recent(stream_id, limit=10)
        except Exception as exc:
            self.ctx.logger.debug("message.get_recent(%s) 失败：%s", stream_id, exc)
            return None
        if isinstance(messages, dict):
            messages = messages.get("messages")
        if not isinstance(messages, list):
            return None
        self_id = plain_id(await self._get_self_id())
        for raw in reversed(messages):
            if not isinstance(raw, dict):
                continue
            if raw.get("is_notify") or raw.get("is_command"):
                continue
            info = raw.get("message_info") if isinstance(raw.get("message_info"), dict) else {}
            user_info = raw.get("user_info") if isinstance(raw.get("user_info"), dict) else {}
            if not user_info and isinstance(info.get("user_info"), dict):
                user_info = info["user_info"]
            user_id = plain_id(user_info.get("user_id"))
            if not user_id or (self_id and user_id == self_id):
                continue
            ts = parse_timestamp(raw.get("timestamp") or raw.get("time"))
            if ts is not None and time.time() - ts > 600:
                return None
            return raw
        return None

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

    async def _build_recall_debug_report(self, target_raw: str, kwargs: dict[str, Any]) -> str:
        """生成 /撤回调试 报告：双方群身份 + 能力比较结论。

        引用消息时以被引用消息的发送者为目标；未引用时以命令发送者为目标。
        """
        requester = self._extract_requester(kwargs)
        trigger_group = self._extract_trigger_group(kwargs)
        self_id = plain_id(await self._get_self_id())
        limit = max(1, int(self.config.rules.self_recall_time_limit_sec))

        target_info: dict[str, Any] | None = None
        target_pid = ""
        target_group = ""
        sender_id = requester
        msg_time: float | None = None
        resolve_err = ""
        if target_raw:
            target_pid, target_info, resolve_err = await self._resolve_target(target_raw)
            if target_info is not None:
                sender_id = plain_id(target_info.get("sender_id"))
                msg_time = parse_timestamp(target_info.get("time"))
                target_group = normalize_group_id(target_info.get("group_id"))
                self_id = self_id or plain_id(target_info.get("self_id"))

        role_group = target_group if target_raw else trigger_group
        if target_raw and target_info is None:
            # 目标解析失败时仍展示当前会话的机器人身份，便于排查
            role_group = trigger_group
        is_group = bool(role_group) or bool(target_info.get("is_group")) if target_info is not None else bool(role_group)
        target_is_bot = bool(self_id) and bool(sender_id) and sender_id == self_id
        bot_role = await self._get_member_role(role_group, self_id) if (role_group and self_id) else ""

        lines: list[str] = ["撤回调试"]
        if target_raw:
            if target_info is None:
                lines.append(f"目标消息：{target_raw}")
                lines.append(f"解析结果：失败（{resolve_err or REASON_NOT_FOUND}）")
            else:
                lines.append(f"目标消息：{target_pid or target_raw}（发送者 {sender_id or '未知'}）")
        else:
            lines.append("目标：未引用消息，按命令发送者判定")

        if role_group:
            lines.append(f"群号：{role_group}")
        elif is_group:
            lines.append("会话：群聊（群号未知）")
        else:
            lines.append("会话：私聊（无群身份）")

        if not self_id:
            bot_role_display = "未知（无法获取机器人账号）"
        elif is_group and not role_group:
            bot_role_display = "未知（群号未知）"
        elif not is_group:
            bot_role_display = "私聊（无群身份）"
        else:
            bot_role_display = role_text(bot_role)
        lines.append(f"机器人：{self_id or '未知'}（{bot_role_display}）")

        if target_is_bot:
            target_role = bot_role
        elif role_group and sender_id:
            target_role = await self._get_member_role(role_group, sender_id)
        else:
            target_role = ""

        if not sender_id:
            lines.append("目标用户：未知")
        else:
            if role_group:
                target_role_display = role_text(target_role)
            elif is_group:
                target_role_display = "未知（群号未知）"
            else:
                target_role_display = "私聊（无群身份）"
            target_label = "机器人自己" if target_is_bot else sender_id
            lines.append(f"目标用户：{target_label}（{target_role_display}）")

        if target_info is not None:
            if msg_time is None:
                lines.append("消息时间：未知")
            else:
                age = max(0, int(time.time() - msg_time))
                lines.append(f"消息时间：{format_message_time(msg_time)}（已发出 {age} 秒）")
        elif not target_raw:
            lines.append("消息时间：无（未引用消息）")

        if target_raw and target_info is not None and not target_pid:
            lines.append("判定：未解析到平台消息 ID，无法调用撤回接口。")
            lines.append("结论：无法撤回该消息。")
            return "\n".join(lines)

        if target_raw and target_info is None:
            lines.append("结论：无法判定（无法解析目标消息及其发送者）。")
            return "\n".join(lines)

        verdict, reason = self._recall_capability(
            group_id=role_group,
            bot_role=bot_role,
            target_role=target_role,
            is_bot_own=target_is_bot,
            msg_time=msg_time,
            limit_sec=limit,
            is_group=is_group,
            bot_identity_known=bool(self_id),
        )
        lines.append(f"判定：{reason}")
        conclusion = {
            _ROLE_VERDICT_YES: "可以",
            _ROLE_VERDICT_NO: "不可以",
            _ROLE_VERDICT_UNKNOWN: "无法判定",
        }[verdict]
        target_desc = "该消息" if target_raw else "该用户的消息"
        lines.append(f"结论：{conclusion}撤回{target_desc}。")
        if verdict == _ROLE_VERDICT_UNKNOWN:
            lines.append("说明：无法确认时会交由协议端最终校验。")
        return "\n".join(lines)


def create_plugin() -> MaiRecallPlugin:
    """Runner 加载入口。"""
    return MaiRecallPlugin()
