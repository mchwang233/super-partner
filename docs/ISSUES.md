# Super Partner 已知问题

记录日期：2026-10-21。来源：当天对 API 模型工具调用的实测（与 Claude Code 对比）、数据库里的运行记录和事件，以及对当前代码的核对。
没有逐行通读全部代码，只靠通读才能发现的问题不在这里。修好一条，就把它移到最后的「已修复」，并写上提交号。

状态：**存在**＝已在当前代码或实测中确认；**待验证**＝已有修复，但还没在完整的真实任务中跑过。

## 一、严重：安全，或直接导致任务失败

第 1～3 条已于 2026-10-21 修复，见文末「已修复」。还剩下的部分：

### 1b. Codex（和 Claude Code）执行步骤时能读到私钥、平台数据和别的任务；/tmp 是全机共用的（存在，暂不改）
- 2026-10-21 实测 Codex 的步骤沙箱：能读 `~/.ssh`、平台数据库和其他任务的目录；`/tmp` 是整台机器共用的真实 `/tmp`。Codex 自己的沙箱只限制写，不限制读。
- 方向（用户暂不同意）：让 Codex 跑在平台自己的沙箱里（隐藏主目录和平台数据，`/tmp` 每次独立），并关掉 Codex 自己的沙箱和命令审核。

### 1c. Codex 拒绝带通配符的 `rm`（存在，影响小）
- 2026-10-21 实测：Codex 执行步骤时，内置的危险命令检查（它的 exec policy）会拒绝 `rm -f` 这类命令，即使在它自己的沙箱里。现在平台在 Codex 调用期间往任务目录里临时放一个允许 `rm` 的规则文件（`cli_channels.codex_rules`），调用结束就删掉；`rm` 仍然受 Codex 沙箱限制，工作目录外照样不能删。实测放行：`rm -f a.o b.o`、`rm -rf build`、和其他命令连写的 `rm`；仍被拒：`rm -f *.o`、`rm -f $(...)`（Codex 无法确定会删什么）。步骤提示词里告诉 CLI 模型改用写明文件名或 `find . -name '*.o' -delete`。测试：`test_codex_may_delete_files_in_its_folder_and_the_rule_is_gone_after`。

### 1d. 执行前检查只检查 API 模型的沙箱（存在）
- Codex、Claude Code 执行步骤时用的是它们自己的沙箱，环境（PATH、能读的目录、网络）和平台的沙箱不同，执行前检查没有覆盖。

### 1e. 任务目录在平台仓库里（等目录）
- 默认的 `workspaces/` 在平台仓库里，Codex 和 Claude Code 执行步骤时会读到 Super Partner 自己的 `AGENTS.md`、`CLAUDE.md`。等管理员在 `/projects` 下建好目录后，用设置 `SUPER_PARTNER_WORKSPACE_ROOT` 切过去（`scripts/service.sh` 目前固定传的是仓库里的 `workspaces/`，要一起改）。新任务的目录名已经改成可读的英文名（`20261021-1441-async-fifo-uvm`）。

### 3b. Anthropic 写入缓存的费用按普通输入计算（存在，影响小）
- Anthropic 写入缓存（`cache_creation_input_tokens`）按输入价的 1.25 倍收费，`budget.token_cost` 按 1 倍计算，这部分费用会略微少算。

### 18. 确认任务时用的是拆分步骤之前的估算（存在）
- 现象：2026-10-21 13:05 的异步 FIFO 任务，确认时的估算是 $0.39～0.47；大项拆成 33 个步骤后重新估算为 $4.46～5.47，远超 $2.00 的预算，但任务已经开始执行，花到 $1.58 时因为预算不足暂停。
- 原因：自上而下规划时，确认前只有大项，大项的费用按 `size` 和 `routing.part_estimate` 粗估，远低于拆分后逐步计算的结果。
- 方向：用复盘里的实际数据校准大项的粗估，或者拆分后估算超出预算时先停下来问用户。

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

第 11～13 条已于 2026-10-21 修复，见文末「已修复」。还剩下的部分：

### 11b. 问题按意思归类还做不到（存在）
- 复盘不调用模型，只按记录归类：平台修复按改了哪个模块，环境问题按失败命令里的程序名。所以能看出「沙箱（agent）在两个任务里修过、修完又出问题」，但认不出几次修复其实都是「新思 EDA 环境」这一件事；关键字识别不出来时归成 `general`。
- 方向：任务结束时用最便宜的模型给每个问题起一个简短的名字，并尽量沿用已有的名字。
- 结果页现有「复盘与改进」：可生成模型结论、建立并验证改进项，查看后续应用和效果对比。
  全局重复问题统计仍通过接口 `/api/retrospectives/recurring` 查看；原统计分类仍按模块／程序名。

## 四、体验和其他

第 14～16 条已于 2026-10-21 处理，见文末「已修复」。

### 17. Claude Code 在平台中是停用的（设置，不是缺陷）
- `cli:claude` 的 `enabled=false`，能用的会员 CLI 只有 `cli:codex`。这样真实任务里没法拿 Claude Code 和 API 模型对比。

## 建议的处理顺序

1. 第 1b 条：把会员 CLI 的沙箱也隔离起来，补完第 1 条。
2. 第 5 条：保留 Claude Code 的轮数和费用。
3. 第 11b 条：用模型给问题按意思归类，加前端页面。
4. 跑一次完整的真实任务（例如异步 FIFO 加覆盖率），验证第 9、10 条。

## 已修复

- 第 25 条（2026-10-21）：步骤没有可用的 skill，每一步都从 `vcs -help` 开始自学工具（一个步骤前 20 轮都在读手册、搭小例子）；执行前检查明明跑通了 VCS 加覆盖率加 URG 的完整流程，却只记下「通过」。现在：skill 库（`skills.py`，通用的在 `skills/steps/`，本机的在数据目录 `skills/`）；执行前检查通过后，为任务要用的每个专业工具准备本机 skill（没有就让模型在步骤沙箱里实测后写出，已有就复用，`preflight._prepare_skills`）；每个步骤自动带上和它提到的工具相关的 skill。第一个通用 skill：`skills/steps/synopsys-vcs`（命令已在步骤沙箱实测）。测试：`tests/test_skills.py`。已补充：结项复盘会提取新经验，实际验证后保存为独立 Skill 版本，按适用范围注入后续任务，并记录效果；见 `docs/design/continuous-improvement.md`。
- 第 26 条：同时运行的步骤数默认不再限制（`max_parallel_nodes=0`），改同一批文件的步骤仍会排队。
- 第 24 条（2026-10-21）：一个困难步骤卡住整个任务。实测 FIFO 任务里，最先开始的 RTL 步骤分给了 GLM 5.3（编码分只比 DeepSeek Flash 高 1.6 分，但慢约 8 倍、贵 20 多倍），用 `max` 强度跑了 30 多分钟，后面 6 个步骤全在等它。现在：困难步骤在比最高分低不超过 `hard_score_margin`（默认 2）分的模型里选最便宜的（`routing.close_to_best`）；有别的步骤在等的步骤，用时超过预期（预计轮数 × `minutes_per_turn` 0.5 分钟）的 `critical_overrun_factor`（默认 2）倍时，由主管决定继续，还是停下换更快的模型（`Review._critical_check`，事件 `node.critical_overrun_check`）。测试：`tests/test_critical_path.py`。
- 第 21 条（2026-10-21）：沙箱里的模型不知道自己在什么环境里，而且环境本身有两处不对。
  - 开联网时，pasta 让步骤在沙箱里变成了 root（`uid=0`），模型会以为自己有 root 权限。现在会把身份映射回用户本人（`agent.maps_user`，在不能映射的嵌套环境里照旧）。执行前检查的沙箱自检也会检查这一点。测试：`test_a_step_allowed_the_internet_is_still_the_user_not_root`。
  - 诊断修复环境的 `/dev` 是最小版本，没有 `/dev/net/tun`，修复模型没法启动开联网的步骤沙箱来验证，于是误判平台坏了（2026-10-21 一次修错方向的平台自我修复就是这样来的）。现在修复环境用真实的 `/dev`。测试：`test_repairs_have_the_real_dev_so_they_can_start_a_steps_sandbox`。
  - 每种角色都如实知道自己在哪里：API 步骤（`AGENT_INSTRUCTIONS`：只读系统、主目录隐藏、不是 root、网络、缺东西时如实说明）；CLI 步骤（`quality.cli_environment`，包括 Codex 的命令审核）；修复（`HOST_INSTRUCTIONS`：不在步骤的沙箱里）；规划（`planner.WORK_RULES` 如实描述沙箱，任务允许联网时告诉主管，拆分大项时按当时的设置说明）。测试：`test_every_model_is_told_where_it_runs`。
- 第 22 条：Codex 用平台自己的设置运行（`cli_channels.codex_settings`）：主管 `max` 强度（`lead_reasoning_effort`）和这台机器设置的快速档（`.env` 里的 `SUPER_PARTNER_CODEX_LEAD_SERVICE_TIER=fast`），步骤按难度、诊断 `high`；不加载用户的插件、应用和 MCP 服务。实测一次简单调用从 48 秒、16,429 token 降到 25 秒、1,568 token。主管的工作目录移到仓库外（`~/.cache/super-partner/leads/`，可读的名字），不再读到 Super Partner 自己的 `AGENTS.md`。测试：`test_codex_runs_with_the_platforms_effort_and_without_the_users_extras`、`tests/test_folders.py`。
- 第 23 条：执行前检查把主管写的检查命令截断到 300 字符，把一条长检查命令截成了坏命令。现在 4000 字符以内原样保留，更长的整条丢弃。测试：`test_the_outline_names_the_commands_that_check_the_tools`。
- 第 19 条（2026-10-21 新发现）：任务允许联网时，在服务里运行的步骤，Bash 对任何命令都返回「没有输出」。原因：uvicorn 用 uvloop 运行，它给子进程的输出管道其实是 socket，套在沙箱外面提供网络的 pasta 会把这种输出丢掉；在命令行里用 Python 自带的事件循环时复现不出来。13:05 的 FIFO 任务 154 次命令调用全部没有输出，模型只好先写文件再读，轮数翻倍。现在 Bash 把输出写进临时文件再读回（`agent.Workspace.bash`）。测试：`test_command_output_arrives_under_uvloop_as_in_the_service`。
- 第 20 条：执行前检查（新模块 `preflight.py`）。第一个步骤开始前，按任务的实际设置开一个真实的步骤沙箱，检查：沙箱本身（输出、错误输出、退出码、写工作目录和 /tmp）；主管列出的工具检查命令（`TaskPlan.requires`）；步骤检查命令用到的程序（只警告）；允许联网时能否上网（只警告）；每个 API 模型能否调用（只警告）。会拦住任务的检查没通过时，先自动修一次环境，修完重新检查；还不行就暂停并列出缺什么，用户点「继续」后照常执行。检查通过一次之后，继续任务时不再重复。用设置 `preflight` 可以关闭。在 13:05 的 FIFO 任务上实测：沙箱、vcs、make 通过，并发现沙箱里找不到 `urg`（警告）。测试：`tests/test_preflight.py`。
- 第 11 条：任务结束（成功、失败、未完成、取消）时自动复盘（新模块 `retrospect.py`，数据库表 `retrospectives`）：每步估算和实际的费用、轮数，以及遇到的问题，按「类别:关键字」归类（估算偏低、轮数超出、模型不可用、被判定卡住、暂停等用户、环境问题、平台修复）。跨任务统计每类问题出现在几个任务里，修过之后又出现的标成「修复后复发」。诊断修复时，提示词里会带上同类问题以前的情况和当时的修复报告。旧任务在第一次查询时补算。接口：`/api/retrospectives/recurring`、`/api/tasks/{id}/retrospective`。在当前数据上，它认出了沙箱（`agent`）和环境配置（`environment`）在两个任务里各修过一次、修完又出问题。测试：`tests/test_retrospect.py`。
- 第 12 条：尝试无论怎样结束（正常结束、取消、换模型），都保留轮数、工具调用和完整日志的路径（`Executor._take_log`）。测试：`test_a_cancelled_attempt_keeps_its_turns_tool_calls_and_trace`。
- 第 13 条：每次尝试的每一次工具调用都完整写进数据目录下的日志文件（`traces/<任务>/<步骤>-<第几次>.jsonl`，每条输出最多 20 万字符），数据库里仍只存摘要；诊断提示词会给出这个文件的路径；删除任务时一并删除。步骤的沙箱看不到这个目录。测试：`test_the_full_trace_keeps_every_call_and_its_whole_output`、`test_a_diagnosis_is_told_where_the_full_trace_is`。
- 第 14 条：之前的提交已经修好，不用再改代码：诊断判断阻塞但不需要用户提供什么时，接着修复、重试（`diagnosing.py`）；额度用完时直接换模型；有主管时失败先交给主管决定（`62bdf7f`）。都有测试覆盖（`test_a_blocked_suggestion_without_a_human_dependency_keeps_working`、`test_diagnosed_quota_switches_the_execution_model_instead_of_pausing` 等）。
- 第 15 条：同一用户的多个进程排队读钥匙串（文件锁），超时后用两倍时限再试一次。实测 6 个进程同时启动，全部拿到 key，每个等 0.3～1.7 秒。测试：`test_a_slow_keyring_is_asked_again_with_more_time`、`test_processes_read_the_keyring_one_after_another`。
- 第 16 条：Markdown 渲染器改为页面加载后再单独加载（`markdown-render.tsx`），加载完之前先显示纯文本；主包从 518 KB 降到 364 KB，构建不再警告。测试：`frontend/tests/bundle.test.mjs`。
- 第 4 条：估算偏低。主要原因其实是轮数估少了（第 10 条）、加上当时缓存读取还没按缓存价计费（`653be14`）；按实际轮数算，每轮参数只偏低 10%～25%。已按 2026-10-21 的实测重新校准 `routing.LOOP_*`（每轮增长 1300 token，max 强度每轮输出 1500 token），估算是实测的 0.9～1.13 倍。测试：`test_an_api_steps_estimate_matches_what_measured_runs_cost`。
- 第 6 条：思考强度现在发给每家服务（`providers.reasoning_fields`）：OpenAI 兼容服务用 `reasoning_effort`（GLM 只用 low、high、max，OpenAI 最高 xhigh），Anthropic 用 `output_config.effort`，Gemini 用 `thinkingLevel`。某个模型不认这个参数时，去掉重发，以后不再发给它。评审按步骤难度设置强度，进度检查用 low。实测：GLM 5.3 做同一道题从 530 秒、23 轮、$0.25 降到 185 秒、10 轮、$0.089，结果同样正确。测试：`test_each_provider_is_asked_for_the_effort_in_its_own_fields`、`test_a_model_that_rejects_the_effort_answers_without_it_from_then_on`。
- 第 7 条：影响不大，已加开关，默认关闭。步骤一个接一个地跑在同一个模型上没有问题：服务出错时，当前步骤本来就会换模型。只有几个步骤同时跑在同一家服务上、这家服务又繁忙或限流时，才会一起失败、一起从头重做；平台同时最多跑 3 个步骤，而分散开会把日常步骤交给更弱的模型（按当前模型目录是 MiniMax M3）。开关：`max_steps_per_provider` 设为 N 时，同一个任务里每家 API 服务同时最多跑 N 个步骤，再多的交给别家达到门槛、估算费用不超过原来 `spread_cost_factor`（默认 3）倍的模型（`Executor._spread`、`routing.spread_to`）。等以后有几家实力相近的服务可用时再打开。测试：`tests/test_spread.py`。
- 第 8 条：评审员和进度检查员原来按「可靠性」一项分数挑，结果挑中综合能力最弱的 MiniMax M3。现在挑综合分（`CapabilityScore.overall`）不低于被检查模型的模型里最便宜的一个，没有的话挑最强的。按当前模型目录，DeepSeek Flash 的结果由 GLM 5.3 评审（单价约为 M3 的 4 倍）。测试：`test_a_result_is_checked_by_a_model_no_weaker_than_the_one_that_made_it`。
- 第 1 条：API 模型的步骤沙箱能读到其他任务的目录和平台数据库。现在会隐藏平台数据目录、所有任务目录和数据库文件，只把当前任务目录和共享工具目录挂回来；任务就在平台仓库里做时同样有效。测试：`test_a_step_cannot_read_other_tasks_or_the_platform_data`、`test_a_task_in_the_platforms_own_folder_still_cannot_read_its_data`。Claude Code 和 Codex 的部分见 1b。
- 第 2 条：所有 API 请求改为流式（新模块 `streaming.py`），只有在 `api_reply_deadline` 内一点数据都没收到才会停掉。实测：GLM 5.3 两道题都做完并通过了隐藏测试，之前它们都卡在「5 分钟没有回复」。测试：`tests/test_streaming.py`。
- 第 3 条：Anthropic 的工具循环给系统提示词和每轮最新的一条消息加上缓存标记（`providers._cache_point`）。测试：`test_anthropic_tool_loops_read_the_conversation_from_the_prompt_cache`。
- 文件工具（Read、Write、Edit 等）用不了 Bash 的 `/tmp`，提示词又说工作目录外的文件「只读」，和实际不符。`bbaaf29`。
- MiniMax 的回答里夹着 `<think>…</think>` 思考内容，混进结果、评审和 JSON。`bbaaf29`。
