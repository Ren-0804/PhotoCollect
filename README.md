# PhotoCollect

PhotoCollect 是一个 Windows 桌面应用（PySide6），用于按箱号采集和整理集装箱照片：每个箱号是一个任务，软件识别箱号和箱型、检查照片是否齐全，并导出交接包和报表。正式发布物是 PyInstaller 单文件 `PhotoCollect.exe`，通过群晖局域网更新中心推送给同事。

当前版本：**v5.16.17**（版本号以 `con.py` 中的 `APP_VERSION` 为准）。

## 功能

- **任务列表**：以箱号为单位管理照片，显示完成、待补、异常状态，支持多选批量操作；大量任务时只构建可见行，界面保持流畅。
- **OCR**：RapidOCR（ONNX Runtime）识别箱号和箱型，并按 ISO 6346 实时检查校验位；支持收集后批量识别。
- **导出**：ZIP 交接包和 Excel 报表，文件名可按模板生成。
- **软件内更新**：启动时、每 5 分钟、以及收到 ntfy 推送时检查群晖上的 `latest.json`；后台下载并校验大小和 SHA-256；退出后由 PowerShell 安装助手原子替换 EXE。新版本没有按时报告启动健康时，自动回滚并重新打开旧版本，下次启动显示失败原因和日志位置。
- **授权**：启动时向授权服务检查，使用带 Ed25519 签名的租约支持离线宽限。服务端代码不在本仓库。

## 环境

- Windows 10/11 x64
- Python 3.13
- 运行依赖：PySide6、rapidocr-onnxruntime、openpyxl、psutil、pywin32、qtawesome
- 构建和测试：PyInstaller、pytest

```bash
pip install PySide6 rapidocr-onnxruntime openpyxl psutil pywin32 qtawesome pyinstaller pytest
```

当前验证过的版本：PySide6 6.10.1、rapidocr-onnxruntime 1.2.3、onnxruntime 1.23.2、PyInstaller 6.21.0、pytest 9.1.1。

## 获取代码

主程序文件名 `con.py` 以 Windows 保留设备名 `CON` 开头，Git for Windows 默认会拒绝检出它。在 Windows 上克隆时需要关闭这项检查：

```bash
git clone -c core.protectNTFS=false https://github.com/Ren-0804/PhotoCollect.git
```

## 运行

```bash
python con.py
```

## 测试

```bash
python -m unittest discover -s tests
python -m pytest tests\test_authorization_service.py
```

`unittest discover` 不会执行 pytest 风格的模块级测试函数，所以发布流水线会额外跑 pytest。`tests/test_acceptance_workflow.py` 里有一条守卫测试：如果新增的测试文件含模块级 `test_` 函数却没有加入流水线的 pytest 命令，完整测试会失败。

## 构建与发布

1. 修改 `con.py` 中的 `APP_VERSION`。
2. 运行 `一键构建_PhotoCollect.bat`，依次执行：
   1. 完整自动测试（unittest + pytest）
   2. 源码隔离验收，包括固定样图 OCR
   3. PyInstaller 单文件干净构建
   4. EXE 隔离验收，OCR 结果必须与固定样图完全一致
   5. 原子部署到 `dist\` 和桌面
3. 运行 `publish_to_synology.ps1`：上传已验收的 EXE 到群晖，经 HTTP 回读核对 SHA-256 后提交 `latest.json`，再发送 ntfy 通知。发布令牌只保存在发布电脑上，由 Windows DPAPI 加密；客户端不内置任何写入令牌。

`一键构建并推送更新.bat` 会依次执行上面两步。更新中心只服务可信局域网，不开放到公网。

## 目录

| 路径 | 内容 |
| --- | --- |
| `con.py` | 主程序和界面 |
| `updater_services.py` | 更新检查、下载校验、安装计划和 PowerShell 安装助手 |
| `transition_bridge.py`、`transition_bridge_*.cs` | 从旧版本安全迁移的过渡桥 |
| `authorization_service.py`、`authorization_config.py`、`device_identity.py` | 授权客户端 |
| `ocr_services.py`、`metadata_services.py`、`image_import_services.py` | OCR、照片元数据和导入 |
| `storage_services.py`、`report_services.py`、`export_naming.py` | 交接包、报表和命名模板 |
| `task_services.py`、`health_services.py`、`theme_services.py` | 任务状态、数据检查和主题 |
| `acceptance_workflow.py` | 源码和 EXE 的隔离验收 |
| `publish_to_synology.ps1`、`*.bat` | 构建和发布脚本 |
| `tests/` | 自动测试和固定 OCR 样图 |
| `lessons/`、`reference/`、`learning-records/` | 更新中心的搭建说明和验证记录 |

更完整的版本历史和使用说明见 `PhotoCollect_改造全记录与使用手册.md`，项目目标见 `MISSION.md`。

## 最近更新

**v5.16.17**

- 更新安装助手写日志时允许其他程序同时读取日志。以前日志被杀毒软件或记事本打开时，可能导致更新中断、旧版本没有重新打开，或者回滚失败。这个保护从 v5.16.17 发起的下一次更新开始生效。
- 发布流水线新增 pytest 步骤，授权测试不再被跳过。
- 修复两个偶发失败的测试：更新助手锁文件测试和任务列表渲染耗时测试。
