## 改了什么

<!-- 一两句话说明改动内容和原因；关联的 issue 写 Closes #编号 -->

## 怎么验证的

<!-- 例如：pytest 全部通过；--mock 跑通示例工程；在 5090 上 smoke / 示例工程实测结果 -->

## 检查清单

- [ ] `ruff check .` 和 `pytest -q` 通过
- [ ] 改了 `aidrama/graphs/`：已重新导出 `workflows/`，节点快照已更新，参数对照过官方模板
- [ ] 改了行为：已同步更新 `README.md` / `docs/`
- [ ] 已在 `CHANGELOG.md` 的“未发布”中记录
