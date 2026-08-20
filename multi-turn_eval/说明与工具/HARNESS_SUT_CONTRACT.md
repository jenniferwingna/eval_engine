# Harness ↔ 多轮评测集 对接契约

> 读者：负责给座舱 agent 接入这份多轮评测的 Harness 工程（人或 agent 均可直接照做）。
> 目标：Harness 工程只需要做一件事——**让评测侧能够逐轮把用户话术发给 agent，并原样拿到 agent 这一轮实际做了什么**。用户模拟、判分、统计汇总全部在评测侧完成，不需要 Harness 工程关心。

---

## 0. 先看这个：第一期只接 300 条，不是全部 850 条

评测集共 850 条、17 个维度，但发布门槛（P0）只有 6 个维度、300 条，且这 6 个维度的对话结构最简单——**纯"一句用户话术 → 一句 agent 回复（+ 可能的工具调用）"来回，不需要故障注入，不需要外部事件注入**。

第一期只接第 1 节的基础契约，就能跑完这 300 条。第 5 节的两个扩展字段是给 P1 阶段的 `TOOLFAIL`（工具故障恢复）、`STATECONS`（状态一致性）用的，届时再看，现在不用管。

| 阶段 | 覆盖维度 | 需要的能力 |
|---|---|---|
| **第一期（现在做）** | CLARIFY / FALSEMEM / INFERMEM / INSTKEEP / SELFCON / VERSION（300条） | 第1节基础契约 |
| 第二期（之后再说） | 其余 11 个维度 | 第1节 + 第5节故障/事件注入 |

---

## 1. 基础契约：一次请求 = 一轮用户话术，一次响应 = 这一轮 agent 实际做的事

评测侧维护对话历史和场景剧本，每一轮把用户这句话发过来；Harness 工程要做的，是让 agent 处理这句话，然后把这一轮 agent **实际说了什么、调用了什么工具、车辆状态变成了什么**，原样吐回来。不要在返回前做任何"总结""挑重点"式的加工——评测要看的就是原始行为。

### 请求（评测侧 → Harness）

```json
{
  "session_id": "CLARIFY-001__run3",
  "turn_index": 1,
  "user_utterance": "后排两个孩子都说有点闷，开条缝透透气吧",
  "initial_state": {
    "四门车窗": "均关闭",
    "后排左侧有儿童": true,
    "后排右侧有儿童": true
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `session_id` | ✅ | 一次完整对话的标识。**同一个 session_id 内，Harness 必须维持上下文**（agent 要记得住前面几轮说过什么）。评测侧每重跑一次同一条用例，会换一个新的 `session_id`，代表全新会话，不带上一次重跑的任何记忆 |
| `turn_index` | ✅ | 本次是这个 session 里的第几轮用户话术，从 1 开始递增 |
| `user_utterance` | ✅ | 这一轮用户说的话（已经是模拟用户根据用例目标现场生成的自然语句，不是占位符） |
| `initial_state` | 仅 `turn_index=1` 时提供 | 场景初始车辆/环境状态，Harness 要在处理这一轮之前把它设置好，作为 agent 决策的起点 |

### 响应（Harness → 评测侧）

```json
{
  "session_id": "CLARIFY-001__run3",
  "turn_index": 1,
  "assistant_reply": "好的，是要开左后还是右后车窗呢？",
  "tool_calls": [],
  "state": {
    "四门车窗": "均关闭",
    "后排左侧有儿童": true,
    "后排右侧有儿童": true
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `session_id` / `turn_index` | ✅ | 原样回显 |
| `assistant_reply` | ✅ | agent 这一轮回复用户的原文，没有回复就是空字符串，不要省略字段 |
| `tool_calls` | ✅（可为空数组） | agent 这一轮**实际发起**的每一次工具调用，格式见下。没调用就是 `[]` |
| `state` | ✅ | 这一轮处理完之后，车辆/环境的**完整当前快照**（不是增量 patch）。即使这一轮什么都没变，也要把当前完整状态吐回来 |

`tool_calls` 里每一项：

```json
{ "name": "window_opener", "args": {"function": "set", "position": "backRight", "value": "little"} }
```

`name` 必须是 `car_tools_list.md` 里真实存在的工具名；`args` 是这次调用实际传的参数，原样给，不要过滤掉评测用不到的参数。

### 一次完整对话长什么样

用 `CLARIFY-001` 举例，用例剧本是 4 个"轮次"（R1用户/R2助手/R3用户/R4助手），落到 API 上是 **2 次请求-响应往返**（用例里的"助手轮"不是评测侧发起的请求，是 Harness 响应里的 `assistant_reply` + `tool_calls`）：

```
往返1
→ 请求  turn_index=1  user_utterance="后排两个孩子都说有点闷，开条缝透透气吧"  initial_state={...}
← 响应  assistant_reply="好的，是要开左后还是右后车窗呢？"  tool_calls=[]  state={...不变...}

往返2
→ 请求  turn_index=2  user_utterance="右后的"
← 响应  assistant_reply="好的，已经把右后车窗开了条缝"
        tool_calls=[{"name":"window_opener","args":{"function":"set","position":"backRight","value":"little"}}]
        state={... 右后车窗开度变化 ...}
```

评测侧会用这两次响应，对照用例的"检查轮次=2"（也就是往返1的响应）判断：agent 是不是只问了车窗位置、没有提前执行。

---

## 2. Session 生命周期规则

- 一个 `session_id` 对应一次完整的用例执行（可能是这条用例的第 N 次重跑）。**同一个 `session_id` 内所有轮次必须共享同一个对话上下文**——这是多轮评测的核心，agent 记不记得住前面轮次的信息就是在测这个。
- 不同 `session_id` 之间必须完全隔离，不能互相污染上下文或车辆状态。评测侧同一条用例会按 `重跑n` 跑很多次，每次都是全新 `session_id`、全新状态，不是接着上一次继续跑。
- `turn_index=1` 的请求必须先用 `initial_state` 初始化环境，再处理 `user_utterance`。

---

## 3. 这不是 Harness 工程的工作（评测侧全包）

- 用户话术怎么生成（读用例的"目标+条件"、现场生成自然语句） —— 评测侧的用户模拟器负责，Harness 工程收到的 `user_utterance` 已经是成品
- 这一轮该不该算通过、用哪种判分手段判 —— 评测侧的判分引擎负责
- 通过率、分布形状、维度/分组汇总 —— 评测侧的统计层负责
- 用例数据本身（场景、期望终态、禁止动作……）—— Harness 工程不需要读取或解析这份用例 JSON，只需要按上面的请求/响应契约把 agent 接起来

Harness 工程唯一要做的，就是让第 1 节的请求/响应契约跑通。

---

## 4. 联调前自查

- [ ] 同一个 `session_id` 连续发 3-4 轮，agent 能不能记住第 1 轮提到的信息（比如车窗位置一旦在某轮确定，后面轮次不会又重新问一遍）
- [ ] `tool_calls` 里的 `name` 和 `args` 是 agent **实际**调用的原始参数，不是 Harness 自己转写、简化过的
- [ ] `state` 每次都是完整快照，字段命名和取值前后一致（不要这一轮叫 `switch`，下一轮又叫 `power`）
- [ ] agent 没有调用任何工具、没有任何状态变化的轮次，`tool_calls` 老实返回 `[]`、`state` 返回和上一轮相同的快照，不要省略这两个字段
- [ ] 换一个新 `session_id` 之后，car 状态和对话记忆确实是全新的，没有从上一个 session 漏过来的残留

---

## 5. 第二期扩展：故障注入 / 外部事件注入（P1 阶段再看，现在跳过）

`TOOLFAIL`（工具故障恢复）需要评测侧能强制让某次工具调用返回一个指定的错误（比如超时），观察 agent 怎么恢复；`STATECONS`（状态一致性）需要能在用户没说话的情况下，直接把一次外部状态变化"推"给 agent（比如车窗被手动按开）。这两个都需要在第 1 节的请求里加可选字段：

```json
{
  "session_id": "...",
  "turn_index": 3,
  "fault_injection": {"target_tool": "window_switch", "fault_type": "TIMEOUT_UNKNOWN"},
  "injected_event": {"description": "副驾车窗被手动按开", "state_patch": {"副驾车窗": "开启"}}
}
```

以及在响应的 `tool_calls` 每一项里加 `status`（`success` / `error`）和 `error` 字段，用来观察 agent 面对故障时的重试/放弃/查态行为。这部分只影响 P1 的两个维度，第一期完全用不到，先不用为它改动接口。

---

## 6. 联调方式

评测侧会先按本契约写一个假的 Harness（返回预设的 `assistant_reply`/`tool_calls`/`state`），把用户模拟器、判分引擎、统计层在假数据上跑通。Harness 工程按本契约接完之后，双方先拿 1-2 条用例（建议从 `CLARIFY-001` 开始）联调一次，确认字段对得上、多轮上下文保持得住，再进入 300 条 P0 全量跑批。
