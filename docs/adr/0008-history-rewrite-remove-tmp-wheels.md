# git 历史重写清除 tmp/wheels 构建产物

项目首次 push 到 GitHub 前审查发现：`tmp/wheels/` 目录下有 **238 个 Python wheel 构建产物
（共约 5.4GB，含 torch 507MB、nvidia CUDA/vllm wheels 等）** 被误提交进历史
（单一 commit `fef656c`「Qwen3-Omni-Thinking 模型适配」，为 AutoDL 服务器上安装 vLLM 时
的下载缓存）。这导致：① GitHub 单文件 100MB 硬限制，push 必失败；② 每次 clone 拖 5.4GB。

决定与 ADR-0006（cookie 清除）同策：**git filter-repo `--path tmp --invert-paths` 重写全部历史**
剔除 tmp/，执行 reflog expire + gc 回收旧对象；`.gitignore` 增加 `tmp/` 规则防止复发。

## 考虑的方案

- **filter-repo 历史重写（采纳）**：仅 1 个 commit 引入该目录，剔除干净彻底；仓库无远端
  数据（首次 push 前发现），零协作成本。
- 仅 `git rm --cached` 移出 HEAD（拒绝）：历史里的 5.4GB 仍在 pack 中，clone 依旧拖巨量数据。
- 保留不动（拒绝）：GitHub 直接拒收 >100MB 文件，push 不可能成功。

## 关键约束

- 重写后 commit hash 再次全部变化（ADR-0006 之后第二次重写）：0cb9361/687c2d5/c00d6b4
  （开源交付三提交）及此前所有引用以新 hash 为准。
- filter-repo 会自动剥掉 `origin` remote（防误推旧历史），重写后需重新 `git remote add`。
- `tmp/` 目录保留在本机磁盘（AutoDL 部署可能复用），仅不再有任何 git 记录。
- 推送需 `git push --force`（重写后的历史与远端无共同祖先）。

## 后果

- 仓库 pack 从 5.37 GiB 缩至 ~数十 MB；clone 恢复正常体量。
- 所有本地旧 hash 引用失效；任何基于旧 hash 的外部引用需重新拉取。
- 若未来需追溯重写前历史，可从 filter-repo 自动备份（`.git/filter-repo/`）恢复；gc 后
  旧对象不可寻址。
