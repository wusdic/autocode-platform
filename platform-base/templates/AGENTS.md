# 全局工程约束（所有角色必须遵守）

## 通用
- 任何代码改动前必须存在 approved design_version；无设计不写码。
- 只在分配给你的 task worktree 内改文件，不碰范围外文件。
- 完成任务必须调用 kanban_complete 并附 metadata 证据（changed_files / verification / residual_risk）。
- 长任务每小时至少调用一次 kanban_heartbeat。

## 代码规范
- （此处填你的语言/框架规范：命名、目录结构、lint 规则、提交信息格式）

## 接口规范
- （此处填 API 设计约定：REST/GraphQL、错误码、版本策略）

## 安全
- 不在日志/metadata 中写入任何密钥、token。
- 不执行删除类破坏性命令，除非任务明确要求且经审批。
