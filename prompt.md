好，这次把整条链路从 HTTP 请求 → token 序列完整追一遍。先看文件规模和实际客户端代码：

examples 里只有 minicpmo/joyvl 的。找 qwen3_omni duplex 的 HTTP 入口和 `duplex_prefix_token_ids` 的实际消费方：

找到关键消费方了。看客户端示例（`examples/online_serving/qwen3_omni/`、`vllm_omni/examples/qwen3_omni_duplex/uds.py`）和 runtime.py：

客户端这边清楚了：`uds.py` 发的是 OpenAI Realtime 风格事件（`session.update` + `input_audio_buffer.append`），完全不碰 token。现在追服务端怎么把 `instructions`/`context_text`/`task_type` 变成最终 token 序列——读 runtime.py 和 format_policy.py：

核心拼装函数找到了（`runtime.py` 的 `plan_append`）。再确认两个细节：worker 怎么回放上一 chunk 的尾巴、音频特征怎么注入：

链路全部查清了。**结论先说：这个 prompt 不是一段一次性拼好的文本，而是“前缀拼一次 + 每个 240ms chunk 增量拼一小块、靠 KV cache 会话续起来”的流式结构。你草图中间的 `<|im_start|>user`/`<|im_start|>assistant` 两个轮次在整个代码库中根本不存在——那是 ChatML 的思维惯性，真实格式里没有。**

## 拼装的完整链路（五层）

**① 客户端**（`vllm_omni/examples/qwen3_omni_duplex/uds.py:195-224`）——发的是 OpenAI Realtime 风格事件，完全不碰 token：

```python
session.update: {
  instructions: "You are a full-duplex omni-modal assistant...",  # ← 你草图第一块的正文
  extra_body: {
    qwen3_omni_native_duplex: True,
    context_text: instruction,          # ← "Please listen to the audio and transcribe..."
    task_type: "audio2text",            # ← 由 modality 动态拼成 f"{modality}2text"
  }
}
input_audio_buffer.append × N   # base64 PCM16，240ms/3840样本一块
input_audio_buffer.commit
```

**② serving_adapter.py:215-267** —— 把文本 encode 成 token 存进 runtime_config：

```python
prefix_tokens = [<|im_start|>] + tokenizer.encode(instructions) + [<|im_end|>, <|task_audio2text|>]   # :234-244
duplex_context_text_token_ids = tokenizer.encode(context_text)   # 整条指令一次 encode :257-266
```

**③ runtime.py `plan_append`（:160-350）** —— 每来一个 240ms chunk 调一次（`seq` 从 1 计），拼本次增量。关键逻辑（:270-310）：

- `seq==1`：`exact_ids = prefix_tokens`（前缀只在这一块出现，唯一带 ChatML 的地方）
- 追加 `<|chunk_bos|>`（它同时是 scheduler 记账 token，`:252-256` 先用 `[<|chunk_bos|>]×budget` 占位再替换成精确序列）
- 追加 `<|audio_start|> <|audio|>×3 <|audio_end|>`（纯占位）
- **context_text 按块切片流式注入**：`chunk_index = seq-1`，取 `all_context_ids[chunk_index*500 : (chunk_index+1)*500]`（:194-197），非空才包上 `<|context_text_start|>...<|context_text_end|>`。指令短的话实际上 chunk 1 就注完了——不是“指令先到、音频后到”，而是和音频同 chunk 交织
- 追加 `<|agent_text_start|>`（解码锚点，到此为止）

**④ worker.py（:237-343）** —— 补上 runtime 层没有的东西，才是真正喂进模型的序列：

- `seq>1` 时开头回放上一 chunk 的收尾：`replayed_chunk_token_ids`，或兼容模式下 `[上个chunk产的agent token] + <|agent_text_end|> <|action_start|> <|action_end|> <|chunk_eos|>`（:240-266）——这就是"每 chunk 的结尾”从哪来的
- `<|audio|>` 占位处**真的过 audio tower**：有音频 encode 真特征，非语音 chunk encode 静音零波形，恒 3 行 embedding（:284-318，行数不匹配直接报错）

**⑤ 输出** —— 模型在 `<|agent_text_start|>` 后产 **1 个 token**：transcript 文本 token，或 `<|listen|>`（沉默）。`decide_output`（runtime.py:352-496）按这个判定 listen/speak。

## 模型逻辑上看到的完整序列（修正版草图）

```text
<|im_start|>系统提示<|im_end|><|task_audio2text|>          ← 仅一次
<|chunk_bos|> <|audio_start|>[A₁]<|audio_end|>
  [<|context_text_start|>指令token片1<|context_text_end|>]
  <|agent_text_start|> g₁ <|agent_text_end|> <|action_start|> <|action_end|> <|chunk_eos|>
<|chunk_bos|> <|audio_start|>[A₂]<|audio_end|>
  [<|context_text_start|>指令token片2<|context_text_end|>]
  <|agent_text_start|> g₂ <|agent_text_end|> <|action_start|> <|action_end|> <|chunk_eos|>
...（几百块）
```

- `[Aᵢ]` = 第 i 块的 3 行音频 embedding；`gᵢ` = 第 i 块模型产的 1 个 agent token（transcript 的一个 token 或 `<|listen|>`）
- transcript = 所有 `gᵢ` 里的文本 token 依序拼接，说的结束信号是 `<|text_break|>`，不是任何 `<|im_end|>`

## 对照你的草图，三处修正

| 你的草图 | 实际情况 |
|---|---|
| `<|im_start|>` 裸前缀 + system 文本 + `<|im_end|>` + `<|task_audio2text|>` | ✅ 对，这就是 `prefix_tokens`，且只出现一次 |
| `<|im_start|>user` 包住音频和 context_text，`<|im_end|>` 收尾 | ❌ 不存在 user 轮。音频走 `<|audio_start|><|audio|>×3`，context_text 走 `<|context_text_start|>...`，都在 `<|chunk_bos|>` 块内 |
| `<|im_start|>assistant` 开放结尾 | ❌ 不存在 assistant 轮。解码锚点是每块的 `<|agent_text_start|>`，每块产 1 token；块间由 `<|agent_text_end|><|action_start|><|action_end|><|chunk_eos|>` 收尾并回放到下一块 |

至于“为啥最后没有 im_end”——在这个格式里问法要反过来：`<|im_end|>` 全序列只在**前缀**出现一次（给 system 收尾），它本来就不承担“结束一轮对话”的职责；对话的节奏信号全部由 agent token 自己承载（`<|listen|>`/`<|speak|>`/`<|text_break|>`）。这也是 duplex 和普通 ChatML 模型的本质区别：没有“轮”的概念，只有每 240ms 一个决策点的状态机。

<options>
    <option>把这个机制整理成文档归档到 asreval 目录</option>
    <option>对照这个格式检查我们 asreval 发的请求</option>
    <option>清楚了，不用继续</option>
</options>
