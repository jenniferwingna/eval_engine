好问题——长会话下这个直接决定转写质量。文档里有一句话提到 eviction，我去查实现细节：

rg 输出被截断得厉害，直接读 `scheduler_offsets.py`——名字就是为这事准备的：

这只是偏移换算。驱逐逻辑应该在 fullduplex 通用层，往上层搜：

只在文档和别的模型（joyvl）里出现。看 fullduplex 的 DESIGN.md 和 engine/core 层：

`completed_append_limit: 256` 浮出水面了。看这段上下文：

`completed_appends` 的 256 上限只是记账元数据的 LRU，不碰模型序列。真正的序列控制要看 `persistent_kv_per_session` 的实现：

实现层面是空的——这三个字段只存在于 draft yaml，没有任何代码引用。查引擎层怎么管跨 append 的 KV：

experimental/fullduplex 这套是骨架。实际注册的 pipeline 在 `model_executor/models/qwen3_omni/duplex_pipeline`：

这些是 audio tower 内部分窗，不是序列驱逐。看 `qwen3_omni_duplex.py` 模型类和 `intermediate.py` 怎么维护持久序列：

模型类只是 re-export。历史 KV 一定在引擎侧。先读你们自己写的调试记录——最接近实际运行情况：

评测文档没有架构信息。回到 vLLM 核心找 duplex 会话的 KV 管理：

vllm 核心无 duplex 代码。找 `replayed_chunk_token_ids` 的生产方——谁维护跨 append 的历史：

核心调度器找到了。看它的会话序列管理和长度限制：

`OmniARScheduler` 继承 vLLM 原生调度器，duplex 请求是持续增长的同一个 Request。读 duplex state 处理段：

`_update_request_as_session` 是关键——查它的实现，确认超长时的行为：

逻辑清楚了。最后确认 vLLM 原生 session 更新对超长的处理：

vllm 核心是外部依赖，找它的安装位置：

当前环境的 vllm 0.11 太老，fork 的核心不在本机。看测试文件里有没有超长场景的定义：

查证完毕，证据链完整了。先存个档，然后给你完整结论：

查证结论：**不压缩、也不丢——历史全量累积在 KV cache 里。文档里写的“丢最老 chunk”策略是个没落地的 draft 承诺。**

## 实际机制：KV 全量累积

duplex 会话在引擎里是 **vLLM streaming session**：同一个 Request 从会话开始活到结束，每次 append 通过 `StreamingUpdate` 把增量接到已有序列上（`omni_ar_scheduler.py:1018` `_update_request_as_session`：`update.prompt_token_ids = [回放尾巴] + [新 chunk 增量]`，KV cache 继续复用）。所以序列单调增长：

```text
prefix + chunk₁ + chunk₂ + ... + chunkₙ + 新 chunk     ← 全在 KV 里，一个都不丢
```

每个 chunk 的 12 个 token（结构 9 + 音频占位 3）+ agent 产物永久驻留。

## 文档声称的 eviction：未实现

`CHUNK_LAYOUT.md:11-14` 写着 *"evict the oldest complete chunks until the new chunk fits. The immutable prefix must always be retained"*，但我把它和实现逐层对过，**没有落地**：

| 证据 | 结果 |
|---|---|
| 配置文件名 | 就叫 `runtime.draft.yaml`——草稿 |
| `persistent_kv_per_session` / `prefill_prefix_once` / `decode_anchor_token` 三个 worker 字段 | 全代码库**零引用**（只有 yaml 里有） |
| `runtime.py` / `worker.py` / `omni_ar_scheduler.py` / `format_policy.py` | 无任何 chunk 驱逐/裁剪/窗口代码 |
| `duplex_session.py:92` 的 `completed_append_limit=256` | 只是对完成 append 的**记账结果**（stage_results 元数据）做 LRU 清理，不碰模型序列 |
| 测试 | 无超长/驱逐 case |

唯一诚实的不确定性：omni 层是个插件包，fork 的 vllm 核心源码本机没有，不排除核心里有部分实现——但 omni 层的 token 预算会计和偏移换算（`scheduler_offsets.py`）都完全没有为驱逐做配套，真有驱逐早就对不齐了。

## 那超长了会怎样

没有优雅降级，两条路都是 vLLM 原生行为：

- **KV cache 空间不够** → preempt：整个 session 换出、腾出空间后**全部重算**（不是丢最老的）
- **逼近 `max_model_len`** → 硬失败，会话报错

## 给你们 asreval 场景的容量账

纯音频会话 ≈ **50 token/s**（12 token / 240ms chunk）：

- 32k 上下文 ≈ **11 分钟**音频到顶
- 有视频再叠 ~25 token/s（每 4 chunk 一帧 × 100 token），掉到 ~7 分钟
- context_text 溢出不丢（`runtime.py:194-197` 切片续流是实现了的），但一般指令 chunk 1 就注完了，可忽略

好消息是转写任务天然抗“遗忘”：模型**边听边写**（每 chunk 产 1 个 token），transcript 在音频进来时就产出了，历史音频的 KV 属于“用完即弃”性质——所以就算哪天真实现了 eviction，对转写几乎无损。真正的风险只有一个：**长音频跑到后半段撞 max_model_len 直接崩**。所以 30 分钟以上的音频样本必须切段评测，别指望服务端自己处理。

这个结论我已经存进 memory 了（`vllm-mola-duplex-seqlen-no-eviction`），下次做长音频评测时会直接提醒。

<options>
    <option>帮我把 asreval 数据集按 11 分钟阈值过一遍，标出会超长的样本</option>
    <option>试着把 evict oldest chunks 策略实现出来</option>
    <option>清楚了，不用继续</option>
</options>
