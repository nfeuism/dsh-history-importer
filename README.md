# DeepSeek Harness 对话导入器

将本机 **Codex** 和 **Hermes Agent** 的历史对话正文导入 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)，并按来源显示在两个项目中。

目前面向 **macOS / Windows、DeepSeek Harness 0.2.0-rc.2**。macOS 和 Windows 安装包由 GitHub Actions 分别构建；Windows 版尚未在真实 Harness 安装上实测。这是社区工具，不是 DeepSeek 官方功能。

## macOS：下载后双击使用

1. 先启动一次 DeepSeek Harness 完成初始化，再从应用菜单**完全退出**。
2. 在 [Releases 下载页](https://github.com/nfeuism/dsh-history-importer/releases/latest)选择对应压缩包：Apple 芯片下载 `macos-arm64.zip`，Intel 芯片下载 `macos-x86_64.zip`。
3. 解压后双击 `Start-Import.command`。输入 `1` 预览对话数量；输入 `2` 开始导入。完成后重新打开 Harness，在侧栏查看 **Codex 历史**、**Hermes 历史**。

压缩包自带程序，无需安装 Python、Homebrew 或 `zstd`。这是未使用 Apple 开发者证书公证的社区程序，macOS 首次打开时可能要求你在系统提示中确认；只从本仓库下载，并核对发布页上的文件名。若系统不允许启动，可使用下方源码运行方式。不要关闭 macOS 的安全保护。

## Windows：下载可执行文件

从 [Releases 下载页](https://github.com/nfeuism/dsh-history-importer/releases/latest)下载 `dsh-history-importer.exe`，在 PowerShell 中打开文件所在目录，运行 `./dsh-history-importer.exe` 预览，运行 `./dsh-history-importer.exe --import` 导入。无需安装 Python。Windows 构建状态见 [GitHub Actions](https://github.com/nfeuism/dsh-history-importer/actions/workflows/windows-build.yml)。

## 从源码运行

1. 先安装并启动一次 DeepSeek Harness，完成初始设置，然后**完全退出应用**。
2. 准备 Python 3.9 或更新版本。macOS 可安装 `zstd`：

   ```bash
   brew install zstd
   ```

3. 将本仓库下载或克隆到本机：

   ```bash
   git clone https://github.com/nfeuism/dsh-history-importer.git
   cd dsh-history-importer
   ```

Windows 和 macOS 都可安装 Python 压缩依赖，替代 `zstd` 命令：

```bash
python3 -m pip install -r requirements.txt
```

Windows PowerShell 中，将下文的 `python3` 换成 `py -3`；安装依赖用 `py -3 -m pip install -r requirements.txt`。

不需要提供账号密码或 API Key；程序只读取本机的历史文件。

## 使用

先预览可导入数量，不写任何文件：

```bash
python3 dsh_import.py
```

确认 DeepSeek Harness 已完全退出后，执行导入：

```bash
python3 dsh_import.py --import
```

结束后重新打开 DeepSeek Harness，在侧栏找 **Codex 历史** 和 **Hermes 历史**，点开一条记录应能看到用户提问与助手回答。

只导入一个来源：

```bash
python3 dsh_import.py --source codex --import
python3 dsh_import.py --source hermes --import
```

只导入最近 5 条，或检查已经导入的记录：

```bash
python3 dsh_import.py --source codex --limit 5 --import
python3 dsh_import.py --verify
```

`--limit` 是**每个来源**最近的 N 条。重复执行时，已有会话不会被覆盖或重复创建。

## 自定义数据位置

默认从 `~/.codex`、`~/.hermes/state.db` 读取，写到 `~/.dsh`。如数据不在默认位置：

```bash
python3 dsh_import.py --import \
  --codex-home /path/to/.codex \
  --hermes-db /path/to/state.db \
  --dsh-home /path/to/.dsh \
  --workspace-root /path/to/imported-projects
```

再次运行时，请保持同一个 `--workspace-root`。导入过的同一会话如果位于另一个目录，程序会停止，避免在 Harness 中造成重复 ID。

## 导入内容与安全性

- 只保留用户与助手的**文字正文**；工具调用、工具输出、系统提示与图片附件不会迁入。含图片的 Codex 消息会留下文字提示。**原项目的源码、工作区文件和附件不会迁移**；侧栏的两个项目只是导入记录的分组。
- Codex 多个 rollout 文件会按会话合并；Hermes 从 SQLite 在线快照读取。两种来源的数据均不修改。
- 每条对话写成 Harness v4 会话日志。标题和最近提问时间另写入会话缓存，便于侧栏显示；聊天正文含完整的回合结束字段，便于打开阅读。
- Harness 的 `workspace.json` 更新前会在同一目录生成 `workspace.json.bak.<时间>.<随机值>`。已有会话文件和缓存文件不会覆盖。导入中断后可重新运行。
- 程序只在本机工作，**不会上传对话、源文件或任何个人数据**。GitHub 仓库只有程序和说明。

这是一份**历史快照**。导入后继续提问会使用 DeepSeek Harness 当前模型和环境，不会恢复原工具执行状态。

## 遇到问题

- 提示缺少 Zstandard：运行 `python3 -m pip install -r requirements.txt`；macOS 也可运行 `brew install zstd`。
- 提示 Harness 正在运行：从应用菜单完全退出后重试。
- 预览结果为零：确认来源程序确实在默认位置保存了本地历史，或用自定义路径参数指定位置。
- 侧栏或聊天内容为空：先运行 `python3 dsh_import.py --verify`，再确认 Harness 版本是 0.2.0-rc.2。报告问题时不要公开提交聊天记录或 `~/.dsh` 整个目录。
- 想恢复项目登记文件：退出 Harness 后，选取导入前生成的 `workspace.json.bak...`，将其复制回 `workspace.json`；源程序的历史不受影响。会话日志会保留，避免无提示删除。

## 技术说明

Harness 通过两帧 Zstandard 压缩的 `session.v4.jsonl.zstd` 读取会话：第一帧为头记录，第二帧为事件。程序会在写入后解压检查两帧、事件顺序、消息展示字段和回合结束原因，再登记到项目中。文件格式与项目机制可能随 Harness 版本变化，因此目前将兼容范围限定为 0.2.0-rc.2。

## 许可

MIT，见 [LICENSE](LICENSE)。
