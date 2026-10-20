# ntfy 最小权限访问控制已验证

用户已经在群晖 ntfy 容器中正确建立 `client` 的 topic 只读权限、`publisher` 的 topic 只写权限，并确认匿名用户没有其他 topic 权限。这意味着后续课程可以直接进入令牌生成和端到端推送验证，不需要再讲解基础 ACL。

## Evidence

用户提供的 `ntfy access` 输出显示：

- `client` → `photocollect-updates` read-only
- `publisher` → `photocollect-updates` write-only
- anonymous → no access to other topics
