# LLM HUD

LLM HUD 是面向 AI 编程 CLI 的轻量状态栏配置器：为 Claude Code 渲染用量 HUD，为 Codex CLI 配置原生状态栏，并在检测到 Kimi CLI 时保留其信息更完整的内置工具栏。

它不启动后台进程，也不跨工具收集或混用数据。这里的 HUD 指 CLI 会话底部的状态栏，不是 iTerm2 状态栏、系统菜单栏或 Shell 提示符。

## 效果预览

Claude Code 通常显示双行 HUD：

```text
Claude · Opus · ~/projects/example
5h  ██░░░░░░░░   24% used  ↻ 14:30    7d  ████░░░░░░   41% used  ↻ Fri 09:00
```

Claude 的条形图表示已用比例，超过 70% 时变红。窄终端会自动换行或压缩路径。

Codex CLI 由自身渲染，LLM HUD 只选择并排列原生字段：

```text
gpt-5.6 xhigh · ~/projects/example · 5h 82% left · weekly 63% left · Context 98% left
```

Codex 的额度和上下文字段显示剩余比例。

## 支持与要求

| 工具 | LLM HUD 的动作 | 常驻信息 | 版本要求 |
| --- | --- | --- | --- |
| Claude Code | 安装本地状态栏命令 | 模型、目录、5 小时与 7 天额度 | 基础状态栏 ≥ 1.0.71；完整体验建议 ≥ 2.1.153 |
| Codex CLI | 配置原生 `[tui].status_line` | 模型、目录、5 小时与周额度、上下文 | ≥ 0.99.0 |
| Kimi CLI | 检测但不修改配置 | 保留 Kimi 内置工具栏信息 | LLM HUD 没有额外配置版本要求 |

运行 LLM HUD 需要 Python 3.9 或更高版本，并且至少一个受支持的 CLI 已安装且能从 `PATH` 找到。不需要 `pip` 或虚拟环境。

| 平台 | 要求 |
| --- | --- |
| macOS、Linux | 使用下面的安装命令 |
| WSL | LLM HUD 与目标 CLI 必须安装在 WSL 同一侧 |
| 原生 Windows | 安装 Git for Windows，并在 Git Bash 中执行安装和所有 `llm-hud` 管理命令；不需要 WSL |

PowerShell 和 CMD 直装目前不受支持。安装后的运行时和各 CLI 仍是原生 Windows 进程。

## 快速开始

从最新 GitHub Release 安装：

```bash
curl -fsSL https://github.com/codermali/llm-hud/releases/latest/download/install.sh | sh
```

安装器会下载与该安装脚本同版本的源码包和 SHA-256 清单，将版本化运行时安装到 `~/.local/share/llm-hud`，在 `~/.local/bin/llm-hud` 创建启动器，然后配置检测到的工具。

安装后执行：

```bash
~/.local/bin/llm-hud doctor
```

如果 `~/.local/bin` 已在 `PATH` 中，也可以直接运行 `llm-hud doctor`。安装器会在需要时打印 PATH 配置提示。`doctor` 会检查各工具的 `--version` 是否能正常执行以及相应集成是否已配置，但不会强制校验上表中的最低版本。

最后重新打开或刷新目标 CLI 会话。Claude 配额数据由 Claude Code 提供，仅在 Pro/Max 订阅账户首次收到响应后出现；没有配额数据时 HUD 只显示模型和目录。用量详情请使用 Claude Code 的 [`/usage`](https://code.claude.com/docs/en/commands)。

## 它会修改什么

- Claude Code：配置用户 `settings.json` 中的 `statusLine`，并保留安装前的值以便恢复。设置了 `CLAUDE_CONFIG_DIR` 时使用该目录。
- Codex CLI：保守修改用户 `config.toml` 中的 `[tui].status_line`，并保存原字段。设置了 `CODEX_HOME` 时使用该目录。
- Kimi CLI：不修改配置，继续使用 Kimi 自带的底部工具栏。
- LLM HUD：默认将运行时、启动器和恢复状态分别保存在 `~/.local/share/llm-hud`、`~/.local/bin/llm-hud` 和 `~/.config/llm-hud`。

默认 Release 安装会校验下载包的 SHA-256。运行时完成复制和验证后才会切换；运行时安装或激活阶段失败时会保留更新前的版本。随后各 provider 逐个配置，这一阶段不是跨 provider 的整体事务：如果其中一个失败，先前已经成功的配置会保留，安装命令返回非零状态。

LLM HUD 会在提交 provider 配置前再次检查可观察到的外部修改，检测到配置被同时修改时会拒绝覆盖。请避免在执行配置命令时用其他程序编辑同一文件；更完整的并发边界见[维护者文档](docs/maintainers.md#provider-配置安全)。

## 常用操作

`install.sh` 与 `llm-hud install` 的含义不同：前者安装或更新 LLM HUD 运行时，并在最后配置检测到的工具；后者只配置或重新配置 provider，不安装或更新程序。

| 目的 | 命令 |
| --- | --- |
| 检查工具与集成状态 | `llm-hud doctor` |
| 查看检测到的工具 | `llm-hud providers` |
| 重新配置所有已检测工具 | `llm-hud install` |
| 配置一个工具 | `llm-hud install --provider claude` 或 `--provider codex` |
| 更新到最新 Release | 重新运行快速开始中的安装命令 |
| 回到上一个已安装运行时 | `llm-hud rollback` |
| 恢复接入前的 provider 配置 | `llm-hud uninstall` |
| 只恢复一个 provider | `llm-hud uninstall --provider claude` 或 `--provider codex` |
| 查看帮助或版本 | `llm-hud --help`、`llm-hud --version` |

`rollback` 只在版本化安装存在上一运行时时切换 LLM HUD 版本，不修改 Claude Code 或 Codex CLI 配置。该命令由安装后的启动器所在的稳定控制层提供，因此不会出现在 `llm-hud --help` 的子命令列表中，也不能在源码目录里通过 `bin/llm-hud` 执行。

显式指定 `--provider` 时，即使对应 CLI 未被检测到，`llm-hud install` 也会照常写入该工具的配置文件，便于提前配置；不带 `--provider` 的 `llm-hud install` 只配置已检测到的工具。

`llm-hud uninstall` 的含义是解除集成：它恢复 Claude/Codex 接入前的配置，但不删除 LLM HUD 启动器、运行时或状态目录。当前没有完整删除程序的单一命令；如需彻底移除，请先执行 `llm-hud uninstall` 并确认 provider 配置已恢复，再删除默认的 `~/.local/bin/llm-hud` 文件、`~/.local/share/llm-hud` 目录和 `~/.config/llm-hud` 目录。使用自定义路径时，只删除 `${LLM_HUD_BIN_DIR}/llm-hud` 这个启动器文件，以及实际的 `LLM_HUD_INSTALL_DIR` 和 `LLM_HUD_STATE_DIR`；不要删除可能被其他程序共用的整个 bin 目录。

## 工具行为与已知限制

### Claude Code

Claude Code 把状态栏 JSON 交给 `llm-hud render claude`，LLM HUD 在本地读取模型、工作目录和可用的 `rate_limits` 窗口，不调用 provider API。两个额度窗口可能单独缺失，此时只显示存在的窗口。

如果安装前已有自定义状态栏，LLM HUD 会保留其配置并尝试委托原命令；原命令能启动、在 5 秒内结束并产生输出时会显示该输出，解除集成时会恢复原配置。

Claude Code [1.0.71](https://code.claude.com/docs/en/changelog#1-0-71) 首次提供自定义状态栏，[2.1.80](https://code.claude.com/docs/en/changelog#2-1-80) 首次提供 `rate_limits`，[2.1.153](https://code.claude.com/docs/en/changelog#2-1-153) 首次提供 `COLUMNS` 和 `LINES`。字段定义以 [Claude Code 状态栏文档](https://code.claude.com/docs/en/statusline)为准。

### Codex CLI

LLM HUD 配置以下原生字段；显示样式和刷新时间由 Codex CLI 决定：

```toml
[tui]
status_line = [
  "model-with-reasoning",
  "current-dir",
  "five-hour-limit",
  "weekly-limit",
  "context-remaining",
]
```

项目级 `.codex/config.toml` 或命令行选择的 profile 可能覆盖用户级配置，`doctor` 只检查用户级基础配置。这里使用的字段需要 Codex CLI [0.99.0](https://github.com/openai/codex/releases/tag/rust-v0.99.0) 或更高版本；当前定义以 [Codex 配置参考](https://developers.openai.com/codex/config-reference/)为准。

### Kimi CLI

LLM HUD 不接管 Kimi CLI 的工具栏，因为外部状态栏无法获得内置栏展示的全部信息。安装时 Kimi 显示为 `builtin`，解除集成时没有配置需要恢复。

Kimi Code 平台账户可以在 Kimi CLI 中使用 [`/usage`](https://moonshotai.github.io/kimi-cli/en/reference/slash-commands.html#usage) 查看配额；其他 provider 或 API key 不一定提供该命令的数据。

## 常见问题

### 安装后找不到 `llm-hud`

先使用完整路径运行 `~/.local/bin/llm-hud doctor`，再按安装器输出把 `~/.local/bin` 加入 Shell 的 `PATH`。自定义了 `LLM_HUD_BIN_DIR` 时使用对应目录。

### 没有检测到受支持的 CLI

运行时和启动器可能已经安装，但 provider 配置会失败并返回非零状态。安装 Claude Code、Codex CLI 或 Kimi CLI 后，运行 `llm-hud install` 重新配置。

### 配置操作报告冲突或错误

停止正在写同一配置文件的其他程序，检查当前配置，再重试。LLM HUD 只有在能够证明改动可以安全合并时才会继续：完整恢复到安装前状态会被识别，Codex 解除集成时也会保留后来新增的非受管状态栏字段；无法证明安全时会拒绝覆盖。

`llm-hud uninstall --forget` 只删除 LLM HUD 保存的恢复记录，不修改任何 provider 配置。它会放弃以后自动恢复原配置的能力，只应在确认不再需要该记录时使用。

## 高级安装

安装指定 Release：

```bash
curl -fsSL https://github.com/codermali/llm-hud/releases/download/vX.Y.Z/install.sh | sh
```

从本地源码目录安装：

```bash
./install.sh
```

安装器依次尝试 `python3.14` 到 `python3.9`，然后尝试 `python3` 和 `python`。通过远程安装指定解释器：

```bash
curl -fsSL https://github.com/codermali/llm-hud/releases/latest/download/install.sh \
  | LLM_HUD_PYTHON=/path/to/python3.9 sh
```

| 变量 | 用途 | 默认行为 |
| --- | --- | --- |
| `LLM_HUD_PYTHON` | 固定运行 LLM HUD 的 Python | 自动寻找 Python 3.9+ |
| `LLM_HUD_INSTALL_DIR` | 版本化运行时目录 | `$HOME/.local/share/llm-hud` |
| `LLM_HUD_BIN_DIR` | 启动器目录 | `$HOME/.local/bin` |
| `LLM_HUD_STATE_DIR` | provider 恢复状态目录 | 当前用户 home 下的 `.config/llm-hud`；设置 `LLM_HUD_HOME` 后随之改变 |
| `LLM_HUD_HOME` | provider 配置查找、默认状态目录和 HUD 路径缩写使用的 home | 当前用户 home；不改变 Shell 安装器从 `$HOME` 推导的运行时和启动器目录 |
| `LLM_HUD_TARBALL_URL` | 自定义源码包地址 | 与安装脚本同版本的 GitHub Release 包 |
| `LLM_HUD_CHECKSUM_URL` | 自定义 SHA-256 清单 | 默认包使用同一 Release 的清单；自定义 tarball 时必须显式设置，否则跳过校验 |

自定义 tarball 时应同时提供对应的 checksum URL；否则不会执行 SHA-256 校验。

## 开发与维护

项目当前处于 0.x / Alpha 阶段。开发环境、测试命令和项目结构见 [CONTRIBUTING.md](CONTRIBUTING.md)；版本化运行时、状态 ABI 和发布流程见 [维护者文档](docs/maintainers.md)。问题报告可提交到 [GitHub Issues](https://github.com/codermali/llm-hud/issues)，请附上操作系统、Python 版本、目标 CLI 版本、`llm-hud --version` 和 `llm-hud doctor` 输出。

## 许可证

本项目使用 [MIT License](LICENSE)。为兼容 Python 3.9 和 3.10，源码内置了同为 MIT 许可的 Tomli 2.2.1；其许可证位于 [`src/llm_hud/_vendor/tomli/LICENSE`](src/llm_hud/_vendor/tomli/LICENSE)。
