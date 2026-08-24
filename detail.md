`<|audio_end|>` 在 [qwen_engine.py:185](src/nonrag_streaming/qwen_engine.py:185),`StreamingSession.commit()` 里:

```python
self._flush_tail()                              # 先补齐不满 1 秒的音频尾巴
tail = "<|audio_end|>\n"
if static_context.strip():
    tail += static_context.strip() + "\n"       # 确定性上下文塞在这
tail += "<|im_end|>\n<|im_start|>assistant\n"
output = self._feed_text(tail)                  # 一次性喂进常驻 KV
```

它是 **Qwen3-Omni 的特殊 token**,作用是告诉模型"音频段到此结束,后面开始是文本"。

## 它和 `<|audio_start|>` 是一对

`<|audio_start|>` 出现在两个地方,都是**为下一轮音频开口子**:

**会话建立时**([qwen_engine.py:111](src/nonrag_streaming/qwen_engine.py:111)):
```python
f"<|im_start|>system\n{instructions}<|im_end|>\n"
"<|im_start|>user\n<|audio_start|>"        # 停在这,等音频进来
```

**每轮生成完之后**([qwen_engine.py:214](src/nonrag_streaming/qwen_engine.py:214)):
```python
self._feed_text("<|im_end|>\n<|im_start|>user\n<|audio_start|>")
```

生成完立刻把下一轮的 user 头和 `<|audio_start|>` 写进去,KV 又停在"等音频"的状态。所以下一轮的音频可以直接往后追加,不用重新组装 prompt。

## 一轮完整的 token 序列

```
<|im_start|>system\n {指令} <|im_end|>\n
<|im_start|>user\n <|audio_start|>
        ↑ 会话建立时写到这里，然后停住

    [音频 embedding 按 1 秒窗口持续追加]     ← push_pcm 期间

<|audio_end|>\n                             ← commit 时补上，第 185 行
{确定性上下文}\n                             ← 可选，回执/任务状态塞这
<|im_end|>\n
<|im_start|>assistant\n
        ↑ 从这里开始逐 token 解码

{生成的文本}
<|im_end|>\n <|im_start|>user\n <|audio_start|>
        ↑ 生成完立刻写，第 214 行，为下一轮开口子
```

## 为什么零 token 提交也要走这一遭

`commit(max_new_tokens=0)` 时,`limit=0`,那个 `for _ in range(limit)` 循环一次都不进,但 `tail` 照样喂进去了,第 214 行的下一轮开头也照样写了。

这就是文档里"rules 已把话说完时仍走一次 `max_new_tokens=0` 的闭合,只闭合 KV 不生成"的实现 —— rules 直答省掉了生成,但 KV 里的对话结构不能断:少了 `<|audio_end|>` 和后面那串,下一轮的音频会直接接在上一轮音频后面,模型看到的就是一段没有边界的连续音频。

函数注释里写得很直白:

> generating — used when a rules-based reply already answered.

## 注意 `_flush_tail()` 的位置

在 `<|audio_end|>` 之前([qwen_engine.py:184](src/nonrag_streaming/qwen_engine.py:184))。因为音频按 1 秒窗口编码,用户说了 2.3 秒的话,那 0.3 秒还在 buffer 里没进 KV。必须先补齐再写结束标记,否则最后 0.3 秒的语音就丢了 —— 往往正好是句尾的关键词。
