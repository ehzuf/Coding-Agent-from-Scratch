# 从零实现 Coding Agent（二十八）：Agent 评测

Agent 改了一版 prompt，工具调用变快了还是变慢了？换了个模型，任务完成率是升了还是降了？

对于传统软件，有单元测试；对于 ML 模型，有 benchmark。但 Agent 是一个**非确定性系统**——同一个 prompt 跑两遍可能产生不同的工具调用序列和结果。这意味着：

1. **不能靠跑一次判断好坏**——需要批量跑、统计分布
2. **不能只看最终结果**——过程（token 消耗、工具使用效率）同样重要
3. **不能靠人盯**——需要自动化评分

这一篇拆解怎么系统地评测 Agent，从轨迹生成到自动评分到结果分析。我们会参考业界 SWE-bench 评测方法论与通用工程实践。

---

## 评测的三个维度

| 维度 | 衡量什么 | 典型指标 |
|------|----------|----------|
| **正确性** | 任务是否完成 | pass rate, 文件是否正确修改, 测试是否通过 |
| **效率** | 代价多大 | token 消耗, 工具调用次数, 完成时间, 费用 |
| **安全性** | 是否越权 | 沙箱违规次数, 敏感信息泄露, 未授权操作 |

一个"好"的 Agent 改进应该：正确性不降（最好升），效率提升（或至少不退化），安全性不降。

---

## 评测数据集设计

评测从一组**标准化的测试用例**开始：

```python
from dataclasses import dataclass, field


@dataclass
class EvalCase:
    """一条评测用例"""
    id: str                               # 唯一标识
    prompt: str                           # 给 Agent 的指令
    expected: dict = field(default_factory=dict)  # 期望结果
    tools_allowed: list[str] | None = None  # 限制可用工具（可选）
    max_turns: int = 20                   # 最大轮次
    timeout_s: int = 300                  # 超时
    tags: list[str] = field(default_factory=list)  # 分类标签


# 数据集格式（JSONL）
# {"id": "file-create-01", "prompt": "创建 hello.py，内容为 print('hello')", "expected": {"file_exists": "hello.py", "file_contains": "print('hello')"}}
# {"id": "bug-fix-01", "prompt": "修复 utils.py 第 15 行的 IndexError", "expected": {"test_passes": "pytest tests/test_utils.py"}}
# {"id": "refactor-01", "prompt": "把 config.py 中的全局变量重构为 dataclass", "expected": {"file_contains": "@dataclass", "no_global_vars": true}}
```

好的数据集特征：
- **覆盖多种任务类型**（创建、修改、删除、查询、多文件协作）
- **有明确的成功标准**（不是"写得好不好"，而是"文件是否存在"）
- **可重复**（固定的初始环境，如 git checkout 到特定 commit）

---

## 批量执行与 Checkpoint

核心挑战：**并行跑、容错、可恢复**。

### 执行器

```python
import json
import time
from pathlib import Path
from multiprocessing import Pool
from dataclasses import dataclass, field


@dataclass
class TrajectoryResult:
    """一次执行的完整轨迹"""
    case_id: str
    success: bool
    messages: list[dict]            # 完整消息历史
    tool_calls: list[dict]          # 工具调用记录
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None
    tool_stats: dict = field(default_factory=dict)  # 每个工具的使用统计


def run_single_case(case: EvalCase, agent_config: dict) -> TrajectoryResult:
    """执行单个评测用例"""
    start = time.time()
    try:
        agent = build_agent(**agent_config)
        if case.tools_allowed:
            agent.tools = [t for t in agent.tools if t.name in case.tools_allowed]

        response = agent.chat(case.prompt)

        return TrajectoryResult(
            case_id=case.id,
            success=True,
            messages=agent.messages,
            tool_calls=_extract_tool_calls(agent.messages),
            total_tokens=agent._total_input_tokens + agent._total_output_tokens,
            total_cost_usd=_estimate_cost(agent),
            duration_s=time.time() - start,
            tool_stats=_count_tool_usage(agent.messages),
        )
    except Exception as e:
        return TrajectoryResult(
            case_id=case.id,
            success=False,
            messages=[],
            tool_calls=[],
            duration_s=time.time() - start,
            error=str(e),
        )
```

> 💡 教学简化：`build_agent`、`_extract_tool_calls`、`_count_tool_usage`、`_estimate_cost` 等辅助函数实现较为直接（构造 Agent、遍历消息提取 `tool_use` block、按模型计价），此处省略，读者可基于前面教程构建的 Agent 工厂函数自行补全。

### 批量并行 + Checkpoint

批量执行器的核心设计：

```python
class EvalRunner:
    """批量评测执行器"""

    def __init__(self, dataset_path: str, output_dir: str, workers: int = 4):
        self.cases = self._load_dataset(dataset_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.output_dir / "checkpoint.json"
        self.workers = workers

    def run(self, resume: bool = False) -> list[TrajectoryResult]:
        """执行全部用例"""
        completed_ids = set()
        if resume:
            completed_ids = self._load_checkpoint()
            print(f"恢复执行，跳过已完成的 {len(completed_ids)} 条")

        pending = [c for c in self.cases if c.id not in completed_ids]
        results = []

        # 分批并行执行
        batch_size = self.workers * 2
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            with Pool(self.workers) as pool:
                batch_results = pool.map(_worker, batch)

            for result in batch_results:
                results.append(result)
                self._save_trajectory(result)
                completed_ids.add(result.case_id)

            # 每批结束后保存 checkpoint
            self._save_checkpoint(completed_ids)
            print(f"进度: {len(completed_ids)}/{len(self.cases)}")

        return results

    def _save_trajectory(self, result: TrajectoryResult) -> None:
        """保存单条轨迹到 JSONL"""
        path = self.output_dir / "trajectories.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result.__dict__, ensure_ascii=False) + "\n")

    def _save_checkpoint(self, completed_ids: set) -> None:
        """保存进度（中断后可恢复）"""
        data = {"completed": list(completed_ids), "total": len(self.cases)}
        with open(self.checkpoint_file, "w") as f:
            json.dump(data, f)

    def _load_checkpoint(self) -> set:
        if self.checkpoint_file.exists():
            data = json.loads(self.checkpoint_file.read_text())
            return set(data.get("completed", []))
        return set()

    def _load_dataset(self, path: str) -> list[EvalCase]:
        cases = []
        with open(path) as f:
            for line in f:
                cases.append(EvalCase(**json.loads(line)))
        return cases
```

---

## 自动评分：规则 + LLM Judge

有了轨迹，下一步是自动判断每条轨迹是否"正确"。

### 基于规则的评分

最可靠，适合有明确成功标准的场景：

```python
import subprocess


def score_by_rules(result: TrajectoryResult, expected: dict) -> float:
    """规则评分，返回 0.0 ~ 1.0"""
    checks = []

    # 文件存在性检查
    if "file_exists" in expected:
        checks.append(Path(expected["file_exists"]).exists())

    # 文件内容检查
    if "file_contains" in expected:
        path = expected.get("file_exists", expected.get("file_path", ""))
        if Path(path).exists():
            content = Path(path).read_text()
            checks.append(expected["file_contains"] in content)
        else:
            checks.append(False)

    # 测试通过检查
    if "test_passes" in expected:
        proc = subprocess.run(
            expected["test_passes"].split(),
            capture_output=True, timeout=60,
        )
        checks.append(proc.returncode == 0)

    # 无错误检查
    if result.error:
        checks.append(False)

    return sum(checks) / max(len(checks), 1)
```

### 基于 LLM 的评分

适合主观性较强的场景（"代码质量"、"方案合理性"）：

> 注意这里没用 `case.expected`——LLM judge 的典型用途是处理写不出规则的主观题（代码质量、方案合理性），这类场景本来就没有标准答案。有标准答案的检查走 `score_by_rules` 更可靠、更便宜。

```python
JUDGE_PROMPT = """你是一个代码评审专家。请评估以下 Agent 的执行结果。

## 任务
{prompt}

## Agent 的最终输出
{agent_output}

## 评分标准
- 5 分：完美完成，代码质量高
- 4 分：基本完成，有小瑕疵
- 3 分：部分完成，有明显问题
- 2 分：方向正确但未完成
- 1 分：完全错误或未执行

请只输出一个数字（1-5）："""


def score_by_llm(
    result: TrajectoryResult,
    case: EvalCase,
    judge_llm,
) -> float:
    """LLM 评分，返回 0.0 ~ 1.0"""
    agent_output = _extract_final_text(result.messages)
    prompt = JUDGE_PROMPT.format(prompt=case.prompt, agent_output=agent_output)

    response = judge_llm.chat(
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        score = int(response.text.strip())
        return max(0, min(score, 5)) / 5.0
    except ValueError:
        return 0.0
```

### 复合评分

```python
def composite_score(
    correctness: float,
    tokens: int,
    tool_calls: int,
    token_budget: int = 50000,
    call_budget: int = 20,
) -> float:
    """复合评分 = 正确性 × 效率系数"""
    # 效率惩罚：超出预算的部分按比例扣分
    token_penalty = min(1.0, token_budget / max(tokens, 1))
    call_penalty = min(1.0, call_budget / max(tool_calls, 1))
    efficiency = (token_penalty + call_penalty) / 2

    return correctness * (0.7 + 0.3 * efficiency)
```

---

## 结果分析与报告

```python
def generate_report(results: list[TrajectoryResult], scores: list[float]) -> str:
    """生成评测报告"""
    total = len(results)
    passed = sum(1 for s in scores if s >= 0.8)
    failed = sum(1 for r in results if r.error)

    avg_tokens = sum(r.total_tokens for r in results) / max(total, 1)
    avg_tools = sum(len(r.tool_calls) for r in results) / max(total, 1)
    avg_time = sum(r.duration_s for r in results) / max(total, 1)
    total_cost = sum(r.total_cost_usd for r in results)

    report = f"""
# 评测报告

## 总体结果
- 用例总数: {total}
- 通过率: {passed}/{total} ({passed/total*100:.1f}%)
- 执行失败: {failed}
- 平均得分: {sum(scores)/len(scores):.2f}

## 效率指标
- 平均 Token: {avg_tokens:,.0f}
- 平均工具调用: {avg_tools:.1f} 次
- 平均耗时: {avg_time:.1f}s
- 总花费: ${total_cost:.4f}

## 失败用例
"""
    for r, s in zip(results, scores):
        if s < 0.8:
            report += f"- [{r.case_id}] score={s:.2f}"
            if r.error:
                report += f" error={r.error[:80]}"
            report += "\n"

    return report
```

---

## 评测工作流

完整的评测流程：

```
  数据集        环境初始化       批量执行         自动评分         报告分析
   │              │              │              │              │
   ▼              ▼              ▼              ▼              ▼
eval_cases    git reset /     EvalRunner    规则 + LLM      pass rate
  .jsonl       Docker          .run()         judge        效率指标 / 失败分析
```

每次修改 Agent 后重跑评测，对比前后指标：

```
           v1.0    v1.1    Δ
pass rate  72%     78%     +6%
avg tokens 12,400  10,800  -13%
avg time   34s     28s     -18%
cost/case  $0.04   $0.03   -25%
```

---

## 与业界方案的对比

| 维度 | SWE-bench | 我们的实现 |
|------|-----------|------------|
| 数据集 | 2294 条真实 GitHub issues | 自定义 JSONL |
| 评判方式 | `pytest` 测试通过 | 规则 + LLM judge |
| 环境隔离 | Docker per case | 建议 Docker / git reset |
| 并行度 | 可配置 | multiprocessing Pool |
| 容错 | 无 checkpoint | checkpoint 支持 |
| 用途 | 发论文 / 排行榜 | 迭代开发的回归测试 |

---

## 小结

Agent 评测的核心思路：

1. **数据集驱动** — 把"Agent 好不好"转化为"这 100 个用例通过了几个"
2. **批量 + 并行 + 容错** — Agent 执行慢且不稳定，必须工程化处理
3. **多维评分** — 正确性是底线，效率是加分，安全是红线
4. **自动化** — 人工判断不可扩展，规则 + LLM judge 组合覆盖大多数场景
5. **轨迹是资产** — 评测产生的轨迹可用于调试、回归对比、案例教学

一句话：**没有评测的 Agent 开发是盲人摸象。改一行 prompt 就重跑一次评测，数字说话。**
