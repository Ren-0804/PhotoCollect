# PhotoCollect

PhotoCollect 是一个 Windows 桌面应用（PySide6），用于按箱号采集和整理集装箱照片：每个箱号是一个任务，软件识别箱号和箱型、检查照片是否齐全，并导出交接包和报表。正式发布物是 PyInstaller 单文件 `PhotoCollect.exe`，通过群晖局域网更新中心推送给同事。

当前版本：**v5.16.19**（版本号以 `con.py` 中的 `APP_VERSION` 为准）。

## 功能

- **任务列表**：以箱号为单位管理照片，显示完成、待补、异常状态，支持多选批量操作；大量任务时只构建可见行，界面保持流畅。
- **OCR**：RapidOCR（ONNX Runtime）识别箱号和箱型，并按 ISO 6346 实时检查校验位；支持收集后批量识别。
- **自动分类**：箱号详情页上方有一个拖入区，拖进去的照片由一个小型图像分类模型判断属于 1-5 类中的哪一类（空箱、半箱、满箱、半关门满箱、全关门），有把握的直接放进对应方框，没把握的弹窗让人选；看起来是 6-8 类的照片提示手动放。方框已有照片时询问追加、替换（旧照片进最近删除，可撤销）还是跳过。原来往各个方框里拖放、选择图片、Ctrl+V 的操作不变。
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
   2. 源码隔离验收，包括固定样图 OCR 和自动分类
   3. PyInstaller 单文件干净构建
   4. EXE 隔离验收，OCR 结果必须与固定样图完全一致，自动分类模型必须已打包并把样图判为关门照片
   5. 原子部署到 `dist\` 和桌面
3. 运行 `publish_to_synology.ps1`：上传已验收的 EXE 到群晖，经 HTTP 回读核对 SHA-256 后提交 `latest.json`，再发送 ntfy 通知。发布令牌只保存在发布电脑上，由 Windows DPAPI 加密；客户端不内置任何写入令牌。

`一键构建并推送更新.bat` 会依次执行上面两步。更新中心只服务可信局域网，不开放到公网。

### 自动分类模型

模型用已经分好类的客户照片训练，所以 `models/` 不进公开仓库（已在 `.gitignore` 中）。构建时 `PhotoCollect.spec` 从本机 `models/photo_classifier.onnx` 和 `models/photo_classifier.json` 打包，缺少时构建直接失败。源码运行缺少模型时，拖入区显示为不可用，其余功能照常。

重新训练需要 torch 和 torchvision（只在训练电脑上，不进 EXE）。工作目录放在仓库外面，`roots.txt` 每行一个已分好类的工作目录：

```bash
python tools/train_photo_classifier.py manifest WORK --roots-file WORK/roots.txt --holdout 项目A --holdout 项目B
python tools/train_photo_classifier.py cache WORK
python tools/train_photo_classifier.py train WORK --epochs 12
python tools/train_photo_classifier.py evaluate WORK --split test
```

标签取自保存的文件名（`箱号-类别[_序号].jpg`），6-8 类合并为“其他”。同一箱号的照片只会落在训练、验证、测试其中一份；`--holdout` 指定的项目整体留作测试。`train` 用验证集选出“有把握”的置信度门槛（自动放入的照片至少 99% 正确），写进 `photo_classifier.json`。训练完把 `WORK/models/` 下两个文件复制到仓库的 `models/`。

## 目录

| 路径 | 内容 |
| --- | --- |
| `con.py` | 主程序和界面 |
| `updater_services.py` | 更新检查、下载校验、安装计划和 PowerShell 安装助手 |
| `transition_bridge.py`、`transition_bridge_*.cs` | 从旧版本安全迁移的过渡桥 |
| `authorization_service.py`、`authorization_config.py`、`device_identity.py` | 授权客户端 |
| `ocr_services.py`、`metadata_services.py`、`image_import_services.py` | OCR、照片元数据和导入 |
| `photo_classifier.py`、`tools/train_photo_classifier.py` | 照片自动分类（运行时推理）和模型训练脚本 |
| `storage_services.py`、`report_services.py`、`export_naming.py` | 交接包、报表和命名模板 |
| `task_services.py`、`health_services.py`、`theme_services.py` | 任务状态、数据检查和主题 |
| `acceptance_workflow.py` | 源码和 EXE 的隔离验收 |
| `publish_to_synology.ps1`、`*.bat` | 构建和发布脚本 |
| `tests/` | 自动测试和固定 OCR 样图 |
| `lessons/`、`reference/`、`learning-records/` | 更新中心的搭建说明和验证记录 |

更完整的版本历史和使用说明见 `PhotoCollect_改造全记录与使用手册.md`，项目目标见 `MISSION.md`。

## 最近更新

**v5.16.19**

- 箱号详情页上方新增“拖到这里自动分类”区域：拖进去的照片会自动判断是空箱、半箱、满箱、半关门满箱还是全关门，并放进对应方框。没把握时弹窗显示最可能的几类让人点选；加固/底梁、铅封这类 6-8 类照片会提示手动放。方框里已有照片时会询问追加、替换（旧照片进最近删除，可撤销）还是跳过。原来往各个方框里拖放、添加图片、Ctrl+V 的操作不变。
- 自动分类在 116 个未参与训练的箱子（707 张照片）上测试，准确率 99.4%；每张照片判断约 15 毫秒，只用 CPU。

**v5.16.18**

- 识别箱门铭牌时，最大总重、自重、载重只取 KG 那一行，容积只取 CU.M 那一行，不再把 LBS 或 CU.FT 的数值当成公斤或立方米保存。单位和数字分成两块、点号作千分位、数字被拆开或少读一位时也能读对。已经识别过的箱子要点“重新识别”才会更新。
- 箱号识别更稳：按文字行分组，方框里的校验位会单独再读一次，与 ISO 6346 校验位一致才算读到；同一箱另一张关门照片读到的校验位也能确认结果。
- 箱型只接受 ISO 箱型代码，误读只纠正到常见箱型，并优先取箱号下方的代码。

**v5.16.17**

- 更新安装助手写日志时允许其他程序同时读取日志。以前日志被杀毒软件或记事本打开时，可能导致更新中断、旧版本没有重新打开，或者回滚失败。这个保护从 v5.16.17 发起的下一次更新开始生效。
- 发布流水线新增 pytest 步骤，授权测试不再被跳过。
- 修复两个偶发失败的测试：更新助手锁文件测试和任务列表渲染耗时测试。
