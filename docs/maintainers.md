# LLM HUD 维护者文档

本文记录不能由普通用户文档承担的架构边界、兼容性合同和发布流程。用户安装与日常操作见仓库根目录的 [`README.md`](../README.md)，开发命令见 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。

## 运行时架构

托管安装由三个层次组成：

1. PATH 中的外部启动器固定安装时选定的 Python，避免 Claude Code、Codex CLI 等精简环境改变解释器选择。
2. 安装根目录中的稳定 dispatcher 和 `runtime_control.py` 读取 activation，验证目标 runtime，再把命令交给 active release。
3. `versions/<release-id>` 保存不可变的版本化 runtime；activation 记录 active 和 previous，用于升级与 `rollback`。

稳定控制层必须独立于 active runtime。即使当前包损坏，它仍要能够验证 previous runtime 并完成回退。因此 `scripts/runtime_control.py` 与 `src/llm_hud/runtime.py`、`scripts/llm-hud-dispatcher` 与包内校验逻辑之间存在有意的重复；不要为了减少代码量而让稳定层导入 active package。行为一致性由 runtime、stable-control 和 ABI 测试维护。

安装器会先完成源码复制、digest 和布局验证，再更新 activation。稳定工具、dispatcher smoke、外部启动器和 provider 配置属于后续阶段：

- activation、稳定工具和外部启动器的受控失败路径在同一 RuntimeLock 事务中恢复；
- provider 配置在运行时安装完成后逐个执行，不是跨 provider 的全局事务；
- inactive runtime 清理使用带身份记录的隔离目录，Windows 删除失败可以留待后续清理；
- 损坏的同版本 runtime 修复使用独立 repair-backup record，进程中断后不得被普通 prune 当作垃圾删除；
- staging 目录只由创建者按 inode 身份清理，不通过 mtime 猜测其他进程是否已经死亡。

这些边界是安全合同，不应为“简化”安装器而弱化。

## 磁盘与状态 ABI

以下格式跨版本存在，并由测试钉住：

- activation 的 active/previous 关系；
- install ownership/layout marker、runtime marker 与 release id；
- stable dispatcher 与 runtime-control 的固定位置和接口；
- launcher state；
- runtime trash 和 repair-backup records；
- Claude/Codex provider state 与 transaction journal。

`rollback` 只切换 runtime，不迁移 provider state。因此兼容性必须同时成立：当前版本能读取上一个发布版本写出的 schema，并且上一个发布版本能读取当前版本写出的 schema。否则升级成功后再执行 `rollback`，旧 runtime 会无法处理磁盘上的新状态。

当前合同由 `tests/test_state_abi.py` 固定：

- 上一个发布版本是 v0.3.2；
- Claude 读取 schema 1、2，写 schema 2；
- Codex 读取并写 schema 1；
- provider transaction journal 使用 schema 1。

每次发布后都要推进测试中的 `PREVIOUS_RELEASE_*` 固定值，即使 schema 没有变化。提升当前写出 schema 前，必须确认上一个正式版本已经能读取它；未知的未来 schema 必须让 HUD 渲染热路径继续工作，同时让常规 `install`/`uninstall` 配置操作 fail closed 并保留原文件。显式的 `uninstall --forget` 是由用户决定放弃恢复记录的例外。

`tests/fixtures/runtime_v0_2_0` 是真实 v0.2.0 runtime 夹具。它的内容、文件模式和 digest 属于历史协议，不能按当前代码格式化或补丁；只有夹具外的说明和兼容测试可以更新。

Windows 无法可靠保留 POSIX executable bit。runtime digest 只在 Windows 对协议中固定的 `bin/llm-hud` 使用规范 executable 标志，package validator 与冻结 stable control 必须保持同构。涉及 marker 大小、换行或精确回滚快照时，还要区分文本 ABI 的换行归一化与 binary snapshot 的逐字节语义。

## Provider 配置安全

Claude 与 Codex 的安装状态记录原配置、安装后配置和 schema。安装/解除集成遵循以下原则：

- 只修改受管字段，保留无关 JSON/TOML 内容和可保留的格式；
- provider 操作通过每个 state 文件的锁串行化 LLM HUD 自身操作；
- 写 provider 配置前比较初始目标和内容，发现可观察到的外部改动时拒绝覆盖；
- 普通跨平台文件系统没有让不合作的外部编辑器参与同一原子 CAS 的机制，因此不能宣称完全消除了最终检查到 `os.replace` 之间的竞争窗口；
- transaction journal 用于恢复 LLM HUD 自身在 state/config 两步写入间发生的受控中断。

Claude 可能委托安装前已有的 status-line 命令，但该命令是不受信任的外部进程，必须保留超时和失败隔离。Codex TOML 修改保持 fail-closed：无法证明只改变目标字段时宁可拒绝写入。

## 发布前检查

版本号唯一来源是 `src/llm_hud/_version.py`。发布前：

1. 确认工作树干净，当前 main 已包含全部准备发布的独立提交。
2. 更新版本号并同步用户文档中的发布状态；不要修改历史 fixture 中的版本。
3. 运行完整测试、Ruff、Shell 语法和发行内容检查：

   ```bash
   python3.9 -m unittest discover -s tests -t . -v
   python3.9 -m ruff check --no-cache src tests scripts
   sh -n install.sh
   python3.9 -m pip install 'build==1.3.*'
   python3.9 -m build
   python3.9 scripts/verify_distribution.py dist
   ```

4. 把版本号和对应发布文档作为一个独立提交提交，并再次确认工作树干净、`HEAD` 正是准备打标签的提交。
5. 把 main 只推送到 GitLab `origin`，确认 GitLab main 到达目标提交。
6. 等待 GitLab 的 push mirror 把 main 同步到 GitHub，并确认该提交的 GitHub test workflow 全绿。
7. 创建与代码版本完全一致、精确指向该提交的 annotated tag，例如 `vX.Y.Z`，只推送到 GitLab：

   ```bash
   git tag -a vX.Y.Z -m "LLM HUD X.Y.Z"
   git push origin refs/tags/vX.Y.Z
   ```

8. 确认镜像把同一 tag object 同步到 GitHub。不要在镜像尚未完成或报错时绕过既定流程直接推 GitHub。
9. GitHub release workflow 成功后，检查 Release、latest 链接和三个发布资产。

Release workflow 会再次核对 tag 与包版本，复用完整 test workflow，构建一次 `llm-hud.tar.gz`、带内嵌 tag 的 `install.sh` 和 `SHA256SUMS`，在 Linux 和原生 Windows Git Bash 中安装同一组资产，最后创建 GitHub Release。

下载后可在临时目录复验：

```bash
sha256sum --check SHA256SUMS
tar -xzf llm-hud.tar.gz
python3.9 -I -B llm-hud-X.Y.Z/bin/llm-hud --version
```

macOS 默认没有 `sha256sum` 时，可使用 `shasum -a 256 -c SHA256SUMS`。

正式 Release 的 `install.sh` 内嵌 tag；使用默认下载地址时，它必须只下载同一 tag 的源码包和校验清单。`LLM_HUD_TARBALL_URL` 和 `LLM_HUD_CHECKSUM_URL` 仍可显式覆盖地址；仓库 main 中的模板脚本才允许动态解析 latest。

## 发布说明

每个 Release 至少说明：

- 一句话摘要；
- Added / Changed / Fixed；
- 用户可见的兼容性变化；
- 升级与 `rollback` 注意事项；
- 已知问题。

版本历史以 GitHub Releases 为准。只有在确定会持续维护时才增加独立 `CHANGELOG.md`，避免两份历史漂移。
