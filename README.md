# LLM HUD

LLM HUD 为受支持的 AI 编程命令行工具配置简洁的终端状态栏。它不会创建
iTerm2 状态栏、macOS 菜单栏、Shell 提示符或后台进程。

## 支持情况

| 工具 | 集成方式 | 常驻显示 | 按需查看 |
| --- | --- | --- | --- |
| Claude Code | 自定义状态栏命令 | 模型、目录、5 小时和 7 天额度 | — |
| Codex CLI | 原生状态栏字段 | 模型、目录、5 小时与周额度、上下文 | — |
| Kimi CLI | 保留内置工具栏 | 模型、目录、Git、任务、上下文 | 配额（`/usage`） |

每个集成只处理对应工具自己的数据，不检测进程，也不混用不同提供方的状态。

## 显示效果

Claude Code 支持外部状态栏命令，因此可以显示完整的双行 HUD：

```text
Claude · Opus · ~/projects/example
5h  ████████░░  76%  ↻ 14:30    7d  ██████░░░░  59%  ↻ Fri 09:00
```

额度数据由 Claude Code 提供，只在 Claude Pro/Max 订阅账户上出现，并且要等到会话的
首次响应之后。没有额度数据时（首次响应之前，或使用 API key 等不提供额度的账户），
HUD 只显示第一行；两个额度窗口也可能各自缺失，此时只显示存在的窗口：

```text
Claude · Opus · ~/projects/example
```

Codex CLI 由自身负责渲染，LLM HUD 只选择和排列原生字段：

```text
gpt-5.6 xhigh · ~/projects/example · 5h 82% left · weekly 63% left · Context 98% left
```

## 安装

需要 Python 3.11 或更高版本。

下面的命令会安装最新的 GitHub Release：

```bash
curl -fsSL https://raw.githubusercontent.com/codermali/llm-hud/main/install.sh | sh
```

也可以从本地源码目录安装：

```bash
./install.sh
```

安装器默认执行以下操作：

- 将版本化运行时安装到 `~/.local/share/llm-hud`；
- 在 `~/.local/bin/llm-hud` 创建固定使用已检测 Python 的启动器；
- 检测 Claude Code、Codex CLI 和 Kimi CLI；
- 只配置已检测到且需要配置的工具；
- 将恢复配置所需的状态保存到 `~/.config/llm-hud`。

Python 的检测顺序为 `python3.13`、`python3.12`、`python3.11`、`python3`。
如需指定解释器：

```bash
LLM_HUD_PYTHON=/path/to/python3.11 ./install.sh
```

还可以使用以下环境变量：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `LLM_HUD_INSTALL_DIR` | 运行时安装目录 | `~/.local/share/llm-hud` |
| `LLM_HUD_BIN_DIR` | 命令安装目录 | `~/.local/bin` |
| `LLM_HUD_TARBALL_URL` | 自定义源码包地址 | 最新 GitHub Release |
| `LLM_HUD_CHECKSUM_URL` | 自定义 SHA-256 清单地址 | 最新 Release 的 `SHA256SUMS` |

自定义 `LLM_HUD_TARBALL_URL` 时，建议同时提供对应的
`LLM_HUD_CHECKSUM_URL`；后者未设置时，自定义下载包不会执行 SHA-256 校验。

## 更新与回退

重新运行同一条安装命令即可更新到最新发布版本。安装器会先校验下载包的 SHA-256，
再复制并验证新运行时；全部通过后才切换当前版本。安装失败时会继续使用更新前的
版本，提供方配置也不会因为运行时更新而被重置。

需要回到上一个已安装版本时运行：

```bash
llm-hud rollback
```

`rollback` 只切换 LLM HUD 运行时，不修改 Claude Code 或 Codex CLI 的配置。

## 常用命令

```bash
llm-hud providers
llm-hud install
llm-hud install --provider claude
llm-hud install --provider codex
llm-hud doctor
llm-hud uninstall
llm-hud rollback
```

- `install` 可重复执行，不会重复添加相同配置。
- `doctor` 检查工具是否已安装、集成是否已配置，以及启动器是否可执行。
- `uninstall` 恢复接入前的提供方状态栏配置；它不会删除 LLM HUD 运行时。
- 如果用户在安装后自行修改了相关配置，卸载会拒绝覆盖这些修改。

## 各工具的行为

### Claude Code

Claude Code 将状态栏 JSON 传给 `llm-hud render claude`。LLM HUD 从中读取模型、
工作目录和当前 `rate_limits` 窗口。如果原来已有自定义状态栏，LLM HUD 会保留其
输出，并在卸载时恢复原配置。

### Codex CLI

Codex CLI 不使用外部状态栏渲染器。LLM HUD 配置以下原生
`[tui].status_line` 字段：

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

显示样式和刷新时间由 Codex CLI 决定。项目级 `.codex/config.toml` 或命令行选择的
profile 可能覆盖用户级配置，`doctor` 目前只检查用户级基础配置。

### Kimi CLI

LLM HUD 当前保留 Kimi CLI 自带的底部工具栏。原因是外部状态栏命令不能获得内置栏
展示的全部信息；强行接管会丢失 Git 状态、模式、目标或后台任务等内容。配额进度和
重置时间继续通过 Kimi CLI 内的 `/usage` 查看。

安装时 Kimi 会显示为 `builtin`，不会修改其配置，因此卸载时也没有需要恢复的内容。

## 项目结构

```text
install.sh                    一键安装入口
scripts/runtime_control.py    独立于当前版本的启动与回退控制
src/llm_hud/
├── cli.py                    命令行入口
├── hud.py                    通用 HUD 数据模型和渲染器
├── installer.py              安装、更新和启动器管理
├── runtime.py                版本化运行时与原子切换
├── paths.py                  配置和状态路径
├── storage.py                原子文件与 JSON 操作
├── toml_edit.py              保守修改 Codex TOML 配置
└── providers/
    ├── base.py               提供方接口
    ├── claude.py             Claude Code 集成
    ├── codex.py              Codex CLI 原生状态栏集成
    └── kimi.py               Kimi CLI 内置工具栏集成
```

## 开发与测试

开发同样需要 Python 3.11 或更高版本。macOS 自带的 `python3` 可能较旧，建议明确
指定可用版本：

```bash
python3.12 -m unittest discover -s tests -t . -v
python3.12 bin/llm-hud providers
```

测试使用临时的 HOME、配置和状态路径，不会修改本机真实的 Claude Code 或 Codex CLI
配置。

## 发布

维护者先更新 `src/llm_hud/_version.py`，再把同版本的 `vX.Y.Z` 标签推送到 GitHub。
Release 工作流会核对标签与代码版本、运行测试，并发布 `llm-hud.tar.gz` 和
`SHA256SUMS`。创建标签和正式发布仍由维护者主动决定。

## 许可证

本项目使用 [MIT License](LICENSE)。
