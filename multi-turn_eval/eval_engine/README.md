# eval_engine

把 `dataset/multiturn_eval_set_approved.json` 的用例，真跑一遍：LLM 用户模拟器 → 真实
cockpit_harness（走它自己的原生 WebSocket 协议，不需要适配层）→ 判分引擎 → 结论。

这是"打通链条，看看是不是真的能评测"的落地代码，不是设计文档。

## 组成

| 文件 | 作用 |
|---|---|
| `case_schema.py` | 把数据集里 3 种不同形状的「轮次脚本」统一成可驱动的用户轮次列表 |
| `simulator.py` | LLM 用户模拟器：只给目标+条件，不给台词；只看 agent 说过的话，不看 tool_calls |
| `driver.py` | 连 harness 的 WebSocket，跑完整条多轮用例，落盘 transcript |
| `judge.py` | 判分：能代码判的走代码（④结构化信息断言/①终态/③禁止动作里能解析出 api() 的部分），
其余走⑤LLM 二元判分（这是唯一对全部 17 个维度都通用的判分路径） |
| `canon.py` | 参数归一化表（position/switch/function 别名），从 harness 自己的 tool_schema.py 抄一份精简版，
故意不 import 原文件——这个目录要能整个打包发出去，不依赖 cockpit_harness 仓库 |
| `llm_client.py` | 极简 OpenAI 兼容 chat completions 客户端，模拟器和判分员共用 |

## 跑起来

1. 启动 harness（任选一种）：

   ```bash
   cd cockpit_harness
   sh start_harness_mock.sh      # 便宜/快，但走规则兜底 NLU，测不出真实对话能力，只用来查线路通不通
   sh start_harness.sh           # 真实 Qwen Omni 模型，会真花钱
   ```

2. 跑一条用例：

   ```bash
   cd multi-turn_eval/eval_engine
   python3 driver.py --case-id CLARIFY-001
   ```

   常用参数：
   - `--dimension CLARIFY --limit 5`　按维度跑一批
   - `--literal`　跳过 LLM 模拟器，直接把「目标」文本当台词发出去（便宜，纯查线路用，不代表真实用户）
   - `--no-judge`　只跑 SUT 不判分（更便宜的线路检查）
   - `--provider mock`　强制走 mock（不建议用来出结论，只用来查协议）

3. 结果落在 `../eval_runs/<timestamp>/cases.jsonl`（每条用例的完整 transcript + 判分明细）和
   `summary.json`（pass/fail/needs_manual 汇总）。

## 已经验证过什么（2026-08-20，对 CLARIFY-001 跑通的真实结果）

- 线路完全打通：LLM 模拟器生成台词 → 真实 `qwen3.5-omni-flash-realtime` 走 harness 原生协议
  应答 → tool_calls/assistant_reply/state 三样都拿到了 → 判分引擎给出结论。
- 判分引擎的代码检查（④结构化信息断言）和 LLM 二元判分（⑤）两条独立路径**结论一致**，
  互相印证了判分逻辑没写错。
- 这一跑还真的抓到一个 agent 的真实缺陷：CLARIFY-001 要求「缺 position 时必须先追问」，
  实测 agent 直接猜了「后排左右都开 30%」，没问就执行了——不是我们的脚手架有问题，
  是这个 harness 当前配置下这个能力还没做对。

## 还没自动化 / 需要注意的坑（如实说，不夸大覆盖面）

1. **`③禁止动作` 里的语义类条目判不了**：像"根据任一儿童的存在猜测左右车窗"这种，
   代码只能挑出能解析成 `api(args)` 形式的条目去核查（比如"检查轮次执行window_opener"
   能配合结构化断言判断该轮是否调用了目标 api）；纯语义的会显式标成 `needs_manual`，
   不会悄悄判过。
2. **`①④` 结构化/终态断言只覆盖了能解析出明确 api+参数 的用例形状**（CLARIFY/INFERMEM/
   INSTKEEP/VERSION 这类）；FALSEMEM/SELFCON 这种"不应该发生什么"的用例走的是
   "调用次数为0"专门分支，不是通用逻辑。⑤LLM二元判分是唯一对全部 17 个维度都
   通用的自动化路径。
3. **`generic_tools` 状态只是"最后一次调用的原始参数"，不是真实语义状态**：harness 的
   `CockpitState` 只对约 12 类工具（空调/车窗/座椅加热/氛围灯/门锁/后视镜等）做了真实的
   状态推演，其余 500+ manifest 工具调用只会把最后一次的 `{args}` 存进
   `state.generic_tools[tool_name]`，不会真的模拟状态演变。所以判分要看 `tool_calls`
   这条流水账，不能只看 `state.snapshot()`。
4. **发现一处 harness 侧的潜在 bug，没有动它**：`cockpit_harness/harness/domains/
   manifest_tools.py` 的 `POSITION_ALIASES` 没有把 `backRight`/`backLeft`（真实 manifest
   里 `window_opener` 的合法枚举值）映射到 `rear_right`/`rear_left`——如果模型真的按
   manifest 词表输出 `position=backRight`，状态模拟会静默 fallback 成 `driver`。这次跑的
   case 模型用的是 `rear_right`（能正确映射），没触发，但下次遇到别的模型习惯用
   `backRight/backLeft` 时会踩到。这是 cockpit_harness 自己的代码，按你的要求没有动它，
   仅作记录。
5. **mock 模式测不出这份评测集想测的任何东西**：实测 mock 走的是规则兜底 NLU，完全没有
   "追问"能力，直接猜了个位置就执行——README 里早就写了"mock 只用来隔离本地问题，
   正式评测不要用"，这次是拿真实数据实测验证了这句话，不是空口白话。
6. **只验证了 1 条 case、6 个 P0 维度里只深入到 CLARIFY**；FALSEMEM/INFERMEM/INSTKEEP/
   SELFCON/VERSION 的 `case_schema.py` 解析逻辑是照着各自的字段形状写的，但还没有真的
   拿真实 case 跑过，建议下一步按 `MULTITURN_EVAL_BUILD_PLAN.md` Step 7 的顺序
   （CLARIFY→FALSEMEM/INFERMEM/INSTKEEP/VERSION→SELFCON）逐维度验证。
