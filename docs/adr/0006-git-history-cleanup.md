# git 历史重写清除 YouTube Cookie 凭证

项目目标为开源分发，而 `www.youtube.com_cookies.txt`（Netscape 格式登录 Cookie 明文）
曾在 3 个 commit 中入库（81fbede / 3ed63ce / 013abfb）。文件本身已通过 `git rm --cached` +
`.gitignore` 离库，但历史仍含明文凭证——push 到公开仓库即泄漏。
我们决定用 **git filter-repo 重写全部历史**（`--path www.youtube.com_cookies.txt --invert-paths`），
彻底移除该文件的每一次提交痕迹，并执行 reflog expire + gc 回收旧对象。

## 考虑的方案

- **git filter-repo 历史重写（采纳）**：`--invert-paths` 删除该文件的所有历史，75 个 commit
  全部重写（hash 全变）。仓库无 remote（纯本机），零协作成本。
- 仅轮换 Cookie 凭证（拒绝）：旧值失效后历史无害，但 3 个 commit 的明文仍在——任何
  一次误 push 都会泄漏；且历史审查时无法解释文件为何「曾经存在」。
- 不做处理（拒绝）：私有仓库假设不成立，项目明确要开源分发。

## 关键约束

- 执行时仓库无 remote、工作区干净（无未提交改动）；重写后 `.git/filter-repo/` 状态文件
  保留（`suboptimal-issues` 为空 = 无过滤问题）。
- 重写后 commit hash 全部变化：C7 提交 `3ef984a` → `ce8e06f`，此前所有 hash 引用
  （方案文档、ADR、会话记录）以新 hash 为准。
- Cookie 文件本身保留在本机磁盘（`.env` 的 `YOUTUBE_COOKIE` 仍引用），只是不再有任何
  git 记录；凭证更新走用户重新导出，与 git 无关。

## 后果

- 未来 push 到任意公开仓库不会携带历史凭证；`git log --all -- <cookie 文件>` 恒为空。
- 所有本地 clone 的旧 hash 失效；任何基于旧 hash 的引用（如其他机器的 checkout）
  需重新 clone。本仓库无其他 clone，无影响。
- 若未来需要追溯重写前的原始历史，可从重写前保留的备份（如有）恢复；本机旧对象
  已随 gc 回收，不可恢复。
