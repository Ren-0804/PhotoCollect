# ntfy 发布端令牌与写入链路已验证

用户使用重新生成的 `publisher` 令牌成功向 `photocollect-updates` 发布 `release.ready test-001`，响应为 HTTP 200，并返回 `event: message`。这证明发布端认证、topic 写权限和 ntfy 发布接口均已正常，下一步只需验证 `client` 的只读订阅接收。
