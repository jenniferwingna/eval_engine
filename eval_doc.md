# REPO_GUIDE — 全双工模型评测仓库摸底报告

> 写给仓库新负责人。目标：看完就能自己跑一次 Full-Duplex-Bench（fdb）v3 评测、看懂分数、定位 bad case。
> 术语第一次出现都用大白话解释。本文只读仓库产出，未改动任何代码；唯一动过的是跑了零 GPU 单测（§7）。
> 生成日期：2026-10-12。基线：分支 `omni-hwx`，HEAD `9a56c14`（2026-09-29）+ **工作区有未提交改动**（§6 坑 1）。

---

## 1. 一句话总结

这个仓库是**多模态大模型统一评测框架（omni_eval）**，你关心的部分是它的**流式全双工链路**：把测试音频按 **0.24 秒一片**连续"喂"给 vLLM 双工推理服务（模型边听边决定说/不说），录下模型的反应，再用**规则（少量场景用裁判模型）**算分——测模型"该接话时接话、不该接话时闭嘴、被打断时停下"。

**两套"全双工"要分清**：

| 名字 | 是什么 | 代码入口 |
|---|---|---|
| **内部 FD 4 族**（fd_pause/interrupt/over/backchannel） | 内部中文测试集（Full-Duplex-中 / EasyTurn-中 / HumDial-中），自产自评 | `omni_eval/uds_fd_turntaking.py`，config `eval_fd_*` |
| **官方 FDB**（Full-Duplex-Bench v1.0/v1.5/v3） | 官方英文基准，**你的直接用途**。v3 = 口吃场景下的多步工具调用 | `docs/fdb/README.md`，本文主角 |

**fdb v3 是什么（大白话）**：79 个场景（订机票/查快递/找房子等），用户用**带口吃、改口、迟疑**的英文口语提需求，模型要边听边调**模拟 API**（12 个假接口，如 `track_order` 查订单），把该调的接口都调对、参数填对、最后开口回答。每条**二值计分**（全对才 1 分）。

**当前真实水平（step13041_2h checkpoint）**：pass_rate ≈ **0/79**。这不是打分 bug，是模型行为问题——79 条里只有 8 条产出过工具调用，其余全程输出 `<|listen|>` 控制符（模型一直处于"听"的状态，从不开口）。深挖报告已被移出 git（§6 坑 4）。

---

## 2. 快速开始（fdb v3，复制粘贴级）

前提：4 张空闲 GPU；conda 环境 `/yes/envs/vllm-mola`（推理）与 `/yes/envs/omni_benchmk`（打分）已装好（装法见 `docs/ONBOARDING.md §5.4`）；在仓库根目录执行。

```bash
cd /home/ma-user/work/h30079575/codes/Omni-Bench

# ── ① 数据转换（一次性，checkpoint 无关；产出 79 行 v1.1 ledger + 16k wav）──
/yes/envs/omni_benchmk/bin/python scripts/convert_v11_fdb3.py --validate
#   输入: /cache/Full-Duplex-Bench-data/v3.0/fdb_v3_data_released/（100 目录→79 唯一场景）
#   输出: /cache/Full-Duplex-Bench-data/omni_ledger_v11/fdb_v3/fdb_v3_v1.1.jsonl

# ── ② 起双工推理服务（约 5 分钟；必须用此脚本，手动 vllm serve 必崩，见 §6 坑 12）──
CKPT=/cache/h30079575/models/1h_li_resp720_intr720_step13041_streaming_merged  # 换新 checkpoint 只改这里
bash scripts/start_vllm_omni_duplex.sh --model $CKPT --port 8190 --gpus 0,1,2,3 \
  --config configs/qwen3_omni_duplex_thinker_tp4_guard.yaml
curl -s http://127.0.0.1:8190/v1/models   # 就绪探针

# ── ③ 一条龙：export→convert→toolloop 推理→import→score（官方推荐托管方式）──
export STREAMING_MODEL=$CKPT
export STREAMING_MODEL_NAME=step13041_2h          # 新 checkpoint 用新名字（进 cache_key）
OMNI_BENCH_REPO_ROOT=$PWD bash scripts/run_streaming_full_eval.sh --use-run-manager fdb_v3
#   正式跑前先演练：加 --dry-run（只做 PRE-FLIGHT 校验，0 GPU 推理）

# ── ④ 看分数 ──
cat outputs/fdb_v3/$STREAMING_MODEL_NAME/fdb_v3/metrics/fdb_v3.json
#   accuracy 就是官方 pass_rate；先查 error_n/valid_n（推理失败的样本不算数，见 §6 坑 3）
#   bad case 看 predictions/fdb_v3.jsonl 里每条的 metadata.fdb3_failure_reason

# ── ⑤ 跑 v1.0/v1.5 官方 9 子集（可选）──
OMNI_BENCH_REPO_ROOT=$PWD bash scripts/run_streaming_full_eval.sh --use-run-manager \
  fdb_pause_candor fdb_turn_taking_candor fdb_backchannel_icc fdb_pause_synthetic \
  fdb_interrupt_synthetic fdb_user_interruption_v15 fdb_user_backchannel_v15 \
  fdb_talking_to_other_v15 fdb_background_speech_v15
#   interrupt 两个子集需要裁判服务: bash scripts/start_judge_vllm.sh（9001 端口）

# ── ⑥ 零 GPU 自检（随时可跑，~6s）──
/yes/envs/omni_benchmk/bin/python -m pytest tests/test_fdb_streaming.py \
  tests/test_fdb3_eval.py tests/test_fdb3_streaming.py -q
#   已知 5/60 failed（测试落后于代码，见 §6 坑 2），只要失败数不涨就说明链路没坏
```

手动分步跑（调试用，等价于 ③，参考 `scripts/run_fdb_v3_chain.sh:16-39`）：convert_ledger_realtime → run_realtime_toolloop_batch → import_output_events --fd3 → omni_eval.cli --mode standard。

---

## 3. 数据流图（fdb v3，一次完整评测的旅程）

```
官方数据  /cache/Full-Duplex-Bench-data/v3.0/fdb_v3_data_released/
          100 个目录 × (input.wav + metadata.json) → 去重后 79 唯一场景
          （tools/fdb3/fdb3_scenarios.py:30 load_scenarios，21 组同 id 重录目录）
   │  scripts/convert_v11_fdb3.py
   ▼
v1.1 ledger  /cache/Full-Duplex-Bench-data/omni_ledger_v11/fdb_v3/fdb_v3_v1.1.jsonl（79 行）
          tool 调用协议文本 bake 进 instruction；gold 进 metadata.fdb3_gold
   │  run_streaming_full_eval.sh 托管步骤 [3/19]：export v1.0 + convert_ledger_realtime.py
   ▼
回放事件流  <work>/converted/events/*.jsonl（每样本一份）
          内容：0.24s 一片的 input_audio_buffer.append + session.update（协议文本在 context_text）
   │  步骤 [12/19]：run_realtime_toolloop_batch.py（vllm-mola 侧）
   │    WebSocket ws://127.0.0.1:8190/v1/realtime?duplex=1，最多 4 轮 tool-loop
   ▼
合并输出事件  output_logs/<sample>.jsonl
          chunk.decision（每 0.24s 一条，agent_silent / thinker_token_ids）
          + fdb3.tool_call（客户端解析标记后本地执行假 API 的事件）
          + fdb3.sample_summary；另有 fdb3_predictions.jsonl sidecar
   │  步骤 [14-15/19]：tools/import_output_events.py --fd3 --run-mode default
   ▼
predictions  outputs/fdb_v3/<model>/<run.name>/predictions/fdb_v3.jsonl
          parsed_answer = {"tool_calls": [...], "n_rounds": N, "session_ok": true}
   │  步骤 [16/19]：omni_eval.cli --mode standard（全 cache 命中，不推理）
   │    adapter FDBV3.evaluate_one（omni_eval/benchmarks/uds_streaming_fdb3.py:82）
   │    → tools/fdb3/fdb3_eval.py:191 evaluate_scenario_pass
   ▼
最终指标  outputs/fdb_v3/<model>/<run.name>/metrics/fdb_v3.json + reports/report.md
```

**全双工"边听边说"是怎么模拟的**：服务端把用户音频切成 0.24 秒一片，每收一片就做一次决策，返回一个 chunk.decision 事件——模型这次决定"继续听"（agent_silent=true，输出 listen 占位 token）还是"开口说话"（输出真实文本 token）。评测侧不传音频文件，而是按 0.24s 节奏（或 --no-realtime-pacing 全速）逐片推流。时间锚 = (seq-1)×0.24 秒（曾因用 seq×0.24 系统性偏移 0.24s，2026-09-09 修正，见 omni_eval/uds_fd_turntaking.py:66 注释）。停止条件：连续静音 1920ms（fdb_v3 特殊用 4800ms，因 v3 音频末尾没有静音垫）或 tail 4096 片兜底。

**tool-loop 多轮**：round 1 模型若在文本里产出 tool-call 标记（或裸 JSON 信封），客户端解析出 {name, arguments}，本地调 tools/fdb3/fdb3_mock_apis.py:105 MockAPIRegistry（12 个假接口，instant 零延迟）拿到假结果；round n 起**新会话**重放同一段音频，把协议+上轮回复+工具结果拼进 context_text。官方是真 function-calling 通道且同一会话——这是已声明口径差异（internal_comparable，docs/fdb/README.md deviation 表 #5/#6）。

---

## 4. 指标说明表

### fdb_v3（你的主战场）

| 指标 | 大白话 | 方向 | 在哪算 | 依赖 |
|---|---|---|---|---|
| **accuracy = 官方 pass_rate** | 每条：该调的工具全调到 + 没多调 + 参数全对，三条同时满足才得 1 分 | 越高越好 | tools/fdb3/fdb3_eval.py:191 evaluate_scenario_pass（adapter 包装 omni_eval/benchmarks/uds_streaming_fdb3.py:82） | 纯规则，无外部依赖 |
| fdb3_tool_f1（metadata 辅助） | 工具名集合的 F1（调全与调准的调和平均），比二值 pass 更细 | 越高越好 | fdb3_eval.py:35 evaluate_tool_selection | 无 |
| fdb3_recall / precision | 该调的调齐几成 / 调出来的都是该调的几成 | 越高越好 | 同上 | 无 |
| fdb3_failure_reason | 失败原因：Missing tools / Unexpected tools / Wrong arguments | — | fdb3_eval.py:226,260 | 无 |
| turn_take_rate | 模型有没有开口（transcript 非空即算） | 越高越好 | fdb3_eval.py:171 | 无 |
| agent_response_latency_s | 用户说完到模型开口秒数（负值=抢话） | 越低越好 | fdb3_eval.py:141；时间来自 run_realtime_toolloop_batch.py:149 extract_timing | 缺 result_data 则不计 |

**拿真实样例算一遍 pass_rate**（/cache/h30079575/data/fdb_v11_work/repro/fdb3_predictions.jsonl 第 1 条，step13041_2h 全量 run）：
- 场景 ecommerce_01：用户口述查订单，gold 要求调 track_order(order_id=...)。
- 模型实际：45.8 秒音频每片都决策 listen，从未开口，tool_calls=[]。
- 计算：expected=[track_order]，actual=[] → recall=0/1=0 → 失败，failure_reason="Missing tools: ['track_order']" → 该条 0 分。
- 对照"开口者" ecommerce_08：调了 search_products(query="mechanical keyboard", max_price=200)，工具名对但 gold 要求更多，仍不 PASS。全量 79 条仅 8 条调过工具（共 8 次调用），最终 PASS 0~1/79（文件快照 1，终局报告 0，见 §6 坑 3）。

### FDB v1.0/v1.5 官方 9 子集（行为族，TOR 口径）

TOR（Turn-Over Rate，接管率）大白话：模型"接了话轮"没有。模型出声且持续够长（>1 秒或 >3 词，omni_eval/fdb_eval_rules.py:45 _tor_from_chunks）记 TOR=1。**方向由子集决定**（fdb_eval_rules.py:209 tor_group_correct，2026-10-10 修正版）：

| 子集 | 题数 | 测什么 | TOR 方向 | 附加指标 | 规则函数 |
|---|---|---|---|---|---|
| fdb_pause_candor | 216 | 用户停顿，模型别抢话 | TOR=0 好 | — | fdb_eval_pause（:68） |
| fdb_pause_synthetic | 137 | 同上（合成停顿） | TOR=0 好 | — | 同上 |
| fdb_turn_taking_candor | 119 | 用户说完，模型及时接 | TOR=1 好 | fdb_latency（s） | fdb_eval_turn_taking（:78） |
| fdb_interrupt_synthetic | 200 | 模型说话被插话，要停下重答 | TOR=1 好 | fdb_latency + judge 0-5 分 | fdb_eval_interrupt（:186） |
| fdb_user_interruption_v15 | 400 | 同上 v1.5 版 | TOR=1 好 | 同上 | 同上 |
| fdb_backchannel_icc | 55 | 用户长篇讲话，模型别抢 | TOR=0 好 | fdb_frequency / fdb_jsd | fdb_eval_backchannel（:101） |
| fdb_user_backchannel_v15 | 196 | 同上 | TOR=0 好 | — | 同上 |
| fdb_talking_to_other_v15 | 200 | 用户对别人说话，模型别插嘴 | TOR=0 好 | — | 同上 |
| fdb_background_speech_v15 | 200 | 背景人声，模型别误触发 | TOR=0 好 | — | 同上 |

时间线来自 omni_eval/uds_fd_turntaking.py extract_fd_timeline（0.24s chunk 网格）；interrupt 质量分由裁判模型打（官方 gpt-4-turbo，本库 omni_eval/metrics/fdb_judge.py FDBInterruptJudge = Qwen3.8-27B 服务 :9001）。这些分数只在本库内部可比（与官方有 4 处已声明偏差，docs/fdb/README.md §4）。

> 读历史报告的方向陷阱：2026-10-09 之前的批次 pause 族按"TOR=1 好"记分，数值实际是抢话率（越低越好），与现在相反——fdb_eval_rules.py:216 注释有详细说明。

---

## 5. 换一个新 checkpoint 要改哪几处

**结论：代码零改动，只用环境变量 + 启动参数。**

| # | 改哪 | 怎么改 | 说明 |
|---|---|---|---|
| 1 | checkpoint 路径 | export STREAMING_MODEL=/cache/h30079575/models/<新ckpt> | scripts/run_streaming_full_eval.sh:67 读此 env |
| 2 | 模型名 | export STREAMING_MODEL_NAME=<新名字> | 同文件 :68；进 cache_key 第 1 分量，import/score 必须同名；也是 outputs 目录第二段 |
| 3 | 服务启动 | start_vllm_omni_duplex.sh --model $STREAMING_MODEL ... | 脚本默认模型是 step13185 路径，必须显式传 --model |
| 4 | tokenizer | 不用改 | import 与 toolloop 都用 --tokenizer $MODEL（= STREAMING_MODEL） |

两个不要踩：
- eval_fdb_v3.yaml 里 model.model_name: qwen3-omni-duplex 不用改——run_score 用 CLI --model-name $MODEL_NAME 覆盖（run_streaming_full_eval.sh:403），它只是 yaml 声明。
- 输出目录是 outputs/fdb_v3/<STREAMING_MODEL_NAME>/fdb_v3/。run.name 千万别复用旧名（旧 cache 会让 score 读回假分数，历史上 N=3000 实验曾误报 43.97%）。

fdb v1.0/v1.5 的 9 子集同理，把 fdb_v3 换成对应子集名（一次可传多个）。历史战役脚本 scripts/run_fdb_v3_chain.sh / run_repro_driver.sh 里模型名是写死的（step13041_2h），复用时要改脚本内变量。

---

## 6. 坑清单（按严重程度排序）

### 高危

**坑 1：核心评分代码有未提交的滞留改动（git status 可见），换机器/重 clone 会丢**

```
 M omni_eval/fdb_eval_rules.py      # TOR 方向修正（2026-10-10 版，§4 方向表的依据）
 M omni_eval/metrics/evaluator.py   # 注册 fdb_tor/fdb_latency/fdb_frequency/fdb_jsd 四个 metric
 M omni_eval/metrics/fdb_judge.py
?? omni_eval/metrics/fdb_tor_metric.py   # 全新文件，evaluator.py:28 直接 import 它
```

fdb_tor_metric.py 是 untracked 新文件——这个文件一丢，evaluator.py import 报错，整个评分链路崩。建议尽快 commit（或至少备份）。另有 6 个 M 文件 + 大量 untracked 的 VCB v4 配置（eval_uds_streaming_vcb_*_incremental_v4_*.yaml，与 2026-10-09 DI 全量批次相关）。

**坑 2：零 GPU 单测 5/60 失败——测试落后于代码，不是链路坏了**

tests/test_fdb_streaming.py 的失败：a) registry 断言 10 个注册名、实际 15 个（:97，后来加了别名注册）；b) pause 族 3 个断言按旧方向口径写"TOR=1 则 correct=1"（:163,405），与 2026-10-10 方向修正后的代码（TOR=0 才 correct=1）相反。看懂即可，跑分不受影响。

**坑 3：fdb_v3 的"0/79"有两层假象，看分数先查 error_n 和去重**

outputs/fdb_v3/step13041_2h/fdb_v3/ 下有两份 predictions：

| 文件 | 行数 | 内容 | 结论 |
|---|---|---|---|
| predictions/fdb_v3.jsonl | 79 | 全部 error: "max retries exceeded"（2026-09-24 10:20 GPU 被外部抢占，服务死亡） | 假 0 分，作废 |
| fdb_v3.jsonl（root） | 86（含 7 行 crash-resume 重复，去重后 79） | 有效 run（repro 那次），PASS 1/79 | 真实快照 |

有效 run 的原始推理产物在 /cache/h30079575/data/fdb_v11_work/repro/（79/79 成功、2h11m）。每次看分先做两件事：metrics/summary.json 查 error_n/valid_n；predictions 按 sample_id 去重再统计（重复行会污染分母，ONBOARDING 血泪教训 #21）。

**坑 4：报告类文档已被移出 git**

提交 bf6fb14（2026-10）删除了 FDB 全套报告、v3 0/79 深挖（diagnostics/fdb_v3_0of79_deepdive_20260924.md）、周报等，提交信息说"本地 D:\code4eval 为准"。接手前务必向前任要这份本地目录，否则 v3 失败机制（512-token 悬崖 + 音频门控）等深挖结论只能从 git commit message 倒推。

### 中危

**坑 5：scripts/streaming_profiles.yaml:1592 的 fdb_v3 profile 四个字段填错了**

score_config / import_bmark / v11_ledger / known_issue 填的是 librispeech 的值（复制粘贴残留）。实际跑批不受影响——run_streaming_full_eval.sh:758-767 的 case 分支硬编码了正确值兜底；但直接读 profile 的分析工具/人会踩。建议修正。

**坑 6：写死路径 4000+ 处**。/home/ma-user/work/h30079575、/home/ma-user/work/l30082962（vllm-mola 只读）、/cache/h30079575、/cache/Full-Duplex-Bench-data、/yes/envs/*。重灾区：scripts/recover_fdb.sh、run_fdb_eval*.sh、run_repro_*.sh、run_fdb_v3_chain.sh。换机器必改；主入口 run_streaming_full_eval.sh 已支持 env 覆盖（OMNI_BENCH_REPO_ROOT / STREAMING_MODEL / STREAMING_MODEL_NAME / VLLM_OMNI_ROOT）。

**坑 7：.git/config 的 push URL 内联了访问凭据**（remote.origin.pushurl）。push 请显式用 ssh URL（ONBOARDING 5.1 同款警告）：git push ssh://git@codehub-y-git.rnd.yinwang.com:2222/IASResearch/Omni-Bench.git omni-hwx。

**坑 8：两套 FDB 工作目录并存，找产物别看错**。/cache/h30079575/data/streaming_full_work/fdb_v3/（托管脚本默认 WORK_ROOT，当前 output_events 为空）vs /cache/h30079575/data/fdb_v11_work/（真实产物所在：toolloop 输出、probe 日志、repro）。v3 有效产物在后者。

**坑 9：predictions 落点两套**。规范是 {output_dir}/{model}/{run.name}/predictions/ 子目录，但 run_fdb_v3_chain.sh:35 直接落到 outputs/fdb_v3/step13041_2h/fdb_v3/（root）——这正是坑 3 两份文件并存的原因。手动 import 遵守规范路径。

### 低危 / 提醒

**坑 10：fdb_v3 推理参数三处特殊值**（run_streaming_full_eval.sh:303-311 已内置，手动跑别漏）：--adaptive-silence-stop-ms 4800（默认 1920 会全静默，v3 音频无尾部静音）、--tokenizer-path $MODEL（必须，解码标记）、--max-rounds 4。import 必带 --fd3（漏了 parsed_answer 为空 → 静默 0 分）。

**坑 11：eval_fdb_v3.yaml 的 judge_lifecycle.mode: disabled 不会短路**，必须同时补 model/port/conda_env 三字段让健康检查直接通过（yaml 注释写了缘由），否则 spawn 僵尸进程 + 每子集白等 600s。

**坑 12：duplex 服务只能用 scripts/start_vllm_omni_duplex.sh 启动**。手动 vllm serve 漏 QWEN3_OMNI_THINKER_ONLY=1 会 prewarm KeyError 崩；脚本还内置 cuda-13.0 兼容 LD_LIBRARY_PATH。

**坑 13：一次性战役脚本群**。scripts/ 下 run_fdb_eval*.sh、run_fdb_v3_chain.sh、run_fdb_v2_chain.sh、run_repro_*.sh、recover_fdb.sh 是 2026-09-24 前后 FDB 全套评测战役的驱动脚本（含等待循环、tmux 守护），模型名写死 step13041_2h。日常入口是 run_streaming_full_eval.sh，前者只当参考模板。

---

## 7. 试跑记录（零 GPU 部分；GPU 部分待确认后补）

| 步骤 | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 数据自检 | convert_v11_fdb3.py 的依赖链静态走读（scenario loader + validate） | 79 唯一场景在盘，ledger 79 行确认 | — |
| 零 GPU 单测 | /yes/envs/omni_benchmk/bin/python -m pytest tests/test_fdb_streaming.py tests/test_fdb3_eval.py tests/test_fdb3_streaming.py -q | 55 passed / 5 failed（失败原因=坑 2，非链路问题） | 5.6s |
| 产物核对 | 读 outputs/fdb_v3/step13041_2h/ 两份 predictions + fdb_v11_work/repro/report.json | 发现坑 3 两层假象；repro 79/79 成功、8 次 tool call | — |

尚未做的：端到端 GPU 试跑（起服务 + 2-3 样本全链路）。预计开销：4 卡约 10 分钟起服 + 每样本 1-10 分钟推理（repro 全量 79 条用 2h11m @ 并发 4，单样本均摊约 1.6 分钟）。涉及占卡，等确认再跑。

---

## 8. 我还没搞懂的地方（明确不确定，勿当结论用）

1. **未提交改动的归属与提交计划**：TOR 方向修正 + fdb_tor_metric.py 与 2026-10-09 的 DI 全量批次（另一条工作线）时间重叠，不确定是否已约定"跑完某批次后一起 commit"。建议与交接人/协作会话确认后再动 git。
2. **v3 失败的机制细节**：git 提交信息提到"512-token 悬崖 + 音频门控，174 会话三电池"，但深挖报告已被移出 git（坑 4）。我只能确认表象（listen 卡死 + 仅 8 次调用），无法复述机制结论——拿到 D:\code4eval 后请核对。
3. **chain 脚本 --max-tail-chunks 256 vs 托管脚本 4096**：v3 是短 tool-call 场景，256 可能是刻意为之（repro 也用 256），但没有文档说明两种取值对 v3 分数的影响。不确定哪个是"标准"。
4. **evaluate_all 里 turn_take_rate 的实现**（fdb3_eval.py:306）用 _avg([1] * len(turn_taken)) 计算，有样本时恒等于 1.0；真正有意义的比例是旁边的 rate 字段。疑似移植瑕疵，不影响主指标 pass_rate，但读报告时别用错字段。
5. **outputs_smoke_fdb3/**（仓库根目录，内容近空）和 diagnostics/fdb3_demo_raw/（4 条 demo 轨迹）的定位——推测是接入期的 smoke 留档，未找到文档说明。
6. **scripts/run_streaming_full_eval.sh 头部注释**写 vllm-mola 源码在 z00986117，实际默认值（:53）已是 l30082962——注释过期，以代码为准。

---

## 9. 推理服务仓 vllm-mola（duplex-lm-graph / demo_2heads）

> 位置：`/home/ma-user/work/l30082962/vllm-mola`（**他人目录，只读**）。本机另有可写副本 `/home/ma-user/work/h30079575/codes/vllm-mola`（当前在 demo_2heads_di 分支，DI 多节点方向用）。
> 实质：vllm-project/vllm-omni 的内部 fork（vllm 0.26.0），评测相关的核心增量集中在 `vllm_omni/experimental/fullduplex/`（duplex 运行时）和 `vllm_omni/examples/qwen3_omni_duplex/`（客户端群）。

### 9.1 分支现状（2026-10-12 核实，已更正 ONBOARDING 的过期记录）

| 分支 | 远端 HEAD | 内容 |
|---|---|---|
| `demo_2heads` | `3b70b44`（09-15） | 提速三件套（engine clock / tower 跨 session 批处理 / tower CUDA graph）+ 批处理诊断工具 |
| `duplex-lm-graph` | `15b015c`（09-22） | = demo_2heads 全部 + 1 提交（**fdb_v3 toolloop 批量客户端**）。**服务引擎实际在用这个**（l30082962 工作区 checkout 即此） |

ONBOARDING 说"duplex-lm-graph = 3b70b44"已过期——15b015c 是其之后的新提交。分析以 15b015c 为准（它与 demo_2heads 的差异只有 toolloop 客户端一个文件 `run_realtime_toolloop_batch.py`）。

### 9.2 目录地图（只列评测链路用到的）

```
vllm_omni/
├── experimental/fullduplex/              # duplex 运行时核心
│   ├── qwen3_omni/
│   │   ├── worker.py            (1556行) # duplex worker：logits hook（force_listen / 2-head state_head）
│   │   ├── data_plane.py                 # chunk 级数据面；agent_silent 判定（:736-778）
│   │   ├── runtime.draft.yaml            # 全局行为配置（见下）⚠️ 全局，改了影响所有视觉 benchmark
│   │   ├── CHUNK_LAYOUT.md               # chunk 布局权威文档（0.24s/3音频token/1帧每4chunk/agent每chunk 1 token）
│   │   ├── streaming_audio.py   (1339行) # 流式音频 tower 处理（三件套之二：批处理）
│   │   ├── tower_cuda_graph.py           # 三件套之三：audio tower 整图捕获
│   │   └── serving_adapter.py            # 上下文滚动窗口（context roll，:434）
│   └── openai/
│       ├── runtime_bridge.py             # WS 桥：chunk.decision 事件构造（:830-865）+ engine clock（:199）
│       └── realtime_output.py            # realtime API 输出投影
├── examples/qwen3_omni_duplex/           # 客户端群（评测直接调用）
│   ├── convert_ledger_realtime.py        # v1.0 ledger → events（0.24s chunk 事件流）
│   ├── replay_realtime_events.py         # 核心 replay：推流 + DualSilenceStopper（:34-62 连续静音判定）
│   ├── run_realtime_batch.py             # 批量并发壳（VCB/fdb v1.0/v1.5/一切单轮 benchmark 用）
│   ├── run_realtime_toolloop_batch.py    # fdb_v3 专用多轮 tool-loop（15b015c 新增）
│   └── run_uds_inference.py              # UDS ledger 直推
├── engine/orchestrator.py                # 多 stage 编排（:304 消费 QWEN3_OMNI_THINKER_ONLY；漏 env 崩这里）
├── model_executor/models/qwen3_omni/pipeline.py  # pipeline 拓扑选择（:77 thinker-only 单 stage）
├── core/sched/omni_ar_scheduler.py       # AR 调度；action_token_ids 处理（:458-501）
└── deploy/qwen3_omni_duplex.yaml         # 基础 deploy config（max_sessions: 2 ⚠️ 必须被 Omni-Bench config 覆盖成 128）
```

### 9.3 runtime.draft.yaml（服务端行为配置，当前值）

路径 `vllm_omni/experimental/fullduplex/qwen3_omni/runtime.draft.yaml`，**全局生效**：

| 字段 | 当前值 | 含义 |
|---|---|---|
| `format_policy.chunk.duration_sec` | 0.24 | chunk 时长 |
| `audio_input.placeholders_per_chunk` | 3 | 每 chunk 音频 token 数（13Hz×0.24s） |
| `vision_input.frame_every_n_chunks` | 4 | 每 4 chunk 取 1 帧（0.96s/帧） |
| `vision_input.max_placeholders_per_frame` | **100** | 每帧 vision token 预算（两层压缩第二层；ONBOARDING 的"待回滚 8000"已回落 100；已验证甜点是 3000 配 IMAGE_MAX_DIMENSION=1568） |
| `listen_token` | `<\|listen\|>` | 静默 chunk 的 decode 目标（模型输出它 = 这个 chunk 没说话） |
| `generation.stage0.max_tokens_per_chunk` | 1 | 每 chunk 只 decode 1 个 agent token（流式解码核心） |
| `sequence.context_chunk_length / roll_batch` | 300 / 20 | 滚动上下文窗口（超了重新 prefill 截断） |
| `worker.thinker.persistent_kv_per_session` | true | KV 跨 chunk 常驻 |

⚠️ CHUNK_LAYOUT.md 承诺的"超长序列 evict 最旧 chunk"**未实现**（全仓 qwen3_omni 链路无 evict 代码）——KV 在两次 roll 之间线性累积，32k 序列约 11 分钟音频，~50 token/s。超长音频 benchmark（如 1h 级）要小心显存。

### 9.4 一次 duplex 推理在服务端发生了什么（评测视角）

```
WS 连接（?duplex=1）→ session.update（context_text=协议/指令文本）
→ 客户端每 0.24s 发一次 input_audio_buffer.append（PCM 16k）
→ data_plane 组 chunk（前缀 + 音频3token + [vision] + context_text + agent槽 + action槽）
→ worker logits hook 决定本轮 agent token：
     2-head 模式：state_head 出 listen/speak 决策；
     1-head 兜底：force_listen 掩码（payload 强制 listen 时只放行 <|listen|>）
→ agent_silent 判定（data_plane.py:736-778）：解码 token 全是 <|listen|> id = silent
→ runtime_bridge 打包 chunk.decision 事件回发 WS（:830-865）：
     {seq, input_speech, agent_silent, thinker_token_ids, action_token_ids, tts_cancelled}
→ 客户端 DualSilenceStopper 数连续 silent（1920ms 默认 / fdb_v3 4800ms）→ 主动断流停止
   （停止逻辑在【客户端】，服务端只管每 chunk 出决策；tail 4096 chunks 是客户端兜底）
```

**停止三要素**：adaptive_silence（客户端，1920/4800ms）+ max_tail_chunks（客户端兜底）+ max-sample-wall-s（跑批层硬超时）。服务端不主动停。

### 9.5 提速三件套（e6a3ff1，2026-09-15）

| 件 | 位置 | 作用 |
|---|---|---|
| engine clock | runtime_bridge.py:199 | 服务端全局时间网格 tick，同 tick 内并发 session 的 chunk 合并成一次 forward 批处理（config `duplex_session.engine_clock_ms` / env 覆盖） |
| tower 跨 session 批处理 | streaming_audio.py（deferral sink） | 音频 tower 不再每 token 跑，每 step 一次 batched forward（bitwise 一等已验证） |
| tower CUDA graph | tower_cuda_graph.py | static 形状整图捕获，规避 host sync |
| （附带）tokenizer 缓存 | — | session open 不再每次重解析 vocab（曾占 profile 主导） |

实测 7xTP1+CG 拓扑 4.1x（42→108 题/min）。**⚠️ engine clock 对 FD 时序指标（TOR/latency）的影响未做 A/B 验证**——跑时序敏感 benchmark 前先 smoke 对照，或用 eager 档 config（见 ONBOARDING 警告）。

### 9.6 vllm-mola 侧坑清单

1. **🔴 replay_realtime_events.py 有未提交改动（l30082962 工作区，+17/-3）**：给 thinker_token_ids 之外新增收集 `action_token_ids`（"streaming-SFT tool-call protocol"：tool-call 信封 `<|action_start|>JSON<|action_end|>` 流在 action span，agent-text span 为空）。**没有这个补丁，新协议 checkpoint 的 tool-call token 会被整个丢弃**，fdb_v3 只能靠裸 JSON 文本兜底。换机器重 clone 后必须先核对此补丁（或联系 l30082962 确认是否已推到某分支）。
2. **🟡 分支/副本混乱**：远端 demo_2heads(3b70b44) < duplex-lm-graph(15b015c)，ONBOARDING 记录过期；h30079575 可写副本在 demo_2heads_di（DI 方向）且有未提交改动。用代码前先 `git log -1` 确认。
3. **🟡 deploy 基础 config `max_sessions: 2`**：官方默认只容 2 并发——Omni-Bench config 必须显式覆盖（VCB 跑批 128）。忘覆盖 → capacity_exhausted 全 FAIL。
4. **🟡 runtime.draft.yaml 全局生效**：任何实验性改动（vision token 预算等）影响所有视觉 benchmark，实验完必须回滚；当前 max_placeholders_per_frame=100（甜点 3000 需配合 export 侧 1568）。
5. **🟢 根目录散落调试产物**：`talker-finish-marker-guard.patch`、`vllm-mola-duplex-debug-summary.md`、`qwen3_omni_duplex_eval_results.md` 等，是历史调试留档，非运行时依赖。
6. **🟢 runtime.draft.yaml 自带 `unresolved:` 节**：action_span 训练方式、talker stop token id 等上游遗留问题——官方自己标注未定。

---

## 10. VCB-Bench 48 子集 vs fdb_v3：同链路不同跑法

**骨架相同**：同一条 19 步托管链路（export → convert → duplex 推理 → import → score），同一个服务，同样的 conc=8 / tail=4096 / runmode=default，同样"先查 error_n + sample_id 去重"的看分纪律。

| 环节 | fdb_v3 | VCB 48 子集 |
|---|---|---|
| 推理客户端 | run_realtime_toolloop_batch.py（多轮 tool-loop，最多 4 轮，本地执行假 API 后新会话重放） | run_realtime_batch.py（**单轮** replay，说完即停） |
| 静音停判 | 4800ms（v3 音频无尾部静音） | 1920ms（默认） |
| import 模式 | --fd3（提取 tool_call 事件） | 无特殊 flag（普通 transcript） |
| adapter | FDBV3 单类 | VCBStreamingScorer + 48 薄子类（uds_streaming_vcb.py:317 起） |
| 评分 | 纯规则二值 pass_rate，无 judge | **47/48 子集要 phase2 judge**（Qwen3.8-27B :9001），四类见下 |
| 特有崩点 | --fd3 漏 = 静默 0 分 | phase1_output_dir 模型段 ≠ MODEL_NAME → scored=0 静默假分（run_streaming_full_eval.sh:677 guard 拦截）；per-model judge config 命名 `_phase2_judge_<MODEL_NAME>.yaml` 优先 |

**VCB 评分四类**（uds_streaming_vcb.py:62-64）：
1. **MCQ**（仅 discourse_comprehension）：字母匹配规则直算。⚠️ `:67 _deterministic_choice`：空输出按 sample_id 哈希**蒙一个 A/B/C/D 进分母**（match_type=`mcq_random_fallback`）——模型 listen 卡死时 DC 仍能蒙 ~25% 假分，看分按 match_type 拆开（DI 核查"DC 蒙题"的代码根源）。
2. **reference judge**（general_knowledge / basic_math / math / logical_reasoning）。
3. **ending judge**（story_continue）。
4. **其余 open 子集**：对比 gold 参考答案的 judge。

一句话：VCB = 普通流式问答 + 大量靠裁判模型；fdb_v3 = 多轮工具调用 + 纯规则。推理前置（转换/事件流/服务）完全共享。

---

## 11. 主框架全景 + 各 benchmark 适配对照（画图素材）

### 11.1 评测主框架分层（每层干什么）

```
┌─────────────────────────────────────────────────────────────────────┐
│ 入口层    omni_eval.cli --mode {smoke/standard/judge_only}          │
│           + --config omni_eval/configs/eval_<bench>.yaml            │
├─────────────────────────────────────────────────────────────────────┤
│ 调度层    OneClickRunner（omni_eval/runner.py）                     │
│           preflight → generate → evaluate → report 四拍             │
├──────────┬──────────────┬───────────────┬──────────────────────────┤
│ core 层   │ adapter 层    │ models 层     │ metrics 层               │
│ config    │ benchmarks/   │ OmniHTTPClient│ Evaluator 统一调度       │
│ 两个      │ 每 bench 一个 │ （非流式 HTTP │ 规则类直算；judge 类经   │
│ Registry  │ adapter：     │  客户端）     │ judge_manager（裁判模型  │
│ （Bench-  │ load_samples/ │ judge_manager │ 服务）；BFCL 依赖 vendored│
│ mark/     │ build_prompt/ │ （裁判模型    │ gorilla 子集             │
│ Metric）  │ parse_pred/   │  调度）       │                          │
│ Cache     │ evaluate_one  │              │                          │
│ Manager   │               │              │                          │
│（10 分量  │               │              │                          │
│ cache_key)│               │              │                          │
├──────────┴──────────────┴───────────────┴──────────────────────────┤
│ media 层   media_cache（obs:// → 本地复用）+ storage（obs/local/http）│
│ 报告层     metrics.json + report.md（reporting/reports）             │
├─────────────────────────────────────────────────────────────────────┤
│ 流式旁路：generate 不经 models 层！由外部 duplex 推理完成：          │
│   convert_v11_* → export_uds_v10_* → events → vllm-mola 推理        │
│   → output_events → tools/import_output_events.py 回灌 predictions  │
│   （写同一套 cache_key）→ 从 score 拍进入同一 Evaluator             │
│   ★ 两条链路共用 adapter / metric / cache / judge，只有推理前端不同  │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.2 接入一个 benchmark 要动的 6 处（通用适配面）

| # | 适配点 | 位置 | 干什么 | 谁需要 |
|---|---|---|---|---|
| 1 | 数据转换 | `scripts/convert_v11_<bench>.py` | 原始数据 → UDS v1.1 ledger（金标进 metadata；评分函数不做 IO 铁律） | 流式族必做；非流式不需要 |
| 2 | 媒体导出 | `scripts/export_uds_v10_<bench>.py` | v1.1→v1.0 + 媒体本地化 + 图片 resize（两层压缩第一层） | 有图片的流式族 |
| 3 | adapter | `omni_eval/benchmarks/<bench>.py` | 四协议：load_samples / build_prompt / parse_prediction / evaluate_one；注册 Registry | 所有 |
| 4 | metric | `omni_eval/metrics/` + evaluator 注册 | 规则类注册 MetricRegistry；judge 类接 judge_manager | 有新指标时 |
| 5 | eval yaml | `omni_eval/configs/eval_<bench>.yaml` | run.name / data_file / sampling ratio 1.0 / storage / judge_lifecycle | 所有 |
| 6 | 调度 case | `scripts/run_streaming_full_eval.sh` case 分支 | cfg / v11_ledger / import_bmark / conc / tail / pred_dir / judge_cfg | 流式族必做 |

外加 `scripts/streaming_profiles.yaml` 加条目（drift 检测用）+ `scripts/benchmark_meta.yaml`（看板元数据）。完整 SOP 见 ONBOARDING §7（含世界sense实战样例）。

### 11.3 各 benchmark 族适配对照表（差异只在推理前端 + 评分）

| 族 | 代表 | 转换 | adapter | 评分 | 推理客户端 | runmode |
|---|---|---|---|---|---|---|
| 非流式 turn-based | mmmu_pro / hallusionbench / omnibench | 不需要（直读原始） | 原生 adapter | 规则 + judge | OmniHTTPClient（HTTP） | — |
| 流式文本 | ifeval / bfcl_v3 / longbench_v2 duplex | convert_v11_* | uds_* | 规则 / judge | run_realtime_batch 单轮 | default |
| 流式图文/视频 | mmmu_pro_duplex / HB / mia / mm_ifeval / livesports / avut | + export（媒体 resize） | uds_* | 规则 / judge | run_realtime_batch 单轮 | default / video_only / video_audio |
| 流式 ASR/音乐 t68 | fleurs ×6 / wenet ×3 / gtzan / mmau / mmsu 等 | convert_v11_t68_audio（单点 bake） | uds_t68_audio 共享基类 | WER / MER / BLEU 纯规则 | run_realtime_batch 单轮 | default |
| VCB-Bench | uds_streaming_vcb_*（48 子集） | convert_v11_vcb | VCBStreamingScorer + 48 薄子类 | MCQ 规则（含蒙题兜底）/ reference / ending / open judge | run_realtime_batch 单轮 | default |
| FDB 官方 v1.0/v1.5 | fdb_*（9 子集） | convert_v11_fdb | uds_streaming_fdb（1 scorer + 9 薄类） | TOR 方向修正规则 + interrupt judge | run_realtime_batch 单轮 + --fd-timeline | default |
| FDB v3 | fdb_v3 | convert_v11_fdb3 | FDBV3 | pass_rate 纯规则 | **run_realtime_toolloop_batch 多轮** + --fd3 | default |
| 内部 fd 4 族 | fd_over / interrupt / pause / backchannel | fd_v11 专用链（不入 catalog） | fd_* + extract_fd_timeline | TOR 规则（uds_fd_turntaking）+ qwen38 judge | run_realtime_batch 单轮 | default |

读表要点：
- **adapter 分三派**：原生（非流式直读）/ UDS 族（从 v1.1 ledger 重建样本，流式通用）/ 单点 bake 特化族（t68 / VCB / fdb——一个文件装下整族，靠 metadata.subtask 或子类分派评分逻辑）。
- **runmode 只有三个值且进 cache_key**（default / video_only / video_audio）——import 和 score 两边不一致 = 全 miss，这是全套链路里杀伤力最大的坑。
- **judge 只有两族不需要**：纯规则族（fdb 全部 10 子集、t68 音频族、dc MCQ）和流式 11 核心里的规则 benchmark；VCB 48 里 47 个、内部 fd 的 interrupt、fdb v1.0/v1.5 的 interrupt 都要裁判服务。
- 同一条 19 步托管链路吃下所有流式族；非流式族不走它（OmniHTTPClient 一步到位）。

---

## 12. 一个样本的完整生命周期（从大看小，每步对应代码）

> 以 fdb_v3 样本 `ecommerce_01` 贯穿。六阶段：数据准备 → 托管调度 → 推理 → import 回灌 → score 出分 → 报告。

```
原始数据 ─S0─► v1.1 ledger ──► v1.0 ──► events（0.24s/片） ─S1─► 托管 19 步调度
                                                                    │
        ┌───────────────────────────────────────────────────────────┘
        ▼
   ─S2─► 推理（客户端 toolloop ⇄ 服务端 duplex，逐 chunk 决策） ──► output_events
        ▼
   ─S3─► import 回灌 predictions（写 cache_key）
        ▼
   ─S4─► score（Evaluator + adapter.evaluate_one + Accuracy） ──► metrics.json
        ▼
   ─S5─► 报告 report.md（judge 族另有 phase2）
```

### S0 数据准备（一次性，checkpoint 无关）

| 步 | 输入→输出 | 代码 | 关键逻辑 |
|---|---|---|---|
| S0.1 | 100 目录 → 79 唯一 scenario | `tools/fdb3/fdb3_scenarios.py:30 load_scenarios` | 同 id 重录目录去重（dedupe=True 保留排序第一个） |
| S0.2 | scenario → v1.1 ledger 行 | `scripts/convert_v11_fdb3.py:103 build_record` | `ensure_fdb_pcm16` 音频转 16k mono；gold 进 `metadata.fdb3_gold`；12 工具协议 bake 进 instruction（:74-100 模板） |
| S0.3 | v1.1 → v1.0 | `run_streaming_full_eval.sh:653-658`（fdb_* 分支）→ `export_uds_v10.py` | ledger uri 已本地化，直通复制 |
| S0.4 | v1.0 → events | vllm-mola `convert_ledger_realtime.py` | 每样本一个 jsonl：1 条 `session.update`（context_text=协议）+ N 条 `input_audio_buffer.append`（0.24s/片） |

### S1 托管调度（19 步生命周期）

| 步 | 代码 | 关键逻辑 |
|---|---|---|
| S1.1 冻结+预检 | `run_streaming_full_eval.sh:844 run_benchmark_managed` → `tools/streaming_run_manager.py --action init/preflight` | 冻结 expected_run=队列全集；preflight 19 字段不过 = 禁止 generation |
| S1.2 断点清理 | `run_generate`（:280）→ `scripts/streaming_skip_done.py` | 对照旧 output_events 删已完成（含 response.done）；guard-abort 样本标不重跑 |
| S1.3 客户端分派 | `run_generate` :303-311 | `fdb_v3` → `run_realtime_toolloop_batch.py`（附加 `--tokenizer-path/--max-rounds 4/--adaptive-silence-stop-ms 4800`）；其余 → `run_realtime_batch.py` |

### S2 推理（单样本，客户端 ⇄ 服务端）

| 步 | 代码 | 关键逻辑 |
|---|---|---|
| S2.1 tool-loop 外循环 | vllm-mola `run_realtime_toolloop_batch.py:202 run_toolloop_sample` | round 1 原样 replay；round n>1 `build_round_events`（:174）换 context_text = `round_context`（:187：协议+上轮回复+tool 结果）；无新调用或 4 轮即止 |
| S2.2 事件推流 | `replay_realtime_events.py`（replay 主函数） | WS 连 `?duplex=1`；session.update 发指令；逐片 append；`DualSilenceStopper.observe`（:43-62）：用户说完后**连续 8 片 agent_silent**（1920ms 默认/fdb_v3 4800ms）→ 停；tail 4096 片兜底 |
| S2.3 服务端每片决策 | data_plane.py:736-778（agent_silent 判定）→ runtime_bridge.py:830-865（chunk.decision 打包） | 解码 token 全是 `<\|listen\|>` id → silent=true；事件含 {seq, input_speech, agent_silent, thinker_token_ids, action_token_ids}；runtime.draft.yaml：每片 3 音频 token、agent 槽 1 token、action 槽 ≤10 |
| S2.4 解析+执行假 API | `run_realtime_toolloop_batch.py:82 decode_token_ids` → `:127 parse_tool_calls`（marker 正则；无命中 → `:92 _parse_bare_envelopes` 裸 JSON 兜底）→ `tools/fdb3/fdb3_mock_apis.py:128 MockAPIRegistry.call` | instant profile 零延迟；unknown function 返回 error 结构 |
| S2.5 落产物 | 同文件 :276-288 | 合并 output_logs/<sample>.jsonl（fdb3.round / chunk.decision / fdb3.tool_call / fdb3.sample_summary）；sidecar fdb3_predictions.jsonl（:358-368） |

### S3 import 回灌（output_events → predictions）

| 步 | 代码 | 关键逻辑 |
|---|---|---|
| S3.1 读事件+解 transcript | `tools/import_output_events.py` main（:454-456 `load_events_file`/`extract_transcript`） | tokenizer 解码 `<token:N>` 占位符（thinker_only 必传 --tokenizer） |
| S3.2 提取 tool-call | 同文件 :457-470（--fd3 分支） | `fdb3.tool_call` 事件 → `parsed_answer = {"tool_calls":[...], "n_rounds":N, "session_ok":bool}`；**漏 --fd3 → parsed_answer 空 → 静默 0 分** |
| S3.3 复现 cache_key | 同文件 :494-510 | adapter.build_prompt → prompt_hash/media_hash/generation_hash → `build_cache_key` 10 分量（core/cache.py）；runmode 用 `--run-mode default` |
| S3.4 预评分+落盘 | 同文件 :512-515（parse_prediction+evaluate_one）→ :517+ PredictionRecord | metadata = eval_result 全量展开（:536-539，保 match_type/scoring_status/error_type） |
| 产物 | `outputs/fdb_v3/<model>/<run.name>/predictions/fdb_v3.jsonl` | 每样本一行（crash resume 可能重复，见坑 3） |

### S4 score（Evaluator 出分）

| 步 | 代码 | 关键逻辑 |
|---|---|---|
| S4.1 入口 | `run_score`（run_streaming_full_eval.sh:403）→ `omni_eval.cli --mode standard` | 全 cache 命中，不推理 |
| S4.2 runner 主流程 | `omni_eval/runner.py:99 run` → `:518 run_benchmark` | resolve adapter（:520）→ `cache.load_predictions`（:533）→ `load_samples`（:535，从 v1.1 ledger 重建）→ `Sampler.sample`（:561，ratio 1.0 全量）→ generate 循环逐样本查 cache_key 命中 → `finalize_predictions` 去重（:673） |
| S4.3 Evaluator 调度 | `omni_eval/metrics/evaluator.py:281 evaluate` | `adapter.default_metrics()`=["accuracy"] → METRIC_REGISTRY → Accuracy |
| S4.4 单样本打分 | `omni_eval/benchmarks/uds_streaming_fdb3.py:82 FDBV3Scorer.evaluate_one` | payload 解析 → `evaluate_scenario_pass`（tools/fdb3/fdb3_eval.py:191：recall=1 AND precision=1 AND `exact_match_args` :68 全参对，$ 动态引用跳过）+ F1（:35） |
| S4.5 聚合 | `omni_eval/metrics/accuracy.py:13 compute` | valid = error 为空且 correct 非空的记录；accuracy = mean(correct is True)。**error（推理失败）不计入分母**；summary.json 的 effective_accuracy 才是 error 计 0 分口径 |
| S4.6 breakdown | evaluator.py（compute_breakdown） | 按 fdb3_domain / fdb3_difficulty / fdb3_num_tools 分桶 |
| 产物 | `metrics/fdb_v3.json`（权威分数）+ `reports/report.md`（runner.py:1173 _generate_report → ReportGenerator.generate） | display_score_percent 等字段在 metrics json |

### S5（可选）phase2 judge

fdb_v3 无此步。VCB 47/48 子集走 `run_judge`（run_streaming_full_eval.sh:431+）→ `omni_eval.cli --mode judge_only` → judge_manager 调 :9001 Qwen3.8-27B；phase1_output_dir 必须指向当次 run 的模型段（指错 = scored=0 静默假分，:677 guard）。

### L3 样本贯穿：ecommerce_01 在每一阶段长什么样

| 阶段 | 数据形态 |
|---|---|
| 原始 | `/cache/Full-Duplex-Bench-data/v3.0/fdb_v3_data_released/ecommerce_01_65e8.../`：input.wav（45.8s 口述查订单）+ metadata.json（id=ecommerce_01，expected_tool_calls=[track_order(order_id=...)]） |
| ledger 行 | `{"id": "fdb_v3_ecommerce_01", "messages": [{user: [audio 45.8s, instruction(12 工具协议)]}], "metadata": {"fdb3_gold": {...}}}` |
| events 文件 | 1 条 session.update（context_text=协议）+ 191 条 input_audio_buffer.append（45.8/0.24≈191 片） |
| 推理输出 | 191 条 chunk.decision **全部 agent_silent=true**（模型全程 listen）→ DualSilenceStopper 凑满 8 片停 → fdb3.tool_call 零条 → sample_summary {n_rounds: 1, ok: true} |
| prediction 行 | parsed_answer=`{"tool_calls":[],"n_rounds":1,...}`；correct=False；metadata.fdb3_failure_reason="Missing tools: ['track_order']"、fdb3_recall=0.0、fdb3_tool_f1=0.0 |
| metrics 贡献 | 不在 valid 正确集合 → accuracy 分子不+1；分母 79（全量 valid）；全量快照 PASS 1/79=1.27%（终局报告 0/79，见坑 3） |

> 非流式链路（round path）的对应关系：S0-S2 换成「OmniHTTPClient 一次 HTTP POST（runner.py 内 generate 循环）→ 直接产 PredictionRecord」，S3 的 import 步不存在，S4 之后完全相同。
