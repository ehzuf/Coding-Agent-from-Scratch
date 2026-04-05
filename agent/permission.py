"""
权限系统 —— 控制工具执行权限

对应 reference 中的 utils/permissions.ts

核心功能：
  - ask 模式：工具执行前询问用户确认（human-in-the-loop）
  - allow/deny 规则：支持规则配置，通配符匹配
  - fail-closed 设计：未明确允许的操作默认拒绝

权限模式：
  - allow: 自动允许执行
  - deny: 自动拒绝执行
  - ask: 询问用户确认

规则格式：
  - bash(git *)     允许执行 git 开头的命令
  - bash(rm *)      拒绝执行 rm 命令
  - read(*.py)      允许读取 Python 文件
  - write(/tmp/*)   允许写入 /tmp 目录
"""

import re
import fnmatch
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable


class PermissionMode(Enum):
    """权限模式"""
    ALLOW = "allow"  # 自动允许
    DENY = "deny"    # 自动拒绝
    ASK = "ask"      # 询问用户


class PermissionDecision(Enum):
    """权限决定"""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionRule:
    """权限规则"""

    tool_name: str           # 工具名称，如 "bash"
    pattern: str             # 匹配模式，如 "git *" 或 "*"
    mode: PermissionMode     # 权限模式

    def matches(self, tool_name: str, argument: str) -> bool:
        """
        检查规则是否匹配。

        Args:
            tool_name: 工具名称
            argument: 工具参数（如 bash 命令、文件路径）

        Returns:
            是否匹配
        """
        if tool_name != self.tool_name:
            return False

        # 使用 fnmatch 进行通配符匹配
        return fnmatch.fnmatch(argument, self.pattern)

    @classmethod
    def parse(cls, rule_str: str, mode: PermissionMode) -> "PermissionRule":
        """
        解析规则字符串。

        格式：tool_name(pattern)
        例如：bash(git *), read(*.py)

        Args:
            rule_str: 规则字符串
            mode: 权限模式

        Returns:
            PermissionRule 实例
        """
        # 解析 tool_name(pattern) 格式
        match = re.match(r"(\w+)\((.+)\)", rule_str)
        if match:
            tool_name = match.group(1)
            pattern = match.group(2)
            return cls(tool_name=tool_name, pattern=pattern, mode=mode)

        # 简单格式：只有工具名
        return cls(tool_name=rule_str, pattern="*", mode=mode)


@dataclass
class PermissionConfig:
    """权限配置"""

    # 默认模式（未匹配任何规则时）
    default_mode: PermissionMode = PermissionMode.ASK

    # 允许规则列表
    allow_rules: list[PermissionRule] = field(default_factory=list)

    # 拒绝规则列表
    deny_rules: list[PermissionRule] = field(default_factory=list)

    # ask 回调函数
    ask_callback: Callable[[str, str], bool] | None = None

    @classmethod
    def from_dict(cls, config: dict) -> "PermissionConfig":
        """从字典创建配置"""
        perm_config = cls()

        if "default_mode" in config:
            perm_config.default_mode = PermissionMode(config["default_mode"])

        if "allow" in config:
            for rule_str in config["allow"]:
                rule = PermissionRule.parse(rule_str, PermissionMode.ALLOW)
                perm_config.allow_rules.append(rule)

        if "deny" in config:
            for rule_str in config["deny"]:
                rule = PermissionRule.parse(rule_str, PermissionMode.DENY)
                perm_config.deny_rules.append(rule)

        return perm_config


class PermissionManager:
    """权限管理器"""

    def __init__(self, config: PermissionConfig | None = None):
        self.config = config or PermissionConfig()

    def check_permission(
        self,
        tool_name: str,
        argument: str,
    ) -> PermissionDecision:
        """
        检查工具执行权限。

        检查顺序（fail-closed 设计）：
          1. 检查 deny 规则 -> 如果匹配，拒绝
          2. 检查 allow 规则 -> 如果匹配，允许
          3. 使用默认模式

        Args:
            tool_name: 工具名称
            argument: 工具参数

        Returns:
            权限决定
        """
        # 1. 先检查 deny 规则（安全优先）
        for rule in self.config.deny_rules:
            if rule.matches(tool_name, argument):
                return PermissionDecision.DENY

        # 2. 再检查 allow 规则
        for rule in self.config.allow_rules:
            if rule.matches(tool_name, argument):
                return PermissionDecision.ALLOW

        # 3. 使用默认模式
        if self.config.default_mode == PermissionMode.ALLOW:
            return PermissionDecision.ALLOW
        elif self.config.default_mode == PermissionMode.DENY:
            return PermissionDecision.DENY
        else:
            return PermissionDecision.ASK

    def ask_user(
        self,
        tool_name: str,
        argument: str,
        input_func: Callable[[str], str] | None = None,
    ) -> bool:
        """
        询问用户是否允许执行。

        Args:
            tool_name: 工具名称
            argument: 工具参数
            input_func: 输入函数（用于测试），默认使用 builtins.input

        Returns:
            是否允许执行
        """
        # 如果有自定义回调，使用回调
        if self.config.ask_callback:
            return self.config.ask_callback(tool_name, argument)

        # 默认使用终端输入
        if input_func is None:
            input_func = input

        print(f"\n[权限请求] 工具: {tool_name}")
        print(f"  参数: {argument[:100]}{'...' if len(argument) > 100 else ''}")
        print("  允许执行? [y/n/a(yes to all)] ", end="", flush=True)

        try:
            response = input_func("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if response in ("y", "yes"):
            return True
        elif response == "a":
            # yes to all - 可以添加临时规则
            return True
        else:
            return False

    def should_execute(
        self,
        tool_name: str,
        argument: str,
        input_func: Callable[[str], str] | None = None,
    ) -> tuple[bool, str]:
        """
        检查并决定是否执行工具。

        Args:
            tool_name: 工具名称
            argument: 工具参数
            input_func: 输入函数

        Returns:
            (是否执行, 原因)
        """
        decision = self.check_permission(tool_name, argument)

        if decision == PermissionDecision.ALLOW:
            return True, "allowed by rule"
        elif decision == PermissionDecision.DENY:
            return False, "denied by rule"
        else:
            # ask 模式
            allowed = self.ask_user(tool_name, argument, input_func)
            return allowed, "user " + ("allowed" if allowed else "denied")


# 预设配置
def get_default_permission_config() -> PermissionConfig:
    """获取默认权限配置（安全模式）"""
    return PermissionConfig(
        default_mode=PermissionMode.ASK,
        allow_rules=[
            # 允许读取操作
            PermissionRule.parse("read(*)", PermissionMode.ALLOW),
            PermissionRule.parse("glob(*)", PermissionMode.ALLOW),
            PermissionRule.parse("grep(*)", PermissionMode.ALLOW),
            PermissionRule.parse("get_current_time(*)", PermissionMode.ALLOW),
        ],
        deny_rules=[
            # 拒绝危险命令
            PermissionRule.parse("bash(rm -rf /*)", PermissionMode.DENY),
            PermissionRule.parse("bash(rm -rf /)", PermissionMode.DENY),
            PermissionRule.parse("bash(mkfs*)", PermissionMode.DENY),
            PermissionRule.parse("bash(dd *)", PermissionMode.DENY),
        ],
    )


def get_permissive_permission_config() -> PermissionConfig:
    """获取宽松权限配置（自动允许所有）"""
    return PermissionConfig(
        default_mode=PermissionMode.ALLOW,
        deny_rules=[
            # 仍然拒绝最危险的命令
            PermissionRule.parse("bash(rm -rf /*)", PermissionMode.DENY),
        ],
    )


def get_strict_permission_config() -> PermissionConfig:
    """获取严格权限配置（自动拒绝所有未明确允许的）"""
    return PermissionConfig(
        default_mode=PermissionMode.DENY,
        allow_rules=[
            PermissionRule.parse("read(*)", PermissionMode.ALLOW),
            PermissionRule.parse("glob(*)", PermissionMode.ALLOW),
            PermissionRule.parse("grep(*)", PermissionMode.ALLOW),
            PermissionRule.parse("get_current_time(*)", PermissionMode.ALLOW),
        ],
    )
