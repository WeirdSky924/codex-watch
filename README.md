# Codex Goal Watchdog

Codex Goal Watchdog 用 tmux 托管 Codex CLI，并在长时间 Goal 因上游错误、网络错误或 Codex 自更新而停止时，自动恢复固定的 Codex thread。上下文窗口和自动压缩完全由 Codex 自身管理。

本工具适用于 Linux 服务器上的长时间 Codex 任务。它会自动向 Codex 终端发送按键和命令，但不会修改 `~/.codex/sessions` 中的 Codex 会话文件。

## 先看结论

第一次使用需要完成以下步骤：

1. 安装 Python 3.11+、tmux、Git 和 Codex CLI。
2. 单独运行一次 `codex`，完成登录并确认模型可用。
3. 运行本项目的 `install.sh`。
4. 进入需要工作的项目目录。
5. 使用 `codex-watch` 启动 Codex。
6. 用 `Ctrl-b`、`d` 安全退出 tmux 界面，任务会继续运行。

最常用的命令：

```bash
# 首次启动一个新 Codex 会话
codex-watch --safe

# tmux 消失后，恢复 codex-goal 自己上次绑定的 Codex 会话
codex-watch --safe

# 重新进入正在运行的 tmux 会话
tmux attach -t codex-goal
```

> `--safe` 表示不启用 Codex 的最高权限模式。请先阅读下面的安全说明。

## 安全警告

为了兼容最初的服务器部署，本工具在不传 `--safe` 时会给 Codex 添加：

```text
--dangerously-bypass-approvals-and-sandbox
```

这意味着 Codex 可以不经过确认直接读写文件和执行系统命令。对大多数用户，推荐始终使用：

```bash
codex-watch --safe
```

只有在以下条件全部满足时，才考虑省略 `--safe`：

- 运行环境已经被容器、虚拟机或其他外部机制隔离。
- 运行用户没有不必要的系统权限。
- 代码仓库、凭据和网络访问范围已经检查。
- 你明确接受无人值守执行系统命令的风险。

详细安全说明见 [SECURITY.md](SECURITY.md)。watchdog 日志可能包含提示词、代码片段、目录、请求 ID 和错误内容，发布日志前必须脱敏。

## 1. 系统要求

必须具备：

- Linux。
- Python 3.11、3.12 或 3.13。
- Python `venv` 模块。
- tmux。
- Codex CLI。
- Git，使用源码仓库安装时需要。
- systemd user service，可选但强烈推荐。

先检查当前环境：

```bash
python3 --version
tmux -V
git --version
codex --version
```

只要其中某条命令显示 `command not found`，就先安装对应组件，不要直接运行 `install.sh`。

### Ubuntu 24.04 或 Debian 12

```bash
sudo apt update
sudo apt install -y python3 python3-venv git tmux curl ca-certificates nano
```

安装后再次确认：

```bash
python3 --version
```

版本必须是 3.11 或更高。Ubuntu 22.04 默认 Python 通常低于 3.11，建议升级操作系统，或先通过可信的软件源安装 Python 3.11+，并确保 `python3` 指向受支持版本。

### Fedora

```bash
sudo dnf install -y python3 python3-pip git tmux curl ca-certificates nano
```

### Arch Linux

```bash
sudo pacman -S --needed python git tmux curl ca-certificates nano
```

不同发行版的软件包名称可能略有区别。最终以这两条命令成功为准：

```bash
python3 -m venv --help >/dev/null
tmux -V
```

## 2. 安装并登录 Codex CLI

OpenAI 当前提供的 Linux/macOS 安装方式是：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

也可以使用 npm：

```bash
npm install -g @openai/codex
```

重新加载 Shell，然后检查：

```bash
exec "$SHELL" -l
codex --version
```

第一次运行 Codex：

```bash
codex
```

在界面中完成 ChatGPT 登录或 API Key 配置。进入 Codex 后应至少确认：

- 可以正常发起一次请求。
- 计划使用的模型确实存在。
- 计划使用的 reasoning effort 被当前模型和 provider 支持。

先按 `Ctrl-c` 或使用 Codex 自己的退出命令退出测试会话，再安装 watchdog。

认证、API Key、自定义 provider 和 API base URL 都由 Codex CLI 自己管理，不属于 watchdog 配置。不要把 API Key 写进 `codex-watch` 命令行，因为命令行参数可能被同机用户或进程列表看到。

Codex 官方安装说明：<https://developers.openai.com/codex>

## 3. 安装 Codex Goal Watchdog

推荐使用源码包中的安装器，因为它会同时安装 CLI 和 guardian systemd 服务。

### 方法 A：从 Git 仓库安装

将下面的仓库地址替换为实际发布地址：

```bash
git clone https://github.com/OWNER/codex-goal-watchdog.git
cd codex-goal-watchdog
./install.sh --session codex-goal
```

### 方法 B：从发布压缩包安装

将 `.tar.gz` 发布包放到当前目录，然后运行：

```bash
tar -xzf codex_goal_watchdog-*.tar.gz
cd codex_goal_watchdog-*/
./install.sh --session codex-goal
```

### 方法 C：只安装命令，不安装 guardian

```bash
./install.sh --no-service
```

不推荐在长时间无人值守任务中使用该方式，因为 monitor 意外退出后没有 guardian 帮它重新挂载。

### 安装器具体做了什么

`install.sh` 会：

1. 检查 `python3`、`tmux` 和 `codex`。
2. 创建私有 Python 虚拟环境：

   ```text
   ~/.local/share/codex-goal-watchdog/venv
   ```

3. 创建两个用户命令：

   ```text
   ~/.local/bin/codex-watch
   ~/.local/bin/codex-watch-guardian
   ```

4. 安装并尝试启用 guardian user service。

安装过 guardian unit 后，每次运行 `codex-watch --session NAME` 都会自动执行
`systemctl --user enable --now codex-watch-guardian@NAME.service`。因此 guardian
被手工停止或禁用后，再次启动对应 watchdog session 就会重新启用它。使用
`./install.sh --no-service` 且本机不存在该 unit 时，不会调用 systemd。

升级时安装器会先读取所选 session 的 guardian 状态，再刷新文件：已经停止且禁用
的 session 会继续保持停止，已经运行的 session 会继续运行。systemd unit 使用
安装器拥有的 `$HOME/.local/bin/codex-watch-guardian` 绝对路径，不会因为 `PATH`
中存在另一份 watchdog 而加载不同版本。首次安装没有旧状态时，guardian 默认启用。

安装器本身不需要 root。只有安装系统依赖和启用 lingering 时可能需要 `sudo`。

### 修复 `codex-watch: command not found`

检查命令是否存在：

```bash
ls -l "$HOME/.local/bin/codex-watch"
```

如果文件存在但命令找不到，把用户命令目录加入 PATH：

```bash
printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
source "$HOME/.bashrc"
```

使用 zsh 时，将 `.bashrc` 换成 `.zshrc`。

最后验证：

```bash
codex-watch --version
codex-watch-guardian --version
```

## 4. 配置会话、模型和权限

### 会话名称

默认 tmux 会话名称是：

```text
codex-goal
```

只运行一个项目时可以直接使用默认值。多个项目同时运行时，每个项目必须使用不同名称：

```bash
codex-watch --session project-a --safe
codex-watch --session project-b --safe
```

会话名称建议只使用英文字母、数字、下划线和连字符，不要包含空格。

安装 guardian 时使用的 session 名称，必须和启动 `codex-watch` 时一致：

```bash
./install.sh --session project-a
codex-watch --session project-a --safe
```

### 模型配置

内置默认值是特定 provider 环境使用的模型名称：

```text
primary model:            gpt-5.6-sol
primary reasoning effort: max
compact model:            gpt-5.6-luna
compact reasoning effort: xhigh
```

这些名称不保证在其他 Codex provider 中存在。分享给其他用户时，必须先确认对方可用的模型 ID。

如果当前 provider 支持这些默认值，可以直接启动：

```bash
codex-watch --safe
```

如果不支持，先在 Codex 中检查可用模型，然后明确传入四个参数：

```bash
PRIMARY_MODEL="replace-with-primary-model"
PRIMARY_EFFORT="replace-with-supported-effort"
COMPACT_MODEL="replace-with-compact-model"
COMPACT_EFFORT="replace-with-supported-effort"

codex-watch \
  --safe \
  --primary-model "$PRIMARY_MODEL" \
  --primary-reasoning-effort "$PRIMARY_EFFORT" \
  --compact-model "$COMPACT_MODEL" \
  --compact-reasoning-effort "$COMPACT_EFFORT"
```

参数含义：

- `primary-model`：平时执行 Goal 和恢复后继续工作的模型。
- `compact-model`：遇到 5 分钟上游停滞时，用来执行 `/compact` 的模型。
- `primary-reasoning-effort`：主模型支持的推理强度。
- `compact-reasoning-effort`：压缩模型支持的推理强度。

没有单独压缩模型时，可以先把 compact model 设置成一个确认可用的模型。reasoning effort 的合法值由模型和 provider 决定，不要直接照抄其他人的配置。

### 配置保存在哪里

watchdog 会把模型、effort、阈值和续接提示保存在当前 tmux session 的 `@codex_*` 选项中。这些配置选项会在 tmux 消失后一起消失。恢复次数、成功 compact 次数和等待成功确认的状态另外写入持久 binding，因此 guardian/monitor 重连和 tmux 重启不会把恢复计数重置为 0。

固定 thread ID 还会按 `--session` 名称持久保存到：

```text
~/.local/state/codex-goal-watchdog/bindings/
```

因此 `codex-watch --session project-a` 恢复的是 `project-a` 自己上次固定的 thread，不会选择 Codex 全局最近会话。同一目录使用多个 watchdog 时应给每个实例设置不同的 `--session`。执行 `/clear` 后，monitor 会把新 thread ID 同时写入 tmux 和该持久绑定。

模型等自定义参数仍应保存在自己的启动脚本中，避免每次重新输入长命令。

下面示例会创建 `my-codex-watch`。先把四个 `replace-with-...` 值和 session 名称改成自己的配置：

```bash
nano "$HOME/.local/bin/my-codex-watch"
```

文件内容：

```bash
#!/usr/bin/env bash
set -euo pipefail

exec codex-watch \
  --session project-a \
  --safe \
  --primary-model "replace-with-primary-model" \
  --primary-reasoning-effort "replace-with-supported-effort" \
  --compact-model "replace-with-compact-model" \
  --compact-reasoning-effort "replace-with-supported-effort" \
  "$@"
```

保存后赋予执行权限并检查：

```bash
chmod 700 "$HOME/.local/bin/my-codex-watch"
my-codex-watch --dry-run
```

以后启动和恢复都使用短命令：

```bash
my-codex-watch
my-codex-watch --new
my-codex-watch --resume
```

不带模式参数时恢复 `project-a` 自己的持久绑定；`--new` 明确创建新 thread 并覆盖该绑定；`--resume` 明确改为选择当前目录最近的 Codex thread，并将选择结果绑定给 `project-a`。

如果确实需要最高权限模式，从脚本中删除 `--safe`。不要同时保留一个含密钥的启动脚本；provider 密钥应继续由 Codex 自己的认证配置管理。

### 权限模式

安全模式：

```bash
codex-watch --safe
```

最高权限模式：

```bash
codex-watch
```

最高权限模式是历史默认行为，不代表推荐配置。

### 重试策略

默认设置：

```text
cooldown seconds:    300
maximum recoveries:  0
```

`cooldown seconds=300` 表示第一次 fatal recovery 立即执行；如果恢复后再次 fatal，下一次恢复会先退出失败的 Codex，在 Shell 中等待 5 分钟，再重新启动。`maximum recoveries=0` 表示后续重试次数不受限制。

要修改等待时间或限制次数：

```bash
codex-watch \
  --safe \
  --cooldown-seconds 600 \
  --max-recoveries 5
```

`--cooldown-seconds 0` 表示不等待并立即重启。恢复链始终串行执行，不会同时启动两条恢复流程，也不会因为处于冷静期而丢弃下一次 fatal recovery。

### 压缩等待时间

默认最多等待 600 秒，直到固定 thread 的 rollout 文件真正写入 `context_compacted` 事件：

```bash
codex-watch --safe --compact-wait-seconds 600
```

该数值是超时上限，不是固定睡眠时间。

### 长会话健康遥测

watchdog 会在本机 monitor 内部读取固定 thread 的 rollout 增量，不会让
Codex 模型每隔 30 秒执行一次 `ps`、`tail` 或其他状态轮询。默认遥测字段为：

```text
thread max compactions:       disabled (legacy option; ignored)
thread max rollout bytes:     536870912 (512 MiB; telemetry only)
thread no-progress tokens:    1000000
thread no-event seconds:      1800 (30 分钟)
thread health poll seconds:   30
thread max repeated content:  3
thread max repeated commands: 3
```

`max_compactions` 不属于有效健康阈值。Codex 的原生或历史 `compacted` /
`context_compacted` 事件不会触发 watchdog 轮换；一个 thread 可以进行任意多次
compact。rollout 大小、无进展 token、无事件时长和重复内容/命令只作为诊断遥测，
不会触发 thread rotation、compact 或业务恢复。没有 Goal、普通 paused Goal 或已
完成 Goal 也不会因为这些指标被 watchdog 打断。需要新 thread 时，只能由 Codex
自身、明确的 upstream access denied 恢复，或一次真实 compact timeout 恢复触发；
普通 retryable upstream error 永远只恢复 pinned thread。

`no_rollout_events` 只表示真正没有 rollout 活动的安静 thread。若终端持续出现
503、502、429 或其他已识别的 fatal error，watchdog 会先处理统一 fatal recovery；
在该 fatal recovery 尚未验证成功时，不会让 `no_rollout_events` 抢先执行。新的
成功 task/compact 或恢复验证会清除 fatal 状态；fatal 文本本身不会被当作 rollout
事件计数。

重复内容和重复命令检测只处理 monitor 启动后新增的 rollout 事件，并按同一
active turn 内的连续 streak 计数。它们只作为遥测提供给诊断和 handoff，不会因为
连续重复而自动打断或轮换 thread。`write_stdin`、`wait` 等等待轮询不计入重复
命令。将对应阈值设为 `0` 或负数可关闭该项遥测。

compact 等待超过 `--compact-wait-seconds` 时，流程会把它视为压缩失败，写入
`compaction_timeout` handoff 并走同一套新 thread 轮换；不会无限等待，也不会
反复让模型执行状态查询。该新 thread 例外仅适用于压缩确实超时，不能由
compaction 次数触发。如果 `/compact` 等待期间 rollout 新增了可识别的临时
上游错误，watchdog 会立即停止等待并按普通 fatal recovery 恢复当前 pinned
thread，不会创建 handoff 或新 thread。其他 tmux、Shell 或提交验证超时也不会
被冒充为 `compaction_timeout`。

恢复次数和恢复阶段保存在 watchdog session binding 中。guardian 接管、monitor
重挂或 tmux 重启都不会清零；只有检测到新的成功 `task_complete`、成功
`context_compacted` 或新 thread 已完成可验证接力后才会清零。`context_compacted` 只
作为 Codex 恢复验证和遥测事件，不会累计成 thread 上限，也不会单独触发新 thread。
`--max-recoveries 0` 仍表示无限恢复，健康遥测不会改变这一设置。

可按项目调整 watchdog 遥测阈值：

```bash
codex-watch --safe \
  --thread-max-compactions 0 \
  --thread-max-rollout-bytes 536870912 \
  --thread-no-progress-tokens 1000000 \
  --thread-no-event-seconds 1800 \
  --thread-health-poll-seconds 30 \
  --thread-max-repeated-content 3 \
  --thread-max-repeated-commands 3
```

将某个 watchdog 遥测阈值设为 `0` 或负数可关闭该项记录。watchdog 不再读取
`context_tokens` 或 `model_context_window` 来决定 compact、重启或新建 thread；
上下文窗口、上下文上限和自动 compact 触发值全部由 Codex 自身决定。
`--thread-max-compactions` 无论传入何值都不会产生 watchdog 行为，只为兼容旧
启动脚本保留。
旧参数 `--thread-max-context-tokens` 仅为兼容旧启动脚本而保留，传入任何值
都不会产生 watchdog 行为。

### 自定义日志路径

默认日志目录遵循 XDG：

```text
$XDG_STATE_HOME/codex-goal-watchdog/watchdog.log
```

如果没有设置 `XDG_STATE_HOME`，使用：

```text
~/.local/state/codex-goal-watchdog/watchdog.log
```

临时指定日志文件：

```bash
codex-watch --safe --log-path "$HOME/codex-watchdog.log"
```

## 5. 第一次启动

### 第一步：进入项目目录

必须先进入 Codex 要操作的项目目录：

```bash
PROJECT_DIR="$HOME/projects/your-project"
cd "$PROJECT_DIR"
```

将 `PROJECT_DIR` 改成实际项目目录。`--resume` 会按当前目录查找最近的 Codex thread，因此以后恢复时也必须进入同一目录。

### 第二步：启动

使用默认模型、安全模式和默认 session：

```bash
codex-watch --safe
```

使用自定义模型：

```bash
PRIMARY_MODEL="replace-with-primary-model"
PRIMARY_EFFORT="replace-with-supported-effort"
COMPACT_MODEL="replace-with-compact-model"
COMPACT_EFFORT="replace-with-supported-effort"

codex-watch \
  --safe \
  --primary-model "$PRIMARY_MODEL" \
  --primary-reasoning-effort "$PRIMARY_EFFORT" \
  --compact-model "$COMPACT_MODEL" \
  --compact-reasoning-effort "$COMPACT_EFFORT"
```

后台启动但不立即进入 tmux：

```bash
codex-watch --safe --no-attach
```

正常启动后会发生以下事情：

1. 查找 `codex-goal` 自己持久保存的 thread ID；存在时恢复该 ID，不存在时才创建新 thread。
2. 创建名为 `codex-goal` 的 tmux 会话并启动 Codex。
3. 固定实际 thread ID；新建空白首页尚未写入 rollout 时会读取 Codex 自己创建的 shell snapshot。
4. 持久保存 `codex-goal` 与 thread ID 的绑定，并保存运行参数到当前 tmux 会话。
5. 挂载输出 monitor。
6. 默认自动进入 tmux 界面。

从 `0.1.11` 开始，Codex 退出后供 watchdog 恢复使用的内部 Bash 会禁用命令历史，并将 `HISTFILE` 隔离到 `/dev/null`。自动注入的 `/quit`、`codex ... resume ...` 等命令不会写入宿主用户的 `~/.bash_history`；watchdog 不会修改或清理已有历史记录。

### 第三步：创建并运行 Goal

进入 Codex 后，按正常 Codex CLI 流程创建 Goal。通常 watchdog 只负责在 fatal
error 后恢复当前固定 thread。唯一例外是 `Upstream access denied`：旧 thread
已无法恢复时，watchdog 会从其 rollout 提取上一 Goal Objective，在全新 thread
中要求 Codex 重新创建该 Goal 并接力。

## 6. tmux 日常操作

### 安全退出界面但保持任务运行

依次按：

```text
Ctrl-b
d
```

注意是先按 `Ctrl-b`，松开后再按 `d`。看到 `[detached from codex-goal]` 后，Codex 仍在后台运行。

不要直接关闭 Codex、输入 `/quit`，也不要杀掉 tmux session。

### 重新进入

```bash
tmux attach -t codex-goal
```

自定义 session：

```bash
tmux attach -t project-a
```

### 使用键盘查看历史记录

依次按：

```text
Ctrl-b
[
```

进入复制/滚动模式后，可以使用方向键、`PageUp`、`PageDown`、`g` 和 `G` 查看历史：

- `g`：跳到最早的记录。
- `G`：回到最新位置。
- `/关键词`：向下搜索。
- `?关键词`：向上搜索。
- `n`、`N`：切换搜索结果。
- `q` 或 `Esc`：退出复制/滚动模式。

### 永久开启鼠标滚轮

只对当前 tmux server 临时开启：

```bash
tmux set-option -g mouse on
```

永久开启，并避免重复写入相同配置：

```bash
touch "$HOME/.tmux.conf"
grep -qxF 'set -g mouse on' "$HOME/.tmux.conf" || \
  printf '\nset -g mouse on\n' >> "$HOME/.tmux.conf"
tmux source-file "$HOME/.tmux.conf"
```

确认配置已经生效：

```bash
tmux show-options -g -v mouse
```

期望输出：

```text
on
```

开启后可以直接使用鼠标滚轮查看历史。滚动后按 `q` 或 `Esc` 返回最新终端画面。该配置只改变 tmux 的鼠标交互，不会停止或重启 Codex。

### 查看所有 tmux 会话

```bash
tmux ls
```

### 确认 monitor 管道存在

```bash
tmux list-panes -t codex-goal -F 'pipe=#{pane_pipe} dead=#{pane_dead} cmd=#{pane_current_command}'
```

正常情况下应看到：

```text
pipe=1 dead=0
```

## 7. 恢复上一次会话

### 情况 A：只是退出 SSH 或关闭终端

tmux 仍在时直接重新进入：

```bash
tmux attach -t codex-goal
```

不需要再次运行 `codex-watch --resume`。

如果 tmux 仍在但 Codex 已经被 `/quit` 退出，进入原项目目录后再次运行：

```bash
PROJECT_DIR="$HOME/projects/your-project"
cd "$PROJECT_DIR"
codex-watch --safe
```

当该 session 有持久 binding 且 pane 是空闲 Shell 时，watchdog 会自动启动
binding 中固定的 thread，再挂载 monitor。它不会从目录或全局记录猜测其他
thread；没有 binding、pane 仍在运行其他前台命令，或目录与 binding 不一致时，
watchdog 会保持 fail-closed 并要求你明确处理。

monitor 看到滚屏中残留的 `Goal paused` 或恢复选择文本时，会先确认 pane
仍由 Codex 进程拥有；如果当前只是 Shell，不会把 `/goal resume` 或其他文本
注入 Shell。此时由显式 `codex-watch` 启动绑定 thread，或由带有 pending
recovery 状态的 guardian 接管恢复。

### 情况 B：Codex fatal error，但 tmux 仍在

正常情况下 watchdog 会自动处理，不需要人工操作。可以查看日志：

```bash
tail -f "$HOME/.local/state/codex-goal-watchdog/watchdog.log"
```

### 情况 C：服务器重启或 tmux 会话消失

服务器重启会结束 tmux 进程，但不会自动删除 `~/.codex/sessions` 中的 Codex 会话记录。

进入原项目目录并执行：

```bash
PROJECT_DIR="$HOME/projects/your-project"
cd "$PROJECT_DIR"
codex-watch --safe
```

该命令会恢复 `codex-goal` 自己上次固定的 thread，而不是 Codex 在该目录或全局的最近 thread。只有需要主动改绑到当前目录最近 thread 时才使用 `--resume`；需要放弃旧绑定并创建全新 thread 时使用 `--new`。

当前版本的 guardian 会在开机后启动，但不会自动创建缺失的 tmux 会话。因此整机重启后仍须进入原项目目录执行一次上面的恢复命令。

使用自定义模型时，必须重新传入模型参数，或者使用前面创建的固定启动脚本：

```bash
cd "$PROJECT_DIR"
my-codex-watch
```

如果恢复界面后普通 paused Goal 没有自动继续，在 Codex 中执行：

```text
/goal resume
```

`Goal blocked (/goal resume)` 与普通 paused Goal 不同。watchdog 会把 blocked
视为需要审核或人工批准的状态并保持暂停；确认阻塞原因已经解决后，由用户手工执行
`/goal resume`。blocked 状态本身不会触发重启或进入冷静期。

### 情况 D：恢复指定 thread

如果 Codex 输出过类似 `To continue this session, run codex resume ...` 的提示，可以使用其中的 thread ID：

```bash
codex-watch --thread-id "$THREAD_ID" --safe
```

指定 thread 比“选择当前目录最近记录”更精确。watchdog 的自动恢复始终使用固定 UUID，不使用 `resume --last`，避免被其他 Codex 进程重定向到错误 thread。

### 明确创建新 thread

需要放弃该 watchdog session 的旧绑定并新建时执行：

```bash
codex-watch --session codex-goal --new --safe
```

如果同名 tmux 仍存在，`--new` 会拒绝执行，防止覆盖一个仍在运行的会话。先确认旧任务已经结束并关闭 tmux，或者改用新的 `--session` 名称。

### `--resume` 找不到记录

出现：

```text
no Codex thread found for ...
```

依次检查：

1. 当前目录是否和原 Codex 会话的工作目录一致。
2. 当前 Linux 用户是否和原来一致。
3. `~/.codex/sessions` 是否仍然存在。
4. 是否应改用明确的 `--thread-id`。

## 8. Guardian 服务

guardian 的职责是监督 monitor，而不是替代 Codex 或 tmux：

- monitor 丢失时重新挂载。
- monitor 丢失但 fatal error 仍显示在画面上时接管恢复。
- monitor 重新挂载前 fatal error 已经出现时，从当前画面和 rollout 记录补检并恢复。
- 完成交接后由 monitor 独占实时 fatal；guardian 不会持续重扫 active pipe，双方通过
  原子 incident claim 避免对同一个错误重复执行恢复。
- Codex 自更新完成并退回 Shell 后，重新启动固定 thread。
- tmux session 不存在时记录 `session_missing`，但不自行猜测项目并创建新会话。

### 查看状态

```bash
systemctl --user status codex-watch-guardian@codex-goal.service
```

只输出简短状态：

```bash
systemctl --user is-active codex-watch-guardian@codex-goal.service
systemctl --user is-enabled codex-watch-guardian@codex-goal.service
```

期望结果分别是：

```text
active
enabled
```

### 手动启用、重启或停止

```bash
systemctl --user enable --now codex-watch-guardian@codex-goal.service
systemctl --user restart codex-watch-guardian@codex-goal.service
systemctl --user stop codex-watch-guardian@codex-goal.service
```

### 暂停 watchdog 和内部 Codex thread

暂停时保留 tmux、thread ID、Goal 和恢复状态，只停止自动监控及当前正在执行的
Codex turn：

```bash
systemctl --user disable --now codex-watch-guardian@codex-goal.service
tmux pipe-pane -t codex-goal
tmux send-keys -t codex-goal C-c
```

确认状态：

```bash
tmux list-panes -t codex-goal -F 'pipe=#{pane_pipe} cmd=#{pane_current_command}'
systemctl --user is-active codex-watch-guardian@codex-goal.service
systemctl --user is-enabled codex-watch-guardian@codex-goal.service
```

恢复时从原项目目录执行 `codex-watch --no-attach` 或直接执行
`codex-watch`。这会按该 watchdog session 自己的持久 binding 恢复 thread，并重新
启用 guardian；不会选择其他 Codex session。

自定义 session 时替换最后的 `codex-goal`：

```bash
systemctl --user enable --now codex-watch-guardian@project-a.service
```

### 让 user service 在退出登录后和开机时运行

在服务器上建议启用 lingering：

```bash
sudo loginctl enable-linger "$USER"
```

检查：

```bash
loginctl show-user "$USER" -p Linger
```

期望看到：

```text
Linger=yes
```

如果 `systemctl --user` 提示无法连接 user bus，先重新登录 SSH；仍然失败时检查 systemd user session 和 lingering 配置。

## 9. 自动恢复规则

Codex TUI 中带 `■` 的 fatal error 行会触发恢复；`⚠ Selected model is at capacity. Please try a different model` 容量告警也按 fatal error 处理。源码、测试、工具输出或普通 Agent 消息中出现没有对应终端标记的相同文本，不会触发重启。

所有识别到的 fatal error 都进入同一个串行恢复状态机：第一次立即恢复并处理 Goal
状态；如果恢复失败并再次出现 fatal，则退出失败进程、等待默认 300 秒，再执行同一
恢复流程。后续失败继续每次等待 300 秒，默认次数无限。除 `Upstream access denied`
会轮换到新 thread 外，其余错误恢复当前固定 thread。

从 `0.1.9` 开始，fatal recovery 要求最近一次可见 Goal 状态是 `Pursuing goal`、`Goal stalled (/goal resume)` 或 `Goal blocked (/goal resume)`。如果 Goal 已经 achieved、没有 Goal、处于 paused 或 usage-limited 状态，watchdog 不会因为 fatal 行重启 Codex，也不会发送 fatal-recovery 续接提示；启动和历史回放阶段的普通 paused Goal 自动恢复逻辑不变。这样可以避免任务完成后的旧错误触发无限重启。blocked 状态本身不是 fatal，不会触发恢复或冷静期；如果 blocked 期间另有新的 fatal error，watchdog 仍会恢复 Codex 进程和固定 thread，但会让 Goal 继续保持 blocked，等待人工处理。单独的 stalled 状态同样不触发恢复；只有出现与 rollout 新 `task_complete` incident 对应的 fatal error 时才恢复进程，并在重启后恢复 stalled Goal。

从 `0.1.10` 开始，可见 fatal 行还必须与当前固定 thread 的新 rollout `task_complete` 事件一致。watchdog 会在发送任何 `Ctrl-C` 前持久化该事件的 `turn_id`；inline TUI 后续重绘同一条 503、容量或其他 fatal 行时只会忽略，不会打断新恢复的 turn。同类错误如果发生在新的 turn 中会得到新的 `turn_id`，仍按原冷静期和无限重试配置恢复。

| 错误 | 自动操作 |
| --- | --- |
| `codex upstream stalled: no real data for 5m0s` | 切到 compact model，恢复固定 thread，执行 `/compact`，等待真实压缩事件，再切回 primary model 并继续 Goal |
| Codex context window exhausted | 交由 Codex 自身处理，watchdog 不接管 |
| HTTP 502 且消息为 `Upstream access denied` | 不再恢复被拒绝的 thread；创建新 thread，提取并重建上一 Goal，再自动更新 tmux 与持久绑定 |
| HTTP 401（包括 `API DISABLE`）、402、429、500、502-504、520-524 | 第一次立即使用 primary model 恢复；再次 fatal 后等待冷静期重试 |
| connection reset/closed、broken pipe、gateway/request timeout、unexpected EOF | 使用 primary model 重启固定 thread |
| 结构化 `upstream_error` JSON | 使用 primary model 重启固定 thread |
| `Selected model is at capacity` | 第一次立即使用 primary model 恢复；再次出现时等待冷静期重试 |
| `Our servers are currently overloaded` | 第一次立即使用 primary model 恢复；再次出现时等待冷静期重试，不执行 compact |
| Codex 出现更新选择页 | 选择官方更新、等待返回 Shell、核验实际安装版本，再恢复固定 thread；不计入 fatal recovery 次数，也不执行 300 秒冷静期 |

恢复普通 paused 或 usage-limited Goal 时，watchdog 会优先执行 `/goal resume`。`Goal blocked` 不会自动执行 `/goal resume`，也不会发送文本续接提示；用户完成审核、批准或外部条件处理后再手工恢复。Codex 当前没有向 watchdog 暴露稳定的 blocked 原因分类，因此本工具保守地将所有 blocked 状态都按人工审核处理。

从 `0.1.1` 开始，大型会话恢复时会最多等待 10 分钟，持续检查 `Resume paused goal?` 选择页；出现后自动选择 `Resume goal`。如果已经显示 `Pursuing goal`，不会再注入多余文本。

从 `0.1.2` 开始，HTTP 402 也属于 fatal recovery；所有 fatal recovery 都先立即尝试一次，失败后的后续重试默认等待 5 分钟。5m0s/context exhausted 的 compact 分支完成压缩后切回 primary model 属于同一恢复链内部切换，不会再次等待 5 分钟。

从 `0.1.3` 开始，手工启动、`--resume`、`--thread-id` 恢复以及重新接入已有 tmux session 时，watchdog 都会立即检查当前 Goal 状态。若大型 thread 仍在回放历史，monitor 会在稍后出现 `Resume paused goal?` 时自动选择 `Resume goal`；普通空闲会话不会被注入续接文本。

从 `0.1.4` 开始，Codex 更新页即使出现在 thread ID 创建之前也会由 watchdog 处理。watchdog 会选择 `Update now`、等待官方安装命令结束，然后执行 `codex --version` 核验；如果版本仍低于更新页目标，会额外执行一次 `codex update` 并再次核验。只有真实版本达到目标后才会启动或恢复 Codex，不能再用旧版本继续运行并把更新页留在 tmux 中。

从 `0.1.24` 开始，`--no-alt-screen` 在更新页上方保留旧对话时，watchdog 会识别屏幕尾部的完整更新选择块；如果选择块后已经出现新的 composer、Goal 状态或 Shell 提示，则按历史文本忽略。官方更新返回 Shell 后，pending update 会先核验目标版本，再恢复固定 thread。该重启属于更新流程，不增加 fatal recovery count，也不执行 fatal 的 300 秒冷静期；若原 Goal 已 achieved，只恢复 thread，不等待 Goal 选择页或发送续接文本。

从 `0.1.25` 开始，普通临时上游断链、连续 503/502 等错误始终只恢复当前 pinned thread。`/compact` 等待期间新增的 retryable upstream failure 也会中止等待并回到该 thread；只有明确 `Upstream access denied` 或真实 `compaction_timeout` 才会进入新 thread 轮换。旧版遗留的 thread-rotation marker 如果缺少受支持原因或来源 thread ID，会被自动清理。

monitor 启动时或运行中明确看到 `Goal achieved`，还会清除该 thread 遗留的 pending verification、recovery phase 和 recovery count。guardian 即使读到旧 binding，也不会在 achieved 或其他非恢复 Goal 状态下仅因 Codex 进程缺失而重启 thread。

从 `0.1.5` 开始，`Selected model is at capacity` 容量告警进入统一 fatal recovery 流程：首次立即恢复，后续失败按冷静期继续重试，默认不限制次数。

同一版本还会在 tmux 输入 `/quit`、`/compact`、`/goal resume`、Codex 启动命令或续接提示后等待并检查 composer；如果原文或 Shell 命令仍停留在输入行，会自动重试 Enter。该确认逻辑由所有恢复路径共用，避免恢复流程停在未发送的输入框。

从 `0.1.6` 开始，在受管 Codex 会话中执行 `/clear` 后，watchdog 会从当前 tmux pane 的 Codex 进程树中识别最新顶层 CLI rollout，自动更新固定 thread ID，并将新 thread 的恢复计数重置为 0。子 Agent thread 和同目录下其他 Codex 进程不会被误绑定。

从 `0.1.7` 开始，Codex TUI 中带 `■` fatal 标记的 HTTP 401（包括 `API DISABLE`）进入统一恢复流程：首次立即恢复，后续失败按冷静期继续重试，默认不限制次数。

从 `0.1.8` 开始，每个 `--session` 的固定 thread ID 会持久保存在 watchdog 状态目录。tmux 消失后，不带模式参数重新启动会恢复该 watchdog session 自己的 ID；`/clear` 后的新 ID 会同步覆盖持久绑定。`--resume` 仅在显式使用时选择当前目录最近的 Codex thread，`--new` 用于明确创建新 thread。

从 `0.1.24` 开始，已有 watchdog binding 的 recovery/compaction 计数在手工重启
时优先于可能过期的 tmux option；如果 tmux 仍在但 Codex 已退出，`codex-watch`
会在确认 pane 是空闲 Shell 后恢复这个固定 thread。升级安装会保留 guardian
原有的启用/停止状态，暂停的 session 不会因为升级被偷偷启动。

从 `0.1.9` 开始，fatal recovery 只在最近 Goal 状态为 `Pursuing goal`、`Goal stalled (/goal resume)` 或 `Goal blocked (/goal resume)` 时运行。Goal 完成后即使屏幕上残留 `503`、`upstream_error` 或其他 fatal 行，也不会再触发恢复链。

从 `0.1.10` 开始，monitor 与 guardian 使用 rollout `task_complete` 的 `turn_id` 对 fatal 事件去重。恢复后残留在 tmux 历史中的旧错误不会再次触发 `Ctrl-C`；真正的新失败仍会自动恢复。

从 `0.1.12` 开始，`Goal blocked (/goal resume)` 统一保持暂停。手工启动、历史回放、fatal recovery、guardian 接管和 Codex 更新重启都不会自动越过 blocked；fatal 进程恢复完成后仍等待用户手工 `/goal resume`。

从 `0.1.13` 开始，`stream disconnected before completion: Our servers are currently overloaded. Please try again later.` 进入统一 fatal recovery；它使用 primary model 重启固定 thread，不执行 Luna compact，后续失败按配置冷静期重试，默认次数无限。

从 `0.1.14` 开始，stalled Goal 中出现新的、与 rollout incident 对应的 fatal error 时也会进入统一恢复流程。用户主动进入 stalled、手工启动 watchdog、历史回放、Codex 更新或已处理 fatal 的重绘都不会自动恢复 stalled Goal；只有该次新 fatal 的进程恢复链会在重启后执行 `/goal resume`。

从 `0.1.16` 开始，`502 Bad Gateway: Upstream access denied` 被视为旧 thread 已被
上游拒绝。watchdog 会提取该 thread 最近一次 Goal Objective 原文，启动不含旧
`resume <UUID>` 的全新 Codex thread，并要求它按“最新用户要求 > 当前工作树 >
唯一 ACTIVE Plan > canonical 项目记录 > handoff 缓存”的顺序重新校准后创建同一
Goal。handoff 或计划存在滞后时不得覆盖较新的工作树和 ACTIVE 状态。新 thread ID
会覆盖 tmux 与持久 session 绑定；自动轮换保留恢复次数，因此连续封禁仍遵守首次
立即、后续默认冷静 300 秒的策略。旧 Goal 若处于 blocked 人工审核态，新 thread
只恢复 Objective 和上下文，不会借轮换越过审核继续产品执行。

## 10. 多项目配置示例

项目 A：

```bash
cd "$HOME/projects/project-a"
codex-watch --session project-a --safe --no-attach
```

项目 B：

```bash
cd "$HOME/projects/project-b"
codex-watch --session project-b --safe --no-attach
```

分别安装 guardian：

```bash
systemctl --user enable --now codex-watch-guardian@project-a.service
systemctl --user enable --now codex-watch-guardian@project-b.service
```

分别进入：

```bash
tmux attach -t project-a
tmux attach -t project-b
```

不要让两个不同项目共用同一个 tmux session 名称。

## 11. 日志和排错

### 查看最新日志

```bash
tail -n 100 "$HOME/.local/state/codex-goal-watchdog/watchdog.log"
```

持续查看：

```bash
tail -f "$HOME/.local/state/codex-goal-watchdog/watchdog.log"
```

### guardian 显示 `session_missing`

这表示 guardian 正常运行，但对应 tmux session 不存在。进入项目目录并启动或恢复：

```bash
codex-watch --safe
```

存在该 watchdog session 的持久绑定时会恢复其固定 thread；没有绑定时会创建新 thread。不要为了普通重启添加 `--resume`，否则会改为选择当前目录最近的 Codex thread。

### 出现 `required command not found`

按报错名称检查：

```bash
command -v python3
command -v tmux
command -v codex
```

安装器必须能同时找到这三个命令。

### 模型不存在或 effort 不支持

先直接运行：

```bash
codex
```

确认 provider 的可用模型和推理强度，然后使用 `--primary-model`、`--compact-model` 和对应 effort 参数覆盖默认值。

### 已存在 tmux，但提示没有固定 thread ID

出现：

```text
tmux session 'codex-goal' already exists.
It was not initialized by codex-watch and has no pinned Codex thread ID.
```

说明同名 tmux session 已存在，但其中没有 watchdog 所需的固定 thread ID。watchdog 无法安全判断应该监控哪一个 Codex 会话。

如果需要把已有 Codex 会话接入 watchdog：

1. 进入目标项目目录，先直接运行 `codex`，创建或恢复目标会话。
2. 从 Codex 输出的 `To continue this session, run codex resume <UUID>` 中取得 thread UUID。
3. 退出原 Codex 进程，选择一个未占用的 tmux session 名称，让 watchdog 恢复该 thread：

```bash
codex-watch --session recovered-session --thread-id "$THREAD_ID" --safe
```

如果目标 Codex 进程本来就运行在报错所指的 tmux session 内，可以用该 session 名和 thread UUID 原地接管：

```bash
codex-watch --session existing-session --thread-id "$THREAD_ID" --safe
```

如果不需要恢复已有对话，只想让 watchdog 自动创建一个全新的 Codex 会话，使用一个未占用的 session 名即可，不需要 `--thread-id`：

```bash
codex-watch --session new-session --safe
```

不要为了消除提示直接关闭已有 tmux session；其中可能仍有正在运行的任务。

### fatal error 后没有恢复

依次检查：

```bash
tmux list-panes -t codex-goal -F 'pipe=#{pane_pipe} dead=#{pane_dead}'
systemctl --user is-active codex-watch-guardian@codex-goal.service
tail -n 100 "$HOME/.local/state/codex-goal-watchdog/watchdog.log"
```

如果 `pipe=0`，guardian 正常情况下会在数秒内重挂。需要立即刷新时：

```bash
systemctl --user restart codex-watch-guardian@codex-goal.service
```

### 恢复卡在 `Resume paused goal?`

先确认版本：

```bash
codex-watch --version
```

`0.1.3` 及以后会在手工启动时立即检查，并由 monitor 持续识别稍后出现的选择页，然后自动选择 `Resume goal`。旧版本需要升级。大型 thread 的历史回放可能持续数分钟，期间不要重复启动第二条恢复流程。

### Codex 更新后仍显示旧版本

先确认 watchdog 版本：

```bash
codex-watch --version
```

`0.1.24` 及以后还会在 `--no-alt-screen` 保留旧对话时识别屏幕尾部的真实更新页，并在更新进程已经返回 Shell、monitor 中途退出的情况下继续 pending update。更新恢复不会消耗 fatal recovery 次数或等待 fatal 冷静期。watchdog 会核验真实安装版本，并在官方更新没有落盘时自动补跑 `codex update`。查看更新处理记录：

```bash
tail -n 100 "$HOME/.local/state/codex-goal-watchdog/watchdog.log"
codex --version
```

若日志显示 `Codex update did not install the requested version`，说明包管理器或网络更新确实失败；watchdog 会停止恢复旧版本，而不是越过错误继续启动。

### tmux 中颜色异常

检查终端类型：

```bash
printf '%s\n' "$TERM"
tmux info | head
```

可尝试使用 256 色模式重新连接：

```bash
tmux -2 attach -t codex-goal
```

## 12. 升级

### Git 安装

```bash
cd codex-goal-watchdog
git pull
./install.sh --session codex-goal
```

### 新发布包升级

解压新版本后，在新目录运行：

```bash
./install.sh --session codex-goal
```

安装器会升级私有虚拟环境并刷新 user service 文件；已有 guardian 的启用/停止
状态会被保留，首次安装才默认启用 guardian。

如果 session 当前处于暂停状态，升级会保留停止/禁用状态，不会偷偷启动 guardian
或 Codex。升级后先核对两个入口来自同一版本：

```bash
codex-watch --version
codex-watch-guardian --version
systemctl --user cat codex-watch-guardian@.service
```

两个版本应一致，unit 的 `ExecStart` 应指向 `$HOME/.local/bin/codex-watch-guardian`。

如果 Codex 当前正在 tmux 中工作，不需要杀掉 Codex。让新 monitor 立即加载更新：

```bash
tmux pipe-pane -t codex-goal
systemctl --user restart codex-watch-guardian@codex-goal.service
```

guardian 会在数秒内重新挂载 monitor。

## 13. 卸载

在源码或发布包目录运行：

```bash
./uninstall.sh --session codex-goal
```

默认保留日志和状态。连同日志一起删除：

```bash
./uninstall.sh --session codex-goal --purge-state
```

卸载前先确认是否还有正在运行的 Codex/tmux 任务。卸载器移除 watchdog 环境和 guardian，但不会替你判断正在运行的工作是否可以终止。

## 14. 开发、测试和构建发布包

### 创建开发环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip build twine
```

本项目运行时没有第三方 Python 依赖，`build` 和 `twine` 只用于发布。

### 运行测试

```bash
python -m unittest discover -s tests -v
```

### 编译检查

```bash
python -m compileall -q codex_goal_watchdog tests
bash -n install.sh uninstall.sh
```

### 构建 wheel 和源码包

```bash
python -m build
```

产物位于：

```text
dist/codex_goal_watchdog-<VERSION>-py3-none-any.whl
dist/codex_goal_watchdog-<VERSION>.tar.gz
```

### 检查发布包

```bash
python -m twine check dist/*.whl dist/*.tar.gz
```

在空虚拟环境中安装 wheel：

```bash
python3 -m venv /tmp/codex-watch-smoke
/tmp/codex-watch-smoke/bin/python -m pip install --no-deps dist/*.whl
/tmp/codex-watch-smoke/bin/codex-watch --version
```

### 生成校验和

在 `dist` 目录中运行：

```bash
cd dist
sha256sum codex_goal_watchdog-*.whl codex_goal_watchdog-*.tar.gz > SHA256SUMS
sha256sum -c SHA256SUMS
```

### 发布前检查

1. Python 3.11、3.12 和 3.13 测试全部通过。
2. wheel 能在空虚拟环境安装。
3. sdist 中包含 `install.sh`、`uninstall.sh` 和 systemd unit。
4. README、测试和发布包不包含真实目录、thread ID、主机名或私有日志。
5. `CHANGELOG.md`、`pyproject.toml` 和包内版本一致。
6. wheel、sdist 和 `SHA256SUMS` 一起发布。

## 15. 命令参数速查

```text
--session NAME                     tmux 和 guardian 会话名称
--primary-model MODEL              正常工作和恢复后的主模型
--primary-reasoning-effort VALUE   主模型推理强度
--compact-model MODEL              执行压缩的模型
--compact-reasoning-effort VALUE   压缩模型推理强度
默认（无模式参数）                 恢复该 --session 自己持久绑定的 thread；无绑定时新建
--resume                           明确选择当前目录最近的 Codex thread 并改绑
--thread-id UUID                   恢复明确指定的 thread
--new                              明确新建 thread 并覆盖该 --session 的旧绑定
--safe                             不启用最高权限绕过
--no-attach                        后台启动，不立即进入 tmux
--cooldown-seconds N               首次恢复失败后再次重试前的等待秒数，默认 300
--max-recoveries N                 最大恢复次数，0 表示无限
--compact-wait-seconds N           等待真实压缩事件的超时
--thread-max-compactions N         旧版兼容参数，忽略（Codex 可无限 compact）
--thread-max-rollout-bytes N       rollout 大小遥测阈值，默认 512 MiB，不触发轮换
--thread-max-context-tokens N      旧版兼容参数，忽略（上下文由 Codex 管理）
--thread-no-progress-tokens N      无进展遥测阈值，默认 1000000，不触发轮换
--thread-no-event-seconds N        无事件遥测阈值，默认 1800，不触发轮换
--thread-health-poll-seconds N     遥测采样间隔，默认 30
--thread-max-repeated-content N    重复 assistant 内容遥测阈值，默认 3
--thread-max-repeated-commands N   重复 shell 命令遥测阈值，默认 3
--resume-prompt TEXT               没有 Goal 状态时使用的续接文本
--log-path PATH                    覆盖默认日志文件
--dry-run                          只打印将执行的 tmux/Codex 命令
```

查看当前版本的完整帮助：

```bash
codex-watch --help
```

## License

MIT，见 [LICENSE](LICENSE)。
