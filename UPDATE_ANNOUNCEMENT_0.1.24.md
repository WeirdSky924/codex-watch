# Codex Goal Watchdog 0.1.24 更新公告

发布日期：2026-09-01

发布状态：待发布，本文为发布前公告草稿

Codex Goal Watchdog 0.1.24 重新明确了 watchdog 的职责边界：Codex CLI
负责上下文窗口和原生压缩，watchdog 负责监控终端级致命故障、保存恢复状态，
并在可恢复的情况下继续原有 Goal。

## 本次更新

### 1. 健康指标改为遥测，不再主动接管线程

以下指标仍会被本地 monitor 记录，用于诊断和 handoff，但不会再自动触发
`compact`、重启或新建 thread：

- rollout 文件大小
- 无进展 token
- 长时间没有 rollout 事件
- 连续重复的 assistant 内容
- 连续重复的 shell 命令

上下文窗口耗尽、原生 compact 次数和上下文上限均由 Codex CLI 自己处理。
watchdog 不会因为一个 thread compact 过几次就强制轮换 thread。

旧启动脚本仍可传入 `--thread-max-compactions` 和
`--thread-max-context-tokens`，但这两个参数现在仅为兼容保留，不会产生
watchdog 行为。

### 2. 恢复流程状态持久化

恢复状态不再只存在于当前 monitor 进程或 tmux 选项中。watchdog 现在会持久化：

- 当前恢复阶段
- 下一次允许恢复的时间
- 最近一次恢复原因
- 已使用的恢复次数

因此 guardian 接管、monitor 重挂载、tmux 重建或短暂断联不会意外清零恢复
计数，也不会让两个 worker 同时执行同一条恢复链。

### 3. 冷静期内不丢弃新的致命错误

默认策略保持为：

- 第一次致命错误：立即尝试恢复
- 后续恢复失败：默认等待 300 秒后再尝试
- `--max-recoveries 0`：不限制恢复次数

冷静期由串行恢复步骤统一执行。冷静期内再次观察到新的 fatal incident 时，
watchdog 会保留并记录它，待当前恢复阶段完成后按同一恢复策略处理，不会因为
正在等待就静默丢弃。

### 4. 更可靠的 session/thread 绑定

持久化的 watchdog session binding 现在是固定 thread 的主要来源，tmux 选项
只作为兼容回退。这样可以更可靠地处理：

- tmux 会话被重建
- Codex 执行 `/clear` 后 thread ID 发生变化
- guardian 或 monitor 重新连接
- tmux 中残留旧 thread 选项

启动时如果当前目录与已有 binding 不一致，watchdog 会拒绝含糊恢复，并提示
用户回到原目录、使用新的 session 名称，或明确使用 `--new` 替换绑定。

### 5. 减少重复日志和重复恢复竞争

monitor 与 guardian 对同一恢复锁竞争的重复日志会聚合为首条记录和汇总计数，
避免长时间故障期间持续刷屏。恢复仍由单一串行状态机执行。

默认 rollout 遥测上限同步为 512 MiB。该数值只用于本地诊断观测，不是上下文
限制，也不会单独触发 thread 轮换。

### 6. 修复 tmux 保留但 Codex 已退出的恢复边界

如果用户主动 `/quit`、终端恢复过程中 Codex 进程退出，或者服务器重启后 tmux
只留下 Shell，下一次显式运行 `codex-watch` 会先检查 durable binding。确认
session 绑定有效且 pane 是空闲 Shell 后，watchdog 自动恢复该 binding 的固定
thread，再挂载 monitor；它不会从目录或其他 session 猜测 thread，也不会向仍在
运行其他前台命令的 pane 注入启动命令。

monitor 同样会在提交 `/goal resume` 前确认 pane 仍由 Codex 进程拥有。滚屏中
残留的 paused-Goal 文本不会被误发到 Shell；没有 Codex 进程时只记录跳过，等待
显式 `codex-watch` 或带 pending recovery 状态的 guardian 恢复。

手工启动时 durable binding 中的 recovery/compaction 计数优先于旧 tmux option，
因此重挂载不会把冷静期或恢复历史清零。升级安装仍保留 guardian 原有的启用和
停止状态。

### 7. Codex CLI 更新流程与 fatal recovery 解耦

使用 `--no-alt-screen` 时，Codex 更新选择页上方可能保留旧对话。watchdog 现在
识别当前屏幕尾部的完整更新块；如果该更新块后已经出现新的 composer、Goal 状态
或 Shell 提示，则视为历史文本，不会再次触发更新。

若 monitor 在更新过程中退出，但 tmux 中仍保存 pending update，guardian 会在
更新进程返回 Shell 后继续处理：先核验更新页要求的 CLI 版本，版本不足时补跑真实
`codex update`，只有版本达到目标后才恢复固定 thread。版本核验失败时 pending
标记会保留，旧 CLI 不会被当作更新成功继续运行。

Codex 自更新重启不属于 fatal recovery，因此不会增加 recovery count，也不会因
之前的 fatal 次数等待 300 秒。若原 thread 的 Goal 已 achieved，更新后只恢复该
thread，不等待 Goal picker，也不发送 `/goal resume` 或续接提示。monitor 在启动
时或运行中明确看到 achieved 状态，还会清除旧的 pending verification/recovery
状态；guardian 不会仅凭这类旧 binding 再次启动已完成任务。

## 哪些错误仍会自动恢复

带有 Codex 终端致命标记的上游和连接错误仍进入统一恢复流程，包括常见的
401、402、429、500、502-504、520-524、连接中断、broken pipe、请求超时、
结构化 `upstream_error`，以及模型容量错误。

特殊情况如下：

| 情况 | 处理方式 |
| --- | --- |
| `codex upstream stalled: no real data for 5m0s` | 切换到 compact model，执行 `/compact`，等待真实压缩事件，再切回 primary model 继续 Goal |
| `502 ... Upstream access denied` | 不再恢复被拒绝的旧 thread，创建新 thread，恢复原 Goal 目标并更新绑定 |
| Codex 更新选择页 | 选择官方更新，核验实际安装版本后再恢复固定 thread；不增加 fatal recovery count，不执行 fatal 冷静期 |
| 上下文窗口耗尽 | 交给 Codex CLI 自身处理，watchdog 不再强制 compact 或轮换 |

只有最近状态为 `Pursuing goal`、`Goal stalled` 或 `Goal blocked` 时，致命错误
才会进入进程恢复。已完成、没有 Goal、普通暂停或 usage-limited 状态不会被
历史错误反复重启。`Goal blocked` 仍代表需要人工审核；如果 blocked 期间出现
新的致命错误，watchdog 可以恢复进程，但不会代替用户执行 `/goal resume`。

## 升级方式

### 从源码仓库升级

请先确认当前项目和 watchdog session 的名称，然后在 watchdog 源码目录执行：

```bash
git fetch origin
git pull --ff-only origin main
./install.sh --session <你的-session名称>
```

安装完成后检查版本：

```bash
codex-watch --version
codex-watch-guardian --version
```

两个命令都应显示 `0.1.24`。如果使用了 user-level guardian，安装器会更新
对应 unit；首次安装默认启用 guardian，已有 unit 则保留原有启用和停止状态。
已有的 watchdog binding 和 Codex thread 记录不会因为安装更新而被删除。

### 使用自定义模型时

0.1.24 的内置默认值为：

```text
primary model:            gpt-5.6-sol
primary reasoning effort: max
compact model:             gpt-5.6-luna
compact reasoning effort:  xhigh
```

这些模型名和 effort 取决于所使用的 provider。升级时请继续使用原来的启动
脚本，或明确传入对应的四个参数；不要假设其他 provider 一定提供这些模型。

生产或无人值守环境建议显式使用安全模式：

```bash
codex-watch --safe
```

省略 `--safe` 会启用 Codex 的最高权限兼容模式。该模式允许 Codex 绕过审批
和 sandbox，只有在运行环境和权限边界已经经过检查时才应使用。

## 升级后的配置注意事项

- `--cooldown-seconds` 默认仍为 `300`；设置为 `0` 才会取消等待。
- `--max-recoveries 0` 默认表示无限恢复，不是禁用恢复。
- `--thread-max-compactions`、`--thread-max-context-tokens` 会被接受，但不再
  控制 watchdog 行为。
- rollout、no-progress、no-event、重复内容和重复命令阈值仍可用于调整遥测，
  但不会代替 Codex 管理上下文或触发自动轮换。
- watchdog 不会在服务器重启后猜测项目目录并创建缺失的 tmux 会话。整机重启
  后仍需用户进入原项目目录，显式运行 `codex-watch`；已存在的 binding 会用于
  恢复该 watchdog session 自己的 thread。
- watchdog 会自动处理可识别的终端级 fatal error，但不会把普通 transcript、
  日志文本或用户消息中的相同字样误当成故障。

## 验证情况

本次源码变更已完成以下预发布检查：

```text
218 tests passed, 43 subtests passed
mypy: Success
pyflakes: passed
compileall: passed
git diff --check: passed
```

正式发布前仍应完成仓库提交、版本标签、干净环境安装和目标 provider 下的
实际启动验证。本文当前不表示这些发布动作已经完成。

## 获取源码

项目地址：<https://github.com/WeirdSky924/codex-watch>

完整安装、安全说明和故障排查请参阅仓库中的 `README.md` 与 `SECURITY.md`。
