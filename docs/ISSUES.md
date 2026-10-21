# Super Partner 已知问题

记录日期：2026-10-21。来源：当天对 API 模型工具调用的实测（与 Claude Code 对比）、数据库里的运行记录和事件，以及对当前代码的核对。
没有逐行通读全部代码，只靠通读才能发现的问题不在这里。修好一条，就把它移到最后的「已修复」，并写上提交号。

状态：**存在**＝已在当前代码或实测中确认；**待验证**＝已有修复，但还没在完整的真实任务中跑过。

## 一、严重：安全，或直接导致任务失败

第 1～3 条已于 2026-10-21 修复，见文末「已修复」。还剩下的部分：

### 1b. Claude Code 和 Codex 的沙箱仍能读到其他任务和平台数据（存在）
- 第 1 条只修了 API 模型的沙箱（`agent.py`）。会员 CLI 用的是它们自带的沙箱（`cli_channels.py` 的 `CLAUDE_SANDBOX_SETTINGS`，Codex 的 `workspace-write`），默认能读整个磁盘。
- 方向：给这两种 CLI 设置禁止读取平台数据目录和其他任务目录。

### 3b. Anthropic 写入缓存的费用按普通输入计算（存在，影响小）
- Anthropic 写入缓存（`cache_creation_input_tokens`）按输入价的 1.25 倍收费，`budget.token_cost` 按 1 倍计算，这部分费用会略微少算。

## 二、调度和费用

第 4、6、8 条已于 2026-10-21 修复，第 7 条加了开关、默认关闭，见文末「已修复」。

### 5. 看不到 Claude Code 的用量（存在）
- 原因：`claude -p --output-format json` 会返回 `num_turns`、`total_cost_usd` 和 `usage`，但 `providers.py` 处理 CLI 的分支一律记成 `input_tokens=0, output_tokens=0`。
- 影响：算不出 Claude Code 步骤的费用和轮数；「超出预期轮数就问主管」的检查对 CLI 不起作用；只能等报出 "session limit" 之后才换模型（2026-10-15 的诊断因此连续 5 次先失败、再换到 Codex）。

### 9. 计划拆得太细、基本串行，费用超出预算（已修，待验证）
- 现象：2026-10-20 的 FIFO 任务拆成 24 步，关键路径长达 18 步：每个大项内部一个接一个排下去（验证平台一项是 7 步连成一条线），断言和验证平台两个大项都要等 RTL 大项的最后一步做完。55 分钟只完成 7 步，同时最多跑 2 步；预算 $2.00 已花 $1.87，还有 15 步没开始。大项的预计规模加起来是 12 步，实际被拆成了 24 步。
- 已修：`02a49f3`（大项按接口约定直接开工；排成单线的拆分会被退回）；2026-10-21 起改为按依赖关系计算最长链（`outline.longest_chain`），任意顺序、旁边夹着别的任务也能发现；拆出的步数明显超过大项预计规模（超过 `max(规模+2, 规模×1.5)`）时退回，要求合并。
- 待验证：还没跑过完整的真实任务。

### 10. 步骤轮数大大超出预期（已修，待验证）
- 现象：同一任务每步实际 15～49 轮，预期（`progress.EXPECTED_TURNS`）为 10～15 轮。
- 原因（查看工具调用日志）：约一半轮数用来绕过当时的一个缺陷：命令的输出回不到模型，模型只好把输出写进文件、下一轮再读；其余轮数花在到处找 VCS 装在哪里、最后反复清理文件。
- 已修：`43f57a7`、`05440b4`（命令输出直接返回，用 Claude Code 的工具；每条命令前自动加载 EDA 环境），`0d89df2`（同一轮里批量调用工具、只检查一次、不清理）。2026-10-21 的小题实测都在 10～13 轮内完成。
- 待验证：大任务还没验证过。

## 三、回溯和记录

### 11. 没有复盘，问题不会和以前的对上（存在）
- 现状：跨任务的记忆只有最近 5 条修复记录（`db.shared_repairs(limit=5)`），在诊断时放进提示词。
- 现象：新思 EDA 沙箱的问题 2026-10-15（SSH 凭据）、2026-10-20 06:40（License 守护进程）、2026-10-20 10:07（`.edarc` 环境）各修过一次，每次都是从头诊断。估算偏差每次都一样，从来不拿实际值回头校准。
- 方向：任务结束后自动复盘（每步估算和实际的差距、失败原因、模型不可用情况、诊断和修复）；把问题归类、跨任务计数，并标出「修复后复发」；诊断和主管做决定时能看到同类的历史问题。

### 12. 取消任务时丢了轮数记录（存在）
- 现象：FIFO 任务被取消时，步骤 `e7bc855f` 已经花了 $0.44，尝试记录里的轮数却是 0。

### 13. 工具调用日志有上限，内容被截短（存在）
- 原因：`providers.py` 每次尝试最多记 `MAX_LOGGED_CALLS` 条，每条输出只保留前 `LOGGED_OUTPUT` 个字符。
- 影响：长步骤的后半段，事后查不到它具体做了什么。

## 四、体验和其他

### 14. 诊断没结果时，任务停下来一直等人（待验证）
- 现象：2026-10-14 至 15 日的服务器检查任务，同一步骤 75 分钟内诊断 5 次都没结果（`diagnosis_unresolved`），每次都停下来等用户批准，其中一次空等了 9 个多小时。起因是 Claude Code 的 session limit。
- 已有修复：之后加入的主管决策机制（`62bdf7f` 等）可能已经改善，还没验证。

### 15. 多个进程同时读系统钥匙串会超时（存在）
- 原因：`secrets.hydrate_provider_keys` 的超时是 10 秒，几个进程同时读钥匙串时会超时，结果拿不到 API key。
- 影响：平台只跑一个进程时不受影响；以后多进程运行，或在重启时撞上，就会出现。

### 16. 前端打包超过 500 KB，没有按页面拆分（存在）
- `npm run build` 警告单个包超过 500 KB，首次加载偏慢。

### 17. Claude Code 在平台中是停用的（设置，不是缺陷）
- `cli:claude` 的 `enabled=false`，能用的会员 CLI 只有 `cli:codex`。这样真实任务里没法拿 Claude Code 和 API 模型对比。

## 建议的处理顺序

1. 第 1b 条：把会员 CLI 的沙箱也隔离起来，补完第 1 条。
2. 第 5 条：保留 Claude Code 的轮数和费用。
3. 第 11～13 条：任务复盘，以及补齐回头追查需要的记录。
4. 跑一次完整的真实任务（例如异步 FIFO 加覆盖率），验证第 9、10、14 条。

## 已修复

- 第 4 条：估算偏低。主要原因其实是轮数估少了（第 10 条）、加上当时缓存读取还没按缓存价计费（`653be14`）；按实际轮数算，每轮参数只偏低 10%～25%。已按 2026-10-21 的实测重新校准 `routing.LOOP_*`（每轮增长 1300 token，max 强度每轮输出 1500 token），估算是实测的 0.9～1.13 倍。测试：`test_an_api_steps_estimate_matches_what_measured_runs_cost`。
- 第 6 条：思考强度现在发给每家服务（`providers.reasoning_fields`）：OpenAI 兼容服务用 `reasoning_effort`（GLM 只用 low、high、max，OpenAI 最高 xhigh），Anthropic 用 `output_config.effort`，Gemini 用 `thinkingLevel`。某个模型不认这个参数时，去掉重发，以后不再发给它。评审按步骤难度设置强度，进度检查用 low。实测：GLM 5.3 做同一道题从 530 秒、23 轮、$0.25 降到 185 秒、10 轮、$0.089，结果同样正确。测试：`test_each_provider_is_asked_for_the_effort_in_its_own_fields`、`test_a_model_that_rejects_the_effort_answers_without_it_from_then_on`。
- 第 7 条：影响不大，已加开关，默认关闭。步骤一个接一个地跑在同一个模型上没有问题：服务出错时，当前步骤本来就会换模型。只有几个步骤同时跑在同一家服务上、这家服务又繁忙或限流时，才会一起失败、一起从头重做；平台同时最多跑 3 个步骤，而分散开会把日常步骤交给更弱的模型（按当前模型目录是 MiniMax M3）。开关：`max_steps_per_provider` 设为 N 时，同一个任务里每家 API 服务同时最多跑 N 个步骤，再多的交给别家达到门槛、估算费用不超过原来 `spread_cost_factor`（默认 3）倍的模型（`Executor._spread`、`routing.spread_to`）。等以后有几家实力相近的服务可用时再打开。测试：`tests/test_spread.py`。
- 第 8 条：评审员和进度检查员原来按「可靠性」一项分数挑，结果挑中综合能力最弱的 MiniMax M3。现在挑综合分（`CapabilityScore.overall`）不低于被检查模型的模型里最便宜的一个，没有的话挑最强的。按当前模型目录，DeepSeek Flash 的结果由 GLM 5.3 评审（单价约为 M3 的 4 倍）。测试：`test_a_result_is_checked_by_a_model_no_weaker_than_the_one_that_made_it`。
- 第 1 条：API 模型的步骤沙箱能读到其他任务的目录和平台数据库。现在会隐藏平台数据目录、所有任务目录和数据库文件，只把当前任务目录和共享工具目录挂回来；任务就在平台仓库里做时同样有效。测试：`test_a_step_cannot_read_other_tasks_or_the_platform_data`、`test_a_task_in_the_platforms_own_folder_still_cannot_read_its_data`。Claude Code 和 Codex 的部分见 1b。
- 第 2 条：所有 API 请求改为流式（新模块 `streaming.py`），只有在 `api_reply_deadline` 内一点数据都没收到才会停掉。实测：GLM 5.3 两道题都做完并通过了隐藏测试，之前它们都卡在「5 分钟没有回复」。测试：`tests/test_streaming.py`。
- 第 3 条：Anthropic 的工具循环给系统提示词和每轮最新的一条消息加上缓存标记（`providers._cache_point`）。测试：`test_anthropic_tool_loops_read_the_conversation_from_the_prompt_cache`。
- 文件工具（Read、Write、Edit 等）用不了 Bash 的 `/tmp`，提示词又说工作目录外的文件「只读」，和实际不符。`bbaaf29`。
- MiniMax 的回答里夹着 `<think>…</think>` 思考内容，混进结果、评审和 JSON。`bbaaf29`。
