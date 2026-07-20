# Codex Goal Watchdog

Codex Goal Watchdog 用 tmux 托管 Codex CLI，并在长时间 Goal 因上游错误、网络错误、上下文耗尽或 Codex 自更新而停止时，自动恢复固定的 Codex thread。

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

# 恢复当前项目目录最近一次 Codex 会话
codex-watch --resume --safe

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
- `compact-model`：遇到 5 分钟上游停滞或上下文耗尽时，用来执行 `/compact` 的模型。
- `primary-reasoning-effort`：主模型支持的推理强度。
- `compact-reasoning-effort`：压缩模型支持的推理强度。

没有单独压缩模型时，可以先把 compact model 设置成一个确认可用的模型。reasoning effort 的合法值由模型和 provider 决定，不要直接照抄其他人的配置。

### 配置保存在哪里

watchdog 会把 thread ID、模型、effort、恢复次数和续接提示保存在当前 tmux session 的 `@codex_*` 选项中。这些选项可以支持当前 tmux 内的自动恢复，但服务器重启后会随 tmux 一起消失。

当前版本没有单独的永久配置文件。自定义模型用户应保存一个自己的启动脚本，避免每次重新输入长命令。

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
my-codex-watch --resume
```

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
cooldown seconds:    0
maximum recoveries:  0
```

`maximum recoveries=0` 表示无限重试。要限制重试频率和次数：

```bash
codex-watch \
  --safe \
  --cooldown-seconds 30 \
  --max-recoveries 5
```

恢复链始终串行执行，不会同时启动两条恢复流程。

### 压缩等待时间

默认最多等待 600 秒，直到固定 thread 的 rollout 文件真正写入 `context_compacted` 事件：

```bash
codex-watch --safe --compact-wait-seconds 600
```

该数值是超时上限，不是固定睡眠时间。

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

1. 创建名为 `codex-goal` 的 tmux 会话。
2. 在 tmux 中启动 Codex。
3. 检测并固定新创建的 thread ID。
4. 保存模型、权限和恢复参数到当前 tmux 会话。
5. 挂载输出 monitor。
6. 默认自动进入 tmux 界面。

### 第三步：创建并运行 Goal

进入 Codex 后，按正常 Codex CLI 流程创建 Goal。watchdog 不负责生成 Goal 内容，只负责在 fatal error 后恢复当前固定 thread。

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
codex-watch --resume --safe
```

当前版本的 guardian 会在开机后启动，但不会猜测项目目录并自动创建缺失的 tmux 会话。因此整机重启后必须执行一次上面的恢复命令。

使用自定义模型时，必须重新传入模型参数，或者使用前面创建的固定启动脚本：

```bash
cd "$PROJECT_DIR"
my-codex-watch --resume
```

如果恢复界面后 Goal 处于暂停状态且没有自动继续，在 Codex 中执行：

```text
/goal resume
```

### 情况 D：恢复指定 thread

如果 Codex 输出过类似 `To continue this session, run codex resume ...` 的提示，可以使用其中的 thread ID：

```bash
codex-watch --thread-id "$THREAD_ID" --safe
```

指定 thread 比“选择当前目录最近记录”更精确。watchdog 的自动恢复始终使用固定 UUID，不使用 `resume --last`，避免被其他 Codex 进程重定向到错误 thread。

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

只有 Codex TUI 中带 `■` 的 fatal error 行会触发恢复。源码、测试、工具输出或普通 Agent 消息中出现相同文本不会触发重启。

| 错误 | 自动操作 |
| --- | --- |
| `codex upstream stalled: no real data for 5m0s` | 切到 compact model，恢复固定 thread，执行 `/compact`，等待真实压缩事件，再切回 primary model 并继续 Goal |
| Codex context window exhausted | 执行相同的 compact 流程 |
| HTTP 429、500、502-504、520-524 | 直接使用 primary model 重启固定 thread |
| connection reset/closed、broken pipe、gateway/request timeout、unexpected EOF | 使用 primary model 重启固定 thread |
| 结构化 `upstream_error` JSON | 使用 primary model 重启固定 thread |
| Codex 自更新完成后退回 Shell | 使用更新后的 Codex 恢复固定 thread |

恢复 paused、blocked 或 usage-limited Goal 时，watchdog 会优先执行 `/goal resume`。没有可识别 Goal 状态时才发送文本续接提示。

从 `0.1.1` 开始，大型会话恢复时会最多等待 10 分钟，持续检查 `Resume paused goal?` 选择页；出现后自动选择 `Resume goal`。如果已经显示 `Pursuing goal`，不会再注入多余文本。

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
codex-watch --resume --safe
```

如果项目从未创建过 Codex 会话，去掉 `--resume`。

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
existing tmux session has no pinned Codex thread ID
```

说明该 tmux 可能不是由 `codex-watch` 正确创建。先确认里面没有需要保留的任务，再使用新的 session 名称和明确 thread ID：

```bash
codex-watch --session recovered-session --thread-id "$THREAD_ID" --safe
```

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

`0.1.1` 及以后会持续等待并自动选择 `Resume goal`。旧版本需要升级。大型 thread 的历史回放可能持续数分钟，期间不要重复启动第二条恢复流程。

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

安装器会升级私有虚拟环境并刷新 user service 文件。

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
--resume                           恢复当前目录最近的 thread
--thread-id UUID                   恢复明确指定的 thread
--safe                             不启用最高权限绕过
--no-attach                        后台启动，不立即进入 tmux
--cooldown-seconds N               两次恢复之间的冷却秒数
--max-recoveries N                 最大恢复次数，0 表示无限
--compact-wait-seconds N           等待真实压缩事件的超时
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
