# Contributing to LLM HUD

感谢你帮助改进 LLM HUD。项目仍处于 Alpha 阶段；提交改动时，请优先保持已有配置、状态文件和升级路径的兼容性。

## 开发环境

需要 Python 3.9 或更高版本。项目运行时只依赖 Python 标准库和源码内置的 Tomli，不要求创建虚拟环境；开发者可以按自己的习惯使用虚拟环境。

完整检查命令：

```bash
python3.9 -m unittest discover -s tests -t . -v
python3.9 -m pip install 'ruff==0.16.*'
python3.9 -m ruff check --no-cache src tests scripts
sh -n install.sh
python3.9 bin/llm-hud providers
```

涉及用户配置和安装状态的测试使用临时路径，不应修改本机真实的 Claude Code、Codex CLI 或 Kimi CLI 配置。

GitHub Actions 在 Linux 上覆盖 Python 3.9、3.11 和 3.14，在 macOS 上运行完整测试并用 Apple Command Line Tools 的 Python 3.9 做安装烟测，在原生 Windows 上覆盖 Python 3.9/3.13 的关键模块、安装失败恢复，以及通过 Git Bash 完成安装、升级、`doctor` 和解除集成。

## 项目结构

```text
install.sh                       Release 和本地源码安装入口
bin/llm-hud                      源码树命令入口
scripts/llm-hud-dispatcher       安装后固定的稳定分发器
scripts/runtime_control.py       独立于当前运行时的启动控制
scripts/bench_render.py          状态栏端到端基准
scripts/verify_distribution.py   wheel/sdist 内容检查
.github/workflows/               跨平台测试和 Release 工作流
src/llm_hud/
├── _platform.py                 Windows/POSIX 文件锁和权限差异
├── _tomllib.py                  标准库与内置 Tomli 的兼容入口
├── _vendor/tomli/               Python 3.9/3.10 使用的 TOML 解析器
├── _version.py                  唯一版本号来源
├── cli.py                       命令行入口
├── hud.py                       HUD 数据模型和 Claude 渲染器
├── installer.py                 安装、修复、清理和启动器管理
├── runtime.py                   版本化运行时与激活记录
├── paths.py                     配置和状态路径
├── storage.py                   文件、锁、快照和 JSON 操作
├── toml_edit.py                 保守修改 Codex TOML 配置
└── providers/
    ├── base.py                  provider 接口与共享事务协议
    ├── claude.py                Claude Code 集成
    ├── codex.py                 Codex CLI 原生状态栏集成
    └── kimi.py                  Kimi CLI 内置工具栏声明
tests/                           单元、安装事务和跨版本协议测试
```

## 修改原则

- 一个提交只处理一个逻辑问题，并让实现、回归测试和必要文档一起落地。
- 不要把 `scripts/runtime_control.py` 或 `scripts/llm-hud-dispatcher` 简单改为从当前 `llm_hud` 包导入实现。它们必须在分发前独立验证 active runtime，不能依赖尚未验证的包代码。
- 修改 runtime marker、activation、stable control、launcher state 或 provider state 前，先阅读 `tests/test_stable_control.py`、`tests/test_runtime.py` 和 `tests/test_state_abi.py` 中钉住的协议。
- provider 配置必须保守修改、保存恢复状态，并在检测到可观察的外部编辑时拒绝静默覆盖。
- 提交署名归维护者，不附加 AI 工具的署名行；仓库中的 `.claude/settings.json` 为使用 Claude Code 的贡献者固定了这一行为。
- Windows 支持是正式功能。涉及路径、换行、权限、原子替换、文件锁或安装事务的改动必须考虑原生 Windows 行为，并补充相应测试。

## 测试范围

根据改动选择定向测试，再运行完整测试。例如：

```bash
python3.9 -m unittest -v tests.test_claude tests.test_hud
python3.9 -m unittest -v tests.test_runtime tests.test_stable_control
python3.9 -m unittest -v tests.test_installer tests.test_installer_module
python3.9 -m unittest discover -s tests -t . -v
```

涉及安装器时还应运行 `sh -n install.sh`；涉及发布内容时运行：

```bash
python3.9 -m pip install 'build==1.3.*'
python3.9 -m build
python3.9 scripts/verify_distribution.py dist
```

`scripts/bench_render.py` 测量整个状态栏热路径。结果受固定解释器、硬件、文件系统和缓存影响，记录这些条件后再比较；不要从总耗时反推出解释器启动、导入、稳定控制校验或渲染中某一步的耗时。

## 提交问题

Bug 报告请附上：

- 操作系统及版本；
- Python、LLM HUD 和目标 CLI 版本；
- `llm-hud doctor` 输出；
- 最小复现步骤；
- 是否使用自定义安装目录、provider 配置目录或 Python。

不要提交 API key、访问令牌、完整私有配置或其他敏感数据。

维护者发布步骤和协议约束见 [`docs/maintainers.md`](docs/maintainers.md)。
