# CampaignData Agent Instructions

For tasks in the personal research system, read the [canonical MyPhysics entry](D:/Obsidian/MyPhysics/System/README.md) and follow its pointers to the owning scientific project. This does not change the domain-neutral package contract. [Repository homepage](README.md); [research-system map](D:/Obsidian/MyPhysics/System/Research-System-Map.md).

## Mission

Provide reusable CSV/TXT campaign indexing, audit, deduplication, merge, and
split primitives without assuming a simulator, dataframe schema, registry, or
project output layout.

## Single-file inspection ladder

For "what's in this file?" questions prefer the cheap entry points over
hand-rolled parsing: `python -m campaign_data peek <file>` (stdlib summary)
or `campaign_data.read_table(path)` (pandas DataFrame, `pandas` extra).
Then `audit` for directory relationships, then `merge`/`split` for writes.

## Boundaries

- Inputs are explicit files/directories and column names or indices.
- Never modify source data.
- Cache files are disposable diagnostics and must be isolated from raw data
  ownership decisions made by consumers.
- Do not import consumer applications. pandas is an optional extra behind
  lazy import in `read_table`; the core package stays stdlib-only.
- Keep Chinese and English schemas in consumer adapters.
- Stable consumers install a wheel; source path injection is test-only.

## Workflow

1. Characterize behavior with a real or synthetic campaign case.
2. Change the shared implementation and tests here.
3. Update caller compatibility adapters.
4. Verify runtime provenance from installed wheels.
5. Commit this repository before consumer repositories.


<!-- project-hub:integration:start -->
## 项目总览接入约定

- 本项目的开发计划与验收证据继续保存在原有项目文档中，无需为总览维护额外进度摘要。总览直接读取本地 Git 提交；不再要求更新 `PROJECT_STATUS.md`，也不删除其他会话留下的状态原文。
- 若本项目已接入主要网页，入口以根目录 `PROJECT_WEB.json` 和 `WEB_ENTRY.md` 为准。修改网页入口、启动命令、依赖、端口或停止方式时，同步更新配置、统一启动脚本及入口说明，并验证启动、打开和停止。
- 使用 `start_web.bat`、`stop_web.bat`，需要加载后端或构建改动时使用 `start_web.bat restart`。开关由 Project Hub 统一管理；不要另起重复服务、按端口直接杀进程或自动换端口。
- Git 提交标题准确描述实际改动，不把提交活动视为完成度或验收证明。更新入口不构成提交授权；每次 commit 仍须向用户确认来源分类和具体模型，遵循既有 Origin 规则。
- 多会话修改前重新读取相关文件，只修改自己负责的内容；保留个人关注和备注。已有会话需重新读取本段，本约定不会自动同步会话或发送任务。
<!-- project-hub:integration:end -->
