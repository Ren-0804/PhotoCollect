# PhotoCollect 软件内更新与回滚已验证

PhotoCollect v5.16.0 已接入局域网客户端更新：启动、每 5 分钟和 ntfy
发布事件都会触发后台清单检查；完整单文件 EXE 在后台下载，并按同源 URL、
文件名、release_id、大小和 SHA-256 校验。

安装助手使用每计划独立脚本、跨进程互斥锁、PID 映像核对和同目录原子替换。
真实 onefile 端到端测试确认新版本能够写入健康回执；故意提交不可执行坏包时，
助手完成回滚并恢复原 SHA-256。

最终 v5.16.0 单文件为 `134,753,371` 字节，SHA-256：
`f3429c91fc661aa63f26bf114e7b7b446b5a74575f064037c4bc883e328652eb`。
完整自动测试为 `179/179`。

群晖 ntfy 保持默认 `deny-all`。客户端不内置令牌，需单独给
`everyone` 对 `photocollect-updates` 的 read-only ACL；publisher 继续仅有
write-only 权限。v5.16.0 是更新器引导版本，需要最后手工分发一次。
