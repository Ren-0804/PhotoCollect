# PhotoCollect 群晖更新中心资源

## Knowledge

- [Synology Container Manager：项目](https://kb.synology.com/en-us/DSM/help/ContainerManager/docker_project)
  群晖官方的 Compose 项目创建、启动、停止和编辑说明。用于核对 DSM 菜单与项目操作。
- [Synology Container Manager：容器](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_container?version=7)
  群晖官方的端口、卷、网络、自动重启和容器终端说明。用于排查端口与权限。
- [NGINX 官方容器镜像](https://hub.docker.com/_/nginx)
  官方静态文件只读挂载示例。用于提供更新包和版本清单。
- [ntfy：Docker 安装](https://docs.ntfy.sh/install/)
  ntfy 官方 Docker、持久化缓存、健康检查和架构支持说明。
- [ntfy：认证与访问控制](https://docs.ntfy.sh/config/)
  ntfy 官方用户、令牌、ACL 和默认拒绝策略。用于防止匿名伪造更新通知。
- [ntfy：订阅 API](https://docs.ntfy.sh/subscribe/api/)
  ntfy 官方 JSON、SSE 和 WebSocket 订阅接口。PhotoCollect 使用 JSON 流接收 `release.ready`，不可用时回退到定时检查。
- [Microsoft PowerShell：Get-FileHash](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/get-filehash)
  Microsoft 官方文件哈希说明。群晖发布脚本使用 SHA-256 比对本地验收产物与上传文件。
- [Velopack：Python 接入](https://docs.velopack.io/getting-started/python)
  Python 桌面应用的自动更新接入要求。用于后续把 PhotoCollect 迁移为可增量更新结构。
- [Velopack：增量更新](https://docs.velopack.io/packaging/deltas)
  Zstandard 增量包、版本链和自动回退完整包的官方说明。

## Wisdom (Communities)

- [Synology Community](https://community.synology.com/)
  适合查询具体群晖型号、DSM 版本、卷权限和 Container Manager 的机器差异。
- [ntfy GitHub Discussions](https://github.com/binwiederhier/ntfy/discussions)
  适合核对自托管 ntfy、反向代理和客户端订阅的实际部署问题。

## Gaps

- 群晖地址已按 `192.168.110.79`、HTTP `18080`、ntfy `18081` 固化；更换 NAS 地址时需重新构建客户端。
- ntfy 需给 `everyone` 对 `photocollect-updates` 的只读权限，发布账号继续保持仅写权限。
- v5.16.0 是首个带更新器的过渡版本，现有同事必须最后手工接收一次；此后版本才能在软件内更新。
- 当前校验依赖同源清单、文件大小、release_id 和 SHA-256，尚无代码签名证书。
- PyInstaller onefile 当前使用完整包更新，不生成 Velopack 二进制增量包。
