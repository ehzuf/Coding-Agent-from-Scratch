"""
Demo 工具：获取当前时间

用于验证 Tool Use 流程的最简单示例。
LLM 无法直接获取当前时间，必须通过工具来查询。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from .base import Tool


class GetCurrentTimeTool(Tool):
    """获取当前时间的工具。"""

    @property
    def name(self) -> str:
        return "get_current_time"

    @property
    def description(self) -> str:
        return "获取当前日期和时间。可选参数 timezone 指定时区，默认为本地时区。"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "时区名称，如 Asia/Shanghai、America/New_York、UTC 等",
                },
                "format": {
                    "type": "string",
                    "description": "时间格式，如 %Y-%m-%d %H:%M:%S，默认为 ISO 格式",
                },
            },
            "required": [],
        }

    def is_concurrency_safe(self, input: dict) -> bool:
        return True  # 纯函数，无副作用

    def call(self, input: dict) -> str:
        timezone_name = input.get("timezone")
        fmt = input.get("format", "%Y-%m-%d %H:%M:%S")

        try:
            if timezone_name:
                tz = ZoneInfo(timezone_name)
                now = datetime.now(tz)
            else:
                now = datetime.now()

            result = now.strftime(fmt)
            tz_name = timezone_name or "本地时区"
            return f"当前时间（{tz_name}）: {result}"

        except Exception as e:
            return f"错误：无法获取时间 - {e}"
