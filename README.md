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
5h  ██░░░░░░░░   24% used  ↻ 14:30    7d  ████░░░░░░   41% used  ↻ Fri 09:00
```

额度按“已用百分比”显示，与 Claude Code 自带的 `/status` 用量页同一口径：条形越满
表示用得越多，超过 70% 变红。

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

## 平台支持

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| macOS | ✅ 支持 | 主要开发平台；CI 覆盖 Python 3.9 安装和当前 Python 运行时 |
| Linux | ✅ 支持 | CI 在 Ubuntu 上覆盖最低版本、关键边界和较新 Python 版本 |
| WSL | ✅ 支持 | 等同 Linux；llm-hud 必须与 CLI 工具装在 WSL 同一侧 |
| 原生 Windows | ✅ 支持 | 需要 Git for Windows；安装和管理命令在 Git Bash 中运行 |

原生 Windows 适配本身不需要 WSL，但安装、更新和 `llm-hud` 管理命令目前必须在
Git Bash 中运行；PowerShell/CMD 直装尚未提供。运行时和各 CLI 仍是原生 Windows
进程，现有版本化安装、原子切换和 `rollback` 都会保留。Git Bash 也符合上游行为：
Claude Code 在 Windows 上优先用它运行状态栏命令，Kimi CLI 目前同样要求 Git for
Windows。

## 安装

需要 Python 3.9 或更高版本。原生 Windows 还需要安装 Git for Windows，并在 Git
Bash 中执行本节命令；不要在 PowerShell 或 CMD 中直接运行 `install.sh`。

下面的命令会安装最新的 GitHub Release，使用的安装脚本与该 Release 一同发布、
互相配套：

```bash
curl -fsSL https://github.com/codermali/llm-hud/releases/latest/download/install.sh | sh
```

仓库 main 分支上的 `install.sh`（raw.githubusercontent.com 地址）同样可用，
但它可能领先于最新 Release。

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

Python 的检测顺序为 `python3.14`、`python3.13`、`python3.12`、`python3.11`、
`python3.10`、`python3.9`、`python3`、`python`（最后一项主要用于 Windows）。
如需指定解释器：

```bash
LLM_HUD_PYTHON=/path/to/python3.9 ./install.sh
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

重新运行同一条安装命令即可更新到最新发布版本。安装器会先比较版本：检测到旧版本
时直接原子升级并打印提示；已安装版本与要安装的相同时会询问是否重新安装（没有终
端时默认继续，保证自动化可用）；要安装的版本更旧时会作为降级询问确认。

升级过程先校验下载包的 SHA-256，再复制并验证新运行时；全部通过后才切换当前版
本。安装失败时会继续使用更新前的版本，提供方配置也不会因为运行时更新而被重置。

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
llm-hud uninstall --forget
llm-hud rollback
```

- `install` 可重复执行，不会重复添加相同配置。
- `doctor` 检查工具是否已安装、集成是否已配置，以及启动器是否可执行。
- `uninstall` 恢复接入前的提供方状态栏配置；它不会删除 LLM HUD 运行时。
- `install` 和 `uninstall` 会在写入前再次比对配置目标与内容；如果此时已观察到
  读取后的变更，会以 `conflict` 状态拒绝覆盖并返回非零退出码。该检查会缩小外部
  编辑器的竞争窗口，但不是严格的跨平台 CAS：provider 锁只串行化 LLM HUD 自身
  的操作，最终检查与原子重命名之间仍可能发生外部写入。
- 如果用户删除了 LLM HUD 的配置或手动恢复了原状，`install` 会直接重新配置，
  `uninstall` 会清理遗留的安装状态。
- `uninstall --forget` 只放弃保存的恢复记录，不改动任何提供方配置。

## 各工具的行为

### Claude Code

Claude Code 将状态栏 JSON 传给 `llm-hud render claude`。LLM HUD 从中读取模型、
工作目录和当前 `rate_limits` 窗口，宽度取自 Claude Code 设置的 `COLUMNS` 环境
变量。如果原来已有自定义状态栏，LLM HUD 会保留其输出（包括 `refreshInterval`
等字段），并在卸载时恢复原配置。设置了 `CLAUDE_CONFIG_DIR` 时，LLM HUD 也会在
该目录中查找 `settings.json`。在 Windows 上，写入 Claude 配置的启动器路径使用
正斜杠；保留的原状态栏也继续交给 Git Bash 执行。

上游兼容边界：Claude Code [1.0.71](https://code.claude.com/docs/en/changelog#1-0-71)
首次提供自定义状态栏，[2.1.80](https://code.claude.com/docs/en/changelog#2-1-80)
首次向状态栏脚本提供 `rate_limits`，
[2.1.153](https://code.claude.com/docs/en/changelog#2-1-153) 首次提供 `COLUMNS`
和 `LINES`。LLM HUD 不强制检查版本；要同时获得上述配额和终端宽度数据，请使用
Claude Code 2.1.153 或更高版本。输入字段以
[Claude Code 状态栏文档](https://code.claude.com/docs/en/statusline)为准。

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

上游兼容边界：Codex CLI
[0.99.0](https://github.com/openai/codex/releases/tag/rust-v0.99.0) 是首个发布
`[tui].status_line` 的稳定版本，并包含这里使用的五个字段。LLM HUD 不强制检查
版本；请使用 Codex CLI 0.99.0 或更高版本，并以
[Codex 配置参考](https://developers.openai.com/codex/config-reference/)
中的当前定义为准。

### Kimi CLI

LLM HUD 当前保留 Kimi CLI 自带的底部工具栏。原因是外部状态栏命令不能获得内置栏
展示的全部信息；强行接管会丢失 Git 状态、模式、目标或后台任务等内容。配额进度和
重置时间继续通过 Kimi CLI 内的 `/usage` 查看。

安装时 Kimi 会显示为 `builtin`，不会修改其配置，因此卸载时也没有需要恢复的内容。
这项集成不依赖 Kimi 的配置接口，所以没有由 LLM HUD 引入的配置功能最低版本；
`/usage` 的行为以
[Kimi Code CLI 命令文档](https://moonshotai.github.io/kimi-cli/en/reference/slash-commands.html#usage)
为准。

## 项目结构

```text
install.sh                       一键安装入口
bin/llm-hud                      源码树命令入口
scripts/llm-hud-dispatcher       安装后固定的稳定分发器
scripts/runtime_control.py       独立于当前版本的启动与回退控制
src/llm_hud/
├── _platform.py                 Windows/POSIX 文件锁和权限差异
├── _tomllib.py                  标准库与内置 Tomli 的兼容入口
├── _vendor/tomli/               Python 3.9/3.10 使用的 TOML 解析器
├── _version.py                  唯一版本号来源
├── cli.py                       命令行入口
├── hud.py                       通用 HUD 数据模型和渲染器
├── installer.py                 安装、更新和启动器管理
├── runtime.py                   版本化运行时与原子切换
├── paths.py                     配置和状态路径
├── storage.py                   原子文件与 JSON 操作
├── toml_edit.py                 保守修改 Codex TOML 配置
└── providers/
    ├── base.py                  提供方接口
    ├── claude.py                Claude Code 集成
    ├── codex.py                 Codex CLI 原生状态栏集成
    └── kimi.py                  Kimi CLI 内置工具栏集成
```

## 开发与测试

开发同样需要 Python 3.9 或更高版本。建议明确指定可用版本：

```bash
python3.9 -m unittest discover -s tests -t . -v
python3.9 -m pip install 'ruff==0.16.*'
python3.9 -m ruff check --no-cache src tests scripts
sh -n install.sh
python3.9 bin/llm-hud providers
```

测试使用临时的 HOME、配置和状态路径，不会修改本机真实的 Claude Code 或 Codex CLI
配置。GitHub Actions 在 Linux、macOS 和原生 Windows 上覆盖 Python 3.9
最低版本、关键边界和较新 Python 版本，并在 Windows 上覆盖安装
失败恢复和运行时并发锁，并通过 Git Bash 完成安装、升级、`rollback`、`doctor` 与
卸载烟测。

状态栏热路径有端到端基准脚本 `scripts/bench_render.py`（用法见脚本注释）。结果会显著
受安装时固定的 Python、硬件、文件系统和缓存状态影响；记录这些条件后再比较数据。
该脚本不拆分解释器启动、稳定控制层校验、模块导入和渲染各自的耗时，因此不应从总
耗时反推出其中某一步的开销。

## 发布

维护者先更新 `src/llm_hud/_version.py`，再把同版本的 `vX.Y.Z` 标签推送到 GitLab；
标签由仓库镜像同步到 GitHub。Release 工作流会核对标签与代码版本、运行测试，并
发布 `llm-hud.tar.gz`、`install.sh` 和 `SHA256SUMS`。创建标签和正式发布仍由维护者
主动决定。

提供方状态文件带有 schema 版本号。`rollback` 只切换运行时、不迁移状态，因此每个
版本都必须能读取上一个发布版本写出的 schema；写出的 schema 由
`tests/test_state_abi.py` 钉住，升级 schema 必须同步修改该测试并保留旧 schema 的
读取能力。

## 许可证

本项目使用 [MIT License](LICENSE)。
为兼容 Python 3.9 和 3.10，源码内置了同为 MIT 许可的 Tomli 2.2.1；其许可证保留在
[`src/llm_hud/_vendor/tomli/LICENSE`](src/llm_hud/_vendor/tomli/LICENSE)。
