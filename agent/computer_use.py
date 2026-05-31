"""
Computer Use Agent — 通过截图理解屏幕、通过鼠标/键盘操作 GUI

核心循环：
  1. 发送用户指令给 Claude（带 computer 工具定义）
  2. Claude 返回 tool_use（screenshot / click / type / ...）
  3. 执行操作，截图，将截图作为 tool_result 返回
  4. Claude 看到新截图，决定下一步操作
  5. 直到 Claude 返回 end_turn

与普通 Tool Use 的关键区别：
  - 必须使用 beta header：betas=["computer-use-2025-11-24"]
  - 工具定义是 schema-less 的：Anthropic 定义 schema，用户只提供 type + 屏幕尺寸
  - tool_result 包含图片（base64），不是纯文本
"""

import base64
import subprocess
import sys

import anthropic


# ============================================================================
# 配置
# ============================================================================

MAX_STEPS = 50

COMPUTER_USE_BETA = "computer-use-2025-11-24"
COMPUTER_USE_MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = (
    "你是一个桌面操作助手。用户会给你一个任务，你需要通过截图观察屏幕，"
    "然后用鼠标和键盘操作来完成任务。每次操作后都会收到新的截图。"
    "请一步步完成任务，完成后告诉用户结果。"
)


# ============================================================================
# 截图与坐标缩放
# ============================================================================

def take_screenshot(
    target_width: int | None = None,
    target_height: int | None = None,
) -> tuple[str, int, int]:
    """
    截取当前屏幕，返回 (base64_png, actual_width, actual_height)。

    actual_width/actual_height 是屏幕原始分辨率（缩放前），
    用于将 Claude 的坐标映射回真实屏幕坐标。

    如果提供了 target_width/target_height，截图会缩放到该尺寸，
    确保发给 Claude 的图片与 display_width_px/display_height_px 一致。
    """
    if sys.platform == "darwin":
        result = subprocess.run(
            ["screencapture", "-x", "-t", "png", "/tmp/_cu_screenshot.png"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"screencapture failed: {result.stderr.decode()}")
    else:
        result = subprocess.run(
            ["scrot", "-o", "/tmp/_cu_screenshot.png"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Screenshot failed. Install scrot: sudo apt install scrot"
            )

    with open("/tmp/_cu_screenshot.png", "rb") as f:
        raw_bytes = f.read()

    actual_width, actual_height = _get_png_dimensions(raw_bytes)

    # 缩放到目标尺寸（与 display_width_px/display_height_px 对齐）
    if target_width and target_height:
        raw_bytes = _scale_to(raw_bytes, actual_width, actual_height,
                              target_width, target_height)

    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return b64, actual_width, actual_height


def _get_png_dimensions(data: bytes) -> tuple[int, int]:
    """从 PNG 文件头读取宽高（IHDR chunk，固定偏移）。"""
    # PNG: 8 byte signature + 4 byte length + 4 byte "IHDR" + 4 byte width + 4 byte height
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _scale_to(
    data: bytes, src_w: int, src_h: int, dst_w: int, dst_h: int,
) -> bytes:
    """将截图缩放到指定尺寸。如果已经匹配则直接返回。"""
    if src_w == dst_w and src_h == dst_h:
        return data

    if sys.platform == "darwin":
        subprocess.run(
            ["sips", "-z", str(dst_h), str(dst_w), "/tmp/_cu_screenshot.png"],
            capture_output=True,
        )
    else:
        subprocess.run(
            ["convert", "/tmp/_cu_screenshot.png",
             "-resize", f"{dst_w}x{dst_h}!",
             "/tmp/_cu_screenshot.png"],
            capture_output=True,
        )

    with open("/tmp/_cu_screenshot.png", "rb") as f:
        return f.read()


def scale_coordinates(
    x: int, y: int, from_width: int, from_height: int,
    to_width: int, to_height: int,
) -> tuple[int, int]:
    """将 LLM 返回的坐标从 API 坐标系映射到实际屏幕坐标。"""
    scale_x = to_width / from_width
    scale_y = to_height / from_height
    return int(x * scale_x), int(y * scale_y)


# ============================================================================
# 操作执行
# ============================================================================

def execute_action(
    action: str,
    coordinate: list[int] | None = None,
    text: str | None = None,
    scroll_direction: str | None = None,
    scroll_amount: int | None = None,
    start_coordinate: list[int] | None = None,
    duration: int | None = None,
    *,
    dry_run: bool = True,
) -> str:
    """
    执行一个 Computer Use 操作。

    注意：key action 的按键内容也通过 text 参数传入（与 Anthropic API 一致）。
    dry_run=True 时只打印日志不实际执行。
    实际执行需要 pyautogui（pip install pyautogui）。
    """
    desc = f"[Action] {action}"
    if coordinate:
        desc += f" at ({coordinate[0]}, {coordinate[1]})"
    if text:
        desc += f" text={text!r}"
    if scroll_direction:
        desc += f" scroll_direction={scroll_direction!r} scroll_amount={scroll_amount}"

    if dry_run:
        print(f"  🖥️  {desc} (dry-run, 未实际执行)")
        return desc

    # 实际执行（需要 pyautogui）
    try:
        import pyautogui
    except ImportError:
        return f"Error: pyautogui not installed. Run: pip install pyautogui"

    pyautogui.FAILSAFE = True

    if action == "screenshot":
        pass  # 截图在调用方处理
    elif action == "left_click" and coordinate:
        pyautogui.click(coordinate[0], coordinate[1])
    elif action == "right_click" and coordinate:
        pyautogui.rightClick(coordinate[0], coordinate[1])
    elif action == "double_click" and coordinate:
        pyautogui.doubleClick(coordinate[0], coordinate[1])
    elif action == "type" and text:
        if text.isascii():
            pyautogui.typewrite(text, interval=0.02)
        else:
            # typewrite 不支持非 ASCII，改用剪贴板粘贴
            import subprocess
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            pyautogui.hotkey("command", "v")
    elif action == "key" and text:
        pyautogui.hotkey(*text.split("+"))
    elif action == "mouse_move" and coordinate:
        pyautogui.moveTo(coordinate[0], coordinate[1])
    elif action == "scroll" and coordinate:
        clicks = scroll_amount or 3
        if scroll_direction in ("up", "left"):
            clicks = clicks
        else:
            clicks = -clicks
        pyautogui.scroll(clicks, coordinate[0], coordinate[1])
    elif action == "left_click_drag" and coordinate and start_coordinate:
        pyautogui.moveTo(start_coordinate[0], start_coordinate[1])
        pyautogui.drag(
            coordinate[0] - start_coordinate[0],
            coordinate[1] - start_coordinate[1],
            duration=0.5,
        )
    elif action == "wait":
        import time
        time.sleep(2)
    else:
        return f"Unknown or incomplete action: {action}"

    return desc


# ============================================================================
# ComputerUseAgent
# ============================================================================

class ComputerUseAgent:
    """
    Computer Use Agent——通过 Anthropic beta API 驱动屏幕操作。

    使用方式：
        agent = ComputerUseAgent(dry_run=True)
        result = agent.run("打开浏览器搜索 Python 教程")
    """

    def __init__(
        self,
        display_width: int = 1024,
        display_height: int = 768,
        model: str = COMPUTER_USE_MODEL,
        dry_run: bool = True,
        max_steps: int = MAX_STEPS,
    ):
        self.client = anthropic.Anthropic()
        self.display_width = display_width
        self.display_height = display_height
        self.model = model
        self.dry_run = dry_run
        self.max_steps = max_steps
        self.messages: list[dict] = []
        # 实际屏幕尺寸（首次截图时获取）
        self._actual_width: int | None = None
        self._actual_height: int | None = None

    def _build_tools(self) -> list[dict]:
        """构建 Computer Use 工具定义（schema-less 格式）。"""
        return [
            {
                "type": "computer_20251124",
                "name": "computer",
                "display_width_px": self.display_width,
                "display_height_px": self.display_height,
            }
        ]

    def _build_image_result(self, tool_use_id: str, screenshot_b64: str) -> dict:
        """构建包含截图的 tool_result 消息。"""
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        }
                    ],
                }
            ],
        }

    def run(self, instruction: str) -> str:
        """
        执行一个 Computer Use 任务。

        Args:
            instruction: 用户的自然语言指令

        Returns:
            Claude 的最终文本回复
        """
        self.messages = [{"role": "user", "content": instruction}]

        for step in range(self.max_steps):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=self._build_tools(),
                messages=self.messages,
                betas=[COMPUTER_USE_BETA],
            )

            # 将 assistant 回复加入消息历史
            self.messages.append({"role": "assistant", "content": response.content})

            # 如果没有 tool_use，任务完成
            if response.stop_reason == "end_turn":
                return self._extract_text(response.content)

            # 处理每个 tool_use block
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                action = block.input.get("action", "")
                print(f"  Step {step + 1}: {action}", end="")

                if action == "screenshot":
                    print(" → 截图")
                    screenshot_b64, actual_w, actual_h = take_screenshot(
                        self.display_width, self.display_height,
                    )
                    self._actual_width = actual_w
                    self._actual_height = actual_h
                    tool_results.append(
                        self._build_image_result(block.id, screenshot_b64)
                    )
                else:
                    # 坐标缩放：Claude 坐标基于 display_width/height，
                    # 实际操作需要映射到真实屏幕坐标
                    coord = block.input.get("coordinate")
                    start_coord = block.input.get("start_coordinate")
                    if self._actual_width and self._actual_height:
                        if coord:
                            coord = list(scale_coordinates(
                                coord[0], coord[1],
                                self.display_width, self.display_height,
                                self._actual_width, self._actual_height,
                            ))
                        if start_coord:
                            start_coord = list(scale_coordinates(
                                start_coord[0], start_coord[1],
                                self.display_width, self.display_height,
                                self._actual_width, self._actual_height,
                            ))

                    result = execute_action(
                        action=action,
                        coordinate=coord,
                        text=block.input.get("text"),
                        scroll_direction=block.input.get("scroll_direction"),
                        scroll_amount=block.input.get("scroll_amount"),
                        start_coordinate=start_coord,
                        duration=block.input.get("duration"),
                        dry_run=self.dry_run,
                    )
                    print(f" → {result}")

                    # 操作后截图，让 Claude 看到结果
                    screenshot_b64, _, _ = take_screenshot(
                        self.display_width, self.display_height,
                    )
                    tool_results.append(
                        self._build_image_result(block.id, screenshot_b64)
                    )

            # 将所有 tool_result 合并为一条 user 消息
            if tool_results:
                combined_content = []
                for tr in tool_results:
                    combined_content.extend(tr["content"])
                self.messages.append({"role": "user", "content": combined_content})

        return "[Computer Use] 达到最大步数限制，任务未完成"

    @staticmethod
    def _extract_text(content) -> str:
        """从 response.content 中提取文本。"""
        texts = []
        for block in content:
            if hasattr(block, "text"):
                texts.append(block.text)
        return "\n".join(texts)
