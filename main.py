import os
import sys
import asyncio
import shlex
import time
import socket
import getpass
from pathlib import Path
from typing import Any

from astrbot.api.all import *
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

PLUGIN_NAME = "web_terminal"

# 兼容 AstrBot 的 Web API 工具
try:
    from astrbot.api.web import request, json_response
    HAS_ASTRBOT_WEB = True
except ImportError:
    HAS_ASTRBOT_WEB = False
    try:
        from quart import request, jsonify as json_response
    except ImportError:
        pass


def make_json_resp(data: Any, status: int = 200):
    """统一 JSON 响应封装"""
    if HAS_ASTRBOT_WEB:
        try:
            return json_response(data, status_code=status)
        except TypeError:
            return json_response(data)
    import json
    return json.dumps(data), status, {"Content-Type": "application/json"}


async def read_request_json() -> dict:
    """安全读取请求中的 JSON 数据"""
    try:
        if hasattr(request, "json"):
            if callable(request.json):
                return await request.json(default={})
            elif isinstance(request.json, dict):
                return request.json
        if hasattr(request, "get_json") and callable(request.get_json):
            res = await request.get_json(silent=True)
            if isinstance(res, dict):
                return res
    except Exception as e:
        logger.warning(f"[{PLUGIN_NAME}] 解析请求 JSON 失败: {e}")
    return {}


@register(PLUGIN_NAME, "Admin", "服务器应急 Web 终端", "1.0.0")
class WebTerminalPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        # 默认起始工作目录为用户主目录或当前运行目录
        self.current_cwd = str(Path.home()) if Path.home().exists() else os.getcwd()
        self.active_process = None
        self.history = []

        # 注册 Web API 路由供 WebUI 页面调用
        self._register_web_apis()

    def _register_web_apis(self):
        """注册 Web API 路由（同时注册全称和简称，避免前后端前缀不一致报错）"""
        if not hasattr(self.context, "register_web_api"):
            logger.error("[WebTerminal] 当前 AstrBot 版本不支持 context.register_web_api")
            return

        # 兼容两种常见插件名形式
        plugin_names = ["web_terminal", "astrbot_plugin_web_terminal"]

        for name in plugin_names:
            try:
                self.context.register_web_api(
                    f"/{name}/exec",
                    self.api_exec,
                    ["POST"],
                    "执行终端命令",
                )
                self.context.register_web_api(
                    f"/{name}/info",
                    self.api_info,
                    ["GET"],
                    "获取系统与终端状态",
                )
                self.context.register_web_api(
                    f"/{name}/kill",
                    self.api_kill,
                    ["POST"],
                    "终止正在运行的命令",
                )
            except Exception as e:
                logger.warning(f"[WebTerminal] 注册前缀 /{name} 异常: {e}")

        logger.info("[WebTerminal] Web 终端接口注册完成（已启用双前缀兼容）")

    async def api_info(self):
        """返回服务器基础身份与环境信息"""
        data = {
            "user": getpass.getuser(),
            "hostname": socket.gethostname(),
            "cwd": self.current_cwd,
            "os": sys.platform,
            "history": self.history[-30:],
        }
        return make_json_resp({"status": "ok", "data": data})

    async def api_kill(self):
        """强行终止卡死的命令"""
        if self.active_process and self.active_process.returncode is None:
            try:
                self.active_process.kill()
                return make_json_resp({"status": "ok", "data": {"message": "进程已终止"}})
            except Exception as e:
                return make_json_resp({"status": "error", "message": f"终止失败: {e}"}, 500)
        return make_json_resp({"status": "ok", "data": {"message": "当前无活动进程"}})

    async def api_exec(self):
        """执行命令核心接口"""
        body = await read_request_json()
        command = body.get("command", "").strip()
        custom_cwd = body.get("cwd", "").strip()
        timeout = int(body.get("timeout", 30))

        if not command:
            return make_json_resp({"status": "error", "message": "命令不能为空"}, 400)

        cwd = custom_cwd if custom_cwd and os.path.isdir(custom_cwd) else self.current_cwd
        if not os.path.isdir(cwd):
            cwd = os.getcwd()

        # 包装 Shell 命令：执行命令后输出标记和 pwd，使得 `cd` 操作能跨请求持久保留
        marker = "__ASTRBOT_TERM_PWD_MARKER__"
        wrapped_command = (
            f"cd {shlex.quote(cwd)} || exit 1\n"
            f"{{ {command}\n }} ; __ASTRBOT_RET=$? ; "
            f"echo '{marker}' ; pwd ; exit $__ASTRBOT_RET"
        )

        start_time = time.time()
        output = ""
        exit_code = -1
        new_cwd = cwd

        try:
            # 优先选择 bash，兜底选择 sh
            shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"

            proc = await asyncio.create_subprocess_exec(
                shell_bin,
                "-c",
                wrapped_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.active_process = proc

            try:
                stdout_data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                raw_output = stdout_data.decode("utf-8", errors="replace")
                exit_code = proc.returncode

                # 解析真实输出与执行后的工作目录
                if marker in raw_output:
                    parts = raw_output.rsplit(marker, 1)
                    output = parts[0].rstrip("\r\n")
                    trailing_lines = [line.strip() for line in parts[1].strip().splitlines() if line.strip()]
                    if trailing_lines:
                        candidate_cwd = trailing_lines[-1]
                        if os.path.isdir(candidate_cwd):
                            new_cwd = candidate_cwd
                            self.current_cwd = new_cwd
                else:
                    output = raw_output

            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                output = f"⚠️ 命令执行超时 (超过 {timeout} 秒已自动中断)。\n若命令在后台运行，请确认执行状态。"
                exit_code = 124
            finally:
                self.active_process = None

        except Exception as e:
            output = f"❌ 命令执行异常: {str(e)}"
            exit_code = -1

        duration = round(time.time() - start_time, 2)

        # 记录执行历史
        self.history.append({
            "command": command,
            "cwd": new_cwd,
            "exit_code": exit_code,
            "duration": duration,
            "time": time.strftime("%H:%M:%S"),
        })
        if len(self.history) > 100:
            self.history.pop(0)

        return make_json_resp({
            "status": "ok",
            "data": {
                "output": output,
                "exit_code": exit_code,
                "cwd": new_cwd,
                "duration": duration,
                "user": getpass.getuser(),
                "hostname": socket.gethostname(),
            }
        })
