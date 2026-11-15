# TAPD 来源

[功能专题](README.md)

可信 TAPD 用户在 Story 或 Bug 的评论中写 `@robert-wklken`，Robert 就把这条评论当作一个请求，
在标签对应的仓库里分析，并在原单据下回评。当前版本只处理问答与澄清：分类器只能选择
`comment-analysis` 或 `waiting-for-user`。

## 配置

```yaml
tapd:
  enabled: true
  executable: tapd                 # 可选，默认从 PATH 查找
  mention_name: robert-wklken      # 触发标记和自动评论前缀
  projects:
    - host: tapd.example.com
      workspace_id: "20001234"
      trusted_user: alice
      accept_after: "2026-11-20T09:00:00+08:00"
      label_rules:
        - label: robert-repo-a
          repository: owner/repo-a
```

- `accept_after` 必须带时区。只有在此之后创建的评论可以触发请求，更早的评论只作上下文。
- 每个项目内标签不能重复，`repository` 必须是已配置的仓库。标签只决定仓库，不代表授权。
- 认证由 TAPD CLI 自己管理（`tapd auth` 写入用户主目录的 `.tapd.json`，或使用 `TAPD_*` 环境变量）。
  凭据不写入 Robert 配置。Robert 在 `<data_dir>/tapd` 中运行 CLI，不读取工作目录里的 `.tapd.json`，
  只向 CLI 传递 `HOME` 和 `TAPD_*` 环境变量。
