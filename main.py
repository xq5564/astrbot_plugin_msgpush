"""
astrbot_plugin_msgpush - 消息推送桥接插件

在插件内启动一个带 Token 鉴权的 HTTP API 服务，外部平台调用该接口即可把
消息推送到 AstrBot 所接入的任意会话（群聊/私聊）。

两种互斥工作模式（WebUI 配置或 /推送模式 指令切换）：
- 原始模式(raw):    将原文直接转发到目标会话
- 解析模式(parse):  将原文交给目标会话的 LLM 上下文解析，把 AI 的结果发出

推送消息统一格式:
    [xxxx]推送
    内容
其中 xxxx 为来源平台名（外部接口的 source 字段，缺省用配置里的默认来源名）。

用法概览：
1. 在目标会话（如某个 QQ 群）发送: /绑定推送 <别名>
2. 外部平台调用:
   POST http://<astrbot主机>:<端口>/push/send
   Header: Authorization: Bearer <token>
   Body:   {"target": "<别名>", "content": "消息文本",
            "source": "Telegram", "images": ["图片URL", ...]}
"""

import asyncio
import json
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# 持久化数据存放于 data 目录（而非插件目录），避免更新插件时丢失
DATA_DIR = Path("data") / "msg_push"
BINDINGS_FILE = DATA_DIR / "bindings.json"
TMP_DIR = DATA_DIR / "tmp"

# umo 形如 platform:MessageType:session_id，至少包含 2 个冒号
UMO_PATTERN = re.compile(r"^[\w.\-]+:[\w.\-]+:.+")

MODE_RAW = "raw"
MODE_PARSE = "parse"


@register(
    "astrbot_plugin_msgpush",
    "CodeBuddy",
    "通过内置 HTTP API 将外部平台的消息推送到 AstrBot 指定会话。"
    "支持原始转发 / AI 解析两种互斥模式、别名绑定、文本与图片。",
    "1.1.0",
)
class MsgPushPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TMP_DIR.mkdir(parents=True, exist_ok=True)

        self.bindings: dict = self._load_bindings()

        # Token 为空时自动生成随机 Token 并写回配置
        token = str(self.config.get("token", "") or "").strip()
        if not token:
            token = secrets.token_urlsafe(24)
            self.config["token"] = token
            self.config.save_config()
        self._token = token

        # 模式合法性校验（互斥：raw / parse 二选一）
        if str(self.config.get("mode", MODE_RAW)) not in (MODE_RAW, MODE_PARSE):
            self.config["mode"] = MODE_RAW
            self.config.save_config()

        self._runner: Optional[web.AppRunner] = None
        self._server_task: Optional[asyncio.Task] = None
        self._start_server()

        host = str(self.config.get("host", "0.0.0.0"))
        port = int(self.config.get("port", 6199))
        mode = self._mode()
        mode_desc = "原始转发" if mode == MODE_RAW else "AI 解析"
        logger.info(
            f"[msgpush] 推送服务已启动: http://{host}:{port}/push/send "
            f"| 模式: {mode_desc} | 已绑定目标数: {len(self.bindings)}"
        )

    def _mode(self) -> str:
        return str(self.config.get("mode", MODE_RAW) or MODE_RAW)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_bindings(self) -> dict:
        try:
            if BINDINGS_FILE.exists():
                data = json.loads(BINDINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"[msgpush] 读取绑定文件失败: {e}")
        return {}

    def _save_bindings(self) -> None:
        try:
            BINDINGS_FILE.write_text(
                json.dumps(self.bindings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[msgpush] 写入绑定文件失败: {e}")

    # ------------------------------------------------------------------
    # HTTP 服务
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/push/sessions", self._handle_sessions)
        app.router.add_post("/push/send", self._handle_send)
        app.router.add_post("/push/bind", self._handle_bind)
        app.router.add_post("/push/unbind", self._handle_unbind)

        host = str(self.config.get("host", "0.0.0.0"))
        port = int(self.config.get("port", 6199))

        async def _run() -> None:
            try:
                self._runner = web.AppRunner(app)
                await self._runner.setup()
                site = web.TCPSite(self._runner, host=host, port=port)
                await site.start()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[msgpush] HTTP 服务启动失败(端口 {port}): {e}")

        self._server_task = asyncio.create_task(_run())

    def _check_auth(self, request: web.Request) -> bool:
        """支持三种鉴权方式: Authorization: Bearer <token> / X-Token 头 / ?token= 参数"""
        token = ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.headers.get("X-Token", "").strip()
        if not token:
            token = request.query.get("token", "").strip()
        return bool(token) and secrets.compare_digest(token, self._token)

    def _unauthorized(self) -> web.Response:
        return web.json_response(
            {"ok": False, "error": "unauthorized: token 错误或缺失"}, status=401
        )

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "plugin": "astrbot_plugin_msgpush",
                "version": "1.1.0",
                "mode": self._mode(),
                "endpoints": [
                    "POST /push/send",
                    "GET  /push/sessions",
                    "POST /push/bind",
                    "POST /push/unbind",
                ],
            }
        )

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """列出已绑定的推送目标，以及 AstrBot 内部已知的会话"""
        if not self._check_auth(request):
            return self._unauthorized()

        result = {
            "ok": True,
            "mode": self._mode(),
            "bindings": [
                {
                    "alias": alias,
                    "unified_msg_origin": info.get("umo", ""),
                    "bound_by": info.get("bound_by", ""),
                    "bound_at": info.get("bound_at", 0),
                }
                for alias, info in self.bindings.items()
            ],
            "conversations": [],
        }

        # 尝试从 AstrBot 会话管理器补充会话列表（供远程绑定 umo 使用）
        try:
            conv_mgr = self.context.conversation_manager
            convos = await conv_mgr.get_conversations()
            seen = set()
            for convo in convos:
                umo = getattr(convo, "unified_msg_origin", None)
                if umo and umo not in seen:
                    seen.add(umo)
                    result["conversations"].append(
                        {
                            "unified_msg_origin": umo,
                            "user_id": getattr(convo, "user_id", ""),
                        }
                    )
        except Exception as e:
            result["conversations_error"] = str(e)

        return web.json_response(result)

    async def _handle_send(self, request: web.Request) -> web.Response:
        """推送消息: {"target", "content", "source", "images"}"""
        if not self._check_auth(request):
            return self._unauthorized()

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "请求体必须是 JSON"}, status=400
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "error": "请求体必须是 JSON 对象"}, status=400
            )

        target = str(body.get("target", "") or body.get("alias", "") or "").strip()
        content = str(body.get("content", "") or body.get("text", "") or "").strip()
        source = (
            str(body.get("source", "") or body.get("platform", "") or "").strip()
            or str(self.config.get("default_source", "外部") or "外部")
        ).strip() or "外部"

        images = body.get("images", body.get("image", []))
        if images is None:
            images = []
        if isinstance(images, str):
            images = [images]
        images = [str(u).strip() for u in images if str(u).strip()]

        if not target:
            return web.json_response(
                {"ok": False, "error": "缺少 target(别名或 unified_msg_origin)"},
                status=400,
            )
        if not content and not images:
            return web.json_response(
                {"ok": False, "error": "content 与 images 不能同时为空"}, status=400
            )

        umo = self._resolve_target(target)
        if not umo:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"未找到推送目标 '{target}'，请先在该会话中执行 /绑定推送 {target}"
                    if target not in self.bindings
                    else "目标会话已失效",
                },
                status=404,
            )

        # 统一的推送消息格式: [xxxx]推送\n内容
        formatted = f"[{source}]推送\n{content}" if content else f"[{source}]推送"

        mode = self._mode()
        try:
            if mode == MODE_PARSE:
                result = await self._parse_and_push(umo, formatted, images)
                return web.json_response(
                    {"ok": True, "mode": MODE_PARSE, "target": target, "umo": umo,
                     "result": result}
                )
            else:
                await self._push(umo, formatted, images)
                return web.json_response(
                    {"ok": True, "mode": MODE_RAW, "target": target, "umo": umo}
                )
        except Exception as e:
            logger.error(f"[msgpush] 推送失败: {e}")
            return web.json_response(
                {"ok": False, "error": f"发送失败: {e}"}, status=500
            )

    async def _handle_bind(self, request: web.Request) -> web.Response:
        """远程绑定: {"alias": "xx", "umo": "platform:GroupMessage:12345"}"""
        if not self._check_auth(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        alias = str(body.get("alias", "")).strip()
        umo = str(body.get("umo", "") or body.get("unified_msg_origin", "")).strip()
        if not alias or not UMO_PATTERN.match(umo):
            return web.json_response(
                {
                    "ok": False,
                    "error": "需要合法的 alias 与 umo(形如 platform:GroupMessage:12345)",
                },
                status=400,
            )
        self.bindings[alias] = {
            "umo": umo,
            "bound_by": "remote",
            "bound_at": int(time.time()),
        }
        self._save_bindings()
        return web.json_response({"ok": True, "alias": alias, "umo": umo})

    async def _handle_unbind(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        alias = str(body.get("alias", "")).strip()
        if alias in self.bindings:
            del self.bindings[alias]
            self._save_bindings()
            return web.json_response({"ok": True, "alias": alias})
        return web.json_response({"ok": False, "error": f"别名 '{alias}' 不存在"}, status=404)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _resolve_target(self, target: str) -> Optional[str]:
        """别名 -> umo；若 target 本身形如 umo 则直接返回"""
        if target in self.bindings:
            return str(self.bindings[target].get("umo", ""))
        if UMO_PATTERN.match(target):
            return target
        # 大小写不敏感的别名匹配
        for alias, info in self.bindings.items():
            if alias.lower() == target.lower():
                return str(info.get("umo", ""))
        return None

    async def _push(self, umo: str, content: str, images: list) -> None:
        """原始模式：构造 MessageChain 并直接发送到指定会话"""
        prefix = str(self.config.get("push_prefix", "") or "")
        text = (prefix + content).strip() if content else ""

        chain = MessageChain()
        if text:
            chain.message(text)
        for url in images:
            local = await self._materialize_image(url)
            if local:
                chain.file_image(str(local))

        await self.context.send_message(umo, chain)

    # ------------------------------------------------------------------
    # 解析模式：交给目标会话的 LLM 上下文解析
    # ------------------------------------------------------------------

    async def _parse_and_push(self, umo: str, prompt_text: str, images: list) -> str:
        """解析模式：加载目标会话的 LLM 上下文与人格，请求 LLM，发送解析结果，
        并尽量把这一轮对话写回该会话的历史。返回 AI 的回复文本。"""
        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("AstrBot 未配置任何 LLM 提供商，无法使用解析模式")

        contexts: Optional[list] = None
        system_prompt: Optional[str] = None
        conv_mgr = None
        cid = None
        history_is_dict = False

        # 1. 加载目标会话的对话历史与人格（尽力而为，失败则无上下文解析）
        try:
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if cid:
                convo = await conv_mgr.get_conversation(umo, cid)
                if convo:
                    history = getattr(convo, "history", None)
                    if isinstance(history, str):
                        try:
                            history = json.loads(history)
                        except Exception:
                            history = None
                    if isinstance(history, list) and history:
                        contexts = history
                        history_is_dict = bool(
                            isinstance(history[0], dict)
                        )
                    # 会话人格的 system_prompt
                    try:
                        persona_id = getattr(convo, "persona_id", None)
                        pm = getattr(self.context, "persona_manager", None)
                        if persona_id and pm:
                            persona = await pm.get_persona(persona_id)
                            sp = getattr(persona, "system_prompt", None)
                            if sp:
                                system_prompt = sp
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[msgpush] 加载会话上下文失败(将无上下文解析): {e}")

        # 2. 附加解析模式提示词
        extra = str(self.config.get("parse_prompt", "") or "").strip()
        if extra:
            system_prompt = f"{system_prompt}\n\n{extra}".strip() if system_prompt else extra

        # 3. 请求 LLM（图片交给多模态模型解析）
        image_urls = [u for u in images if u.lower().startswith(("http://", "https://"))]
        resp = await provider.text_chat(
            prompt=prompt_text,
            contexts=contexts,
            system_prompt=system_prompt,
            image_urls=image_urls or None,
        )
        result = self._extract_llm_text(resp)
        if not result:
            raise RuntimeError("LLM 返回了空结果")

        # 4. 发送解析结果到会话
        prefix = str(self.config.get("push_prefix", "") or "")
        chain = MessageChain().message((prefix + result).strip())
        await self.context.send_message(umo, chain)

        # 5. 尽力把本轮对话写回会话历史（保持 AI 上下文连续）
        #    仅当历史本身是 OpenAI dict 格式时才追加，避免破坏内部结构
        try:
            if conv_mgr and cid and history_is_dict:
                new_history = list(contexts or [])
                new_history.append({"role": "user", "content": prompt_text})
                new_history.append({"role": "assistant", "content": result})
                await conv_mgr.update_conversation(umo, cid, new_history)
        except Exception as e:
            logger.warning(f"[msgpush] 写回会话历史失败(不影响本次发送): {e}")

        return result

    @staticmethod
    def _extract_llm_text(resp) -> str:
        """兼容不同版本 LLMResponse 的文本提取"""
        for attr in ("completion_text", "result_text"):
            v = getattr(resp, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        chain = getattr(resp, "result_chain", None)
        if chain:
            parts = []
            try:
                for seg in chain:
                    t = getattr(seg, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
            except Exception:
                pass
            if parts:
                return "\n".join(p for p in parts if p.strip()).strip()
        return ""

    async def _materialize_image(self, src: str) -> Optional[Path]:
        """图片来源支持: http(s) URL(自动下载) / 服务器本地路径。失败返回 None 并记日志。"""
        try:
            if src.lower().startswith(("http://", "https://")):
                max_mb = int(self.config.get("max_image_size_mb", 20))
                suffix = Path(src.split("?")[0]).suffix
                if not re.match(r"^\.[A-Za-z0-9]{1,5}$", suffix or ""):
                    suffix = ".img"
                path = TMP_DIR / f"{int(time.time() * 1000)}_{secrets.token_hex(4)}{suffix}"
                timeout = aiohttp.ClientTimeout(total=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(src) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                if len(data) > max_mb * 1024 * 1024:
                    logger.warning(f"[msgpush] 图片超过 {max_mb}MB，已跳过: {src}")
                    return None
                path.write_bytes(data)
                return path
            else:
                local = Path(src)
                if local.is_file():
                    return local
                logger.warning(f"[msgpush] 本地图片路径不存在: {src}")
                return None
        except Exception as e:
            logger.error(f"[msgpush] 处理图片失败({src}): {e}")
            return None

    # ------------------------------------------------------------------
    # 会话内指令
    # ------------------------------------------------------------------

    @filter.command("绑定推送", alias={"push_bind"})
    async def cmd_bind(self, event: AstrMessageEvent):
        """将当前会话绑定为推送目标。用法: /绑定推送 <别名>"""
        parts = event.message_str.strip().split()
        if len(parts) < 2 or not parts[1]:
            yield event.plain_result("用法: /绑定推送 <别名>\n例如: /绑定推送 群1")
            return
        alias = parts[1]
        umo = event.unified_msg_origin
        self.bindings[alias] = {
            "umo": umo,
            "bound_by": event.get_sender_name() or "",
            "bound_at": int(time.time()),
        }
        self._save_bindings()
        yield event.plain_result(
            f"已将当前会话绑定为推送目标，别名: {alias}\n"
            f"外部推送时 target 传 {alias} 即可。"
        )

    @filter.command("解绑推送", alias={"push_unbind"})
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除推送目标绑定。用法: /解绑推送 <别名>"""
        parts = event.message_str.strip().split()
        if len(parts) < 2 or not parts[1]:
            yield event.plain_result("用法: /解绑推送 <别名>")
            return
        alias = parts[1]
        if alias in self.bindings:
            del self.bindings[alias]
            self._save_bindings()
            yield event.plain_result(f"已解绑推送目标: {alias}")
        else:
            yield event.plain_result(f"别名 '{alias}' 不存在，可用 /推送列表 查看已绑定的目标")

    @filter.command("推送列表", alias={"push_list"})
    async def cmd_list(self, event: AstrMessageEvent):
        """查看所有已绑定的推送目标"""
        if not self.bindings:
            yield event.plain_result(
                "当前没有任何推送目标。\n在目标会话中发送 /绑定推送 <别名> 即可添加。"
            )
            return
        lines = ["已绑定的推送目标:"]
        for alias, info in self.bindings.items():
            bound_at = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(info.get("bound_at", 0))
            )
            lines.append(
                f"- {alias} → {info.get('umo', '')} (绑定者: {info.get('bound_by', '')}, {bound_at})"
            )
        lines.append("\n推送接口: POST /push/send, target 传别名即可。")
        yield event.plain_result("\n".join(lines))

    @filter.command("推送模式", alias={"push_mode"})
    async def cmd_mode(self, event: AstrMessageEvent):
        """查看/切换推送处理模式。用法: /推送模式 [原始|解析]"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            mode = self._mode()
            desc = (
                "原始模式：外部消息原文直接转发到会话"
                if mode == MODE_RAW
                else "解析模式：外部消息交给会话的 AI 上下文解析后发出结果"
            )
            yield event.plain_result(
                f"当前模式: {'原始' if mode == MODE_RAW else '解析'} ({mode})\n{desc}\n\n"
                "切换: /推送模式 原始  或  /推送模式 解析"
            )
            return
        arg = parts[1]
        if arg in ("原始", "raw", "RAW"):
            new_mode = MODE_RAW
            new_desc = "原始模式：外部消息原文直接转发到会话"
        elif arg in ("解析", "parse", "PARSE"):
            new_mode = MODE_PARSE
            new_desc = "解析模式：外部消息交给会话的 AI 上下文解析后发出结果"
        else:
            yield event.plain_result("参数错误。用法: /推送模式 [原始|解析]")
            return
        self.config["mode"] = new_mode
        self.config.save_config()
        yield event.plain_result(f"推送模式已切换为: {'原始' if new_mode == MODE_RAW else '解析'}\n{new_desc}")

    @filter.command("推送测试", alias={"push_test"})
    async def cmd_test(self, event: AstrMessageEvent):
        """测试向某个推送目标发消息。用法: /推送测试 <别名> <消息内容>"""
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法: /推送测试 <别名> <消息内容>")
            return
        alias, text = parts[1], parts[2]
        umo = self._resolve_target(alias)
        if not umo:
            yield event.plain_result(f"未找到推送目标 '{alias}'，可用 /推送列表 查看")
            return
        formatted = f"[测试]推送\n{text}"
        try:
            if self._mode() == MODE_PARSE:
                result = await self._parse_and_push(umo, formatted, [])
                yield event.plain_result(
                    f"已按解析模式向 '{alias}' 推送，AI 回复:\n{result[:500]}"
                )
            else:
                await self._push(umo, formatted, [])
                yield event.plain_result(f"已按原始模式向 '{alias}' 发送测试消息，请到对应会话查看。")
        except Exception as e:
            yield event.plain_result(f"发送失败: {e}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self):
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        # 清理临时图片
        try:
            for f in TMP_DIR.iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("[msgpush] 推送服务已停止。")
