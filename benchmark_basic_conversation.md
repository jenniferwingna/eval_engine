
## 四、语音对话基准自带的评测代码

### 4.1 中文与中英基准

| 基准 | 语言与录音 | 多轮 | 噪声 / 方言 | 语音输出评测 | 裁判与读法 | 换本地裁判 | 许可 |
|---|---|---|---|---|---|---|---|
| **[TELEVAL](https://github.com/Tele-AI/TELEVAL)**（中国电信） | 以中文为主，含 5 种方言 | ✅ | 12 种声学条件，按最差情况的均值计分；方言理解与方言回应 | Paraformer-zh 回识一致性、DNSMOS、emotion2vec、方言分类 | API：Azure gpt4o；离线：Qwen3-30B（4 卡，温度 0.6）；HumDial 任务：Qwen3-Omni 听音频 | **在 registry 里选离线评估器**，不用改代码 | Apache-2.0 |
| **[VCB Bench](https://github.com/Tencent/VCB-Bench)**（腾讯，ACL 2026 Findings） | 中文，**全真人录音** | ✅ MTD | 说话人（年龄、口音、音量、语速）、环境（回声、户外、远场、电视、背景对话、广播、丢包）、内容（语气词、重复、读错） | 语音指令遵循（SIF）由 Gemini-2.5-Pro 听音频 | 文本类：gpt-4o-mini 读转写，温度 0.5，分数乘 20 | 改 `use_azure`、`api_base` 和模型名；SIF 需要本地音频 LLM | 代码 Apache-2.0（`LICENSE-code.txt`）；数据许可未声明 ⚠️ |
| **[URO-Bench](https://github.com/Ruiqi-Yan/URO-Bench)**（上海交大） | 中英成对，分 basic / pro 两档 | ✅ | — | Paraformer-zh / Whisper 回识、UTMOS、emotion2vec；论文定义了首包延迟，仓库里没看到测量脚本 ⚠️ | gpt-4o-mini（温度 0.5，n=3）读转写；SRT 类由 gpt-4o-audio 听音频；部分用 Gemini-2.0-Flash | 改 `mark_gpt.py`；SRT 和 Gemini 部分要重写 | MIT |
| **[VocalBench-zh](https://github.com/SJTU-OmniAgent/VocalBench-zh)**（上海交大） | 普通话，10 个子集，超过 1 万条，12 种用户角色 | ⚠️ | ⚠️（英文版 VocalBench 有 6 类声学扰动，按保持率计） | UTMOS、回识 WER | qwen-max-2025-01-25 读转写，温度 0 | 在 `utils/config.py` 里改 `base_url` 和请求体里的 `model`（构造参数 `model_name` 不生效） | Apache-2.0 |
| [CS3-Bench](https://github.com/SJTU-OmniAgent/CS3-Bench) | 中英语码切换 | ⚠️ | — | 同 VocalBench | 同 VocalBench | 同 VocalBench | Apache-2.0 |
| **[HumDial-EIBench](https://github.com/ASLP-lab/HumDial-EIBench)**（西工大 ASLP） | 中文 528 条 + 英文 549 条，**真人录音** | ✅ 3–5 轮 | — | 声学共情由人工评 | 任务 1、2、4 是选择题；任务 3 共情生成由**本地 Qwen3-Omni-30B-A3B 听音频**打 1–5 分 | 已经是本地，填模型路径即可；TELEVAL 的 registry 里也有对应评估器 | ⚠️ |
| C3（[arXiv 2507.22968](https://arxiv.org/abs/2507.22968)） | 中英；歧义、省略、指代 | ✅ | — | — | GPT 读文本，温度 0 | **只改 `OPENAI_BASE_URL` 和 `--model_name`** | ⚠️ |
| SpeechParaling-Bench（[arXiv 2604.20842](https://arxiv.org/abs/2604.20842)） | 中英平行，1,000 多条，100 多种副语言特征 | — | — | 音频 LLM 与固定基线两两比较 | 同左 | 代码未核 ⚠️ | ⚠️ |
| Reject or Not?（[arXiv 2512.10257](https://arxiv.org/abs/2512.10257)） | 中文智能家居拒识，11,913 条文本与语音配对，12 类对话 | ✅ | — | — | ⚠️ | 代码未核 ⚠️ | ⚠️ |

TELEVAL 的两套离线评估器（[`registry/evaluator/llm_offline.yaml`](https://github.com/Tele-AI/TELEVAL/blob/master/registry/evaluator/llm_offline.yaml)，已核实）：

```yaml
llm_offline:
  class: src.evaluator.llm.qwen_scorer.QwenScorer
  args:
    llm_name: qwen3-30b
    judge_task: empathy_response
    generate_params: {ngpus: 4, max_tokens: 32768, temperature: 0.6, top_p: 0.95}

humandial_task3_llm_offline:
  class: src.evaluator.llm.qwen_scorer.Qwen3OmniScorer
  args:
    llm_name: qwen3_omni
    judge_task: humandial_emotion_response
    generate_params: {ngpus: 4, max_tokens: 16384, temperature: 0.6, top_p: 0.95, top_k: 20}
```

旧的 `llm.yaml` 里还留着一条 `LLMOfflineScorer`，模型名写成 `qwen2.3-72b-instruct`（8 卡，温度 0.7），应是笔误。离线评估器温度为 0.6，同一份结果重判会有波动；正式评测建议改成 0，或者固定判多次取均值。

### 4.2 英文基准

| 基准 | 评什么 | 语音输出 | 裁判与读法 | 换本地裁判 | 框架收录 | 许可 |
|---|---|---|---|---|---|---|
| [VoiceBench](https://github.com/MatthewCYM/VoiceBench) | 11 个子集：开放闲聊、口音问答、知识选择题、指令遵循、推理、安全 | 需自行回识 | gpt-4o-mini 写死，温度 0.5，n=3，读转写 | `OPENAI_BASE_URL` + 改模型名；服务端要支持 n=3 | lmms-eval（全部）、UltraEval、Kimi、AU-Harness（部分） | Apache-2.0 |
| [Audio MultiChallenge](https://labs.scale.com/leaderboard/audiomc) | 多轮语音 rubric：记忆、指令保持、自我纠正 | — | 论文用 o4-mini 读转写，rubric 全部满足才算通过 | **官方未开源评测代码**；MiMo-Audio-Eval 有一份实现 | MiMo | ⚠️ |
| [Big Bench Audio](https://huggingface.co/datasets/ArtificialAnalysis/big_bench_audio) | BBH 推理题转成语音，1,000 题 | 回识 | 官方用 Claude 系判对错，已换过三代裁判 | AU-Harness、MiMo 内置 | AU-Harness、MiMo | MIT |
| [SD-Eval](https://github.com/AmphionTeam/SD-Eval) | 情感、口音、年龄、环境四类副语言感知 | — | gpt-4o，温度 0，1–10 分 | `--model` + `OPENAI_BASE_URL` | — | Apache-2.0 |
| [WildSpeech-Bench](https://github.com/Tencent/WildSpeech-Bench) | 真实用户问题经 TTS 合成，评语音到语音 | 回识 | gpt-4o-mini，温度 0.5，逐题 checklist | 改源码 | — | ⚠️ |
| [VoiceAssistant-Eval](https://github.com/mathllm/VoiceAssistant-Eval) | 13 类任务，覆盖听、说、看 | UTMOS、语音与文本一致性、说话人相似度 | **本地 gpt-oss-20b**，温度 1.0 | 已经是本地；温度要改 | — | ⚠️ |
| [MTalk-Bench](https://github.com/FreedomIntelligence/MTalk-Bench) | 多轮语音到语音：语义、副语言、环境音 | 裁判听音频 | 音频 LLM，温度 0.1，竞技场 + rubric | 改常量；需要本地音频 LLM | — | Apache-2.0 |
| [Speech-IFEval](https://github.com/kehanlu/Speech-IFEval) | 语音指令遵循 | — | gpt-4o-2024-11-20 写死 | 改源码 | — | ⚠️ |
| [ADU-Bench](https://github.com/KuofengGao/ADU-Bench) | 开放式音频对话，含多语子集 | — | gpt-4-0613 写死 | 改源码 | — | ⚠️ |
| [VoxEval](https://github.com/dreamtheater123/VoxEval) | 语音到语音的知识问答 | 回识 | 正则或 GPT | 用正则模式即可 | — | ⚠️ |
| [Audio2Tool](https://audio2tool.github.io/)（Interspeech 2026） | **智能座舱**、家居、可穿戴设备的语音工具调用，约 3 万条，8 档复杂度，含噪声与重叠语音 | — | ⚠️ | 仓库里只有 README ⚠️ | — | ⚠️ |

SOVA-Bench、S2S-Arena、VoxDialogue、EChat-eval 没有找到可用的评测代码（EChat-eval 的数据尚未发布）。

### 4.3 音频理解基准的评分

| 基准 | 规模 | 评分 | 注意 |
|---|---|---|---|
| MMAU（[arXiv 2410.19168](https://arxiv.org/abs/2410.19168)） | 约 1 万题 | 字符串匹配 | test 集要提交官方评测 |
| MMAU-Pro（[arXiv 2508.13992](https://arxiv.org/abs/2508.13992)） | ⚠️ | 选择题用 NVEmbed 匹配；开放题由 Qwen2.5-7B 打 1–5 分 | |
| [MMAR](https://github.com/ddlBoJack/MMAR) | 1,000 题 | 词集合匹配；MMAR-Rubrics 用 gpt-4o-2024-11-20，跑 5 遍、去掉最高和最低后取均值 | Rubrics 约需 5,000 次调用 |
| [MMSU](https://github.com/dingdongwang/MMSU)（ICLR 2026） | 5,000 题，47 个子任务 | 规则 | **与 VoiceBench 的 `mmsu` 子集不是一个东西** |
| AIR-Bench | foundation 选择题 + chat 开放题 | 准确率；chat 由 GPT-4 打 1–10 分，并交换位置 | |

---
