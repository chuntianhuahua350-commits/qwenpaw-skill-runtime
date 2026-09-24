# QwenPaw Skill Runtime

版本 `0.2.0`。QwenPaw 通用插件，在每次构建 Agent 之前刷新工作区技能，避免会话继续沿用旧的 `SKILL.md`。

兼容 QwenPaw `2.0.0` 到 `3.0.0`。

## 解决什么问题

技能文件改过之后，运行时里还可能留着两份旧内容：

- 已经解析过的技能缓存，以及 `SKILL.md` 的文件快照；
- 当前会话里更早一次 Skill 工具返回的正文。

只改磁盘上的文件，下一轮构建仍可能读到旧缓存，模型也可能接着上一轮的旧正文回答。

## 什么时候运行

插件注册 `PRE_AGENT_BUILD` 钩子 `qwenpaw_skill_runtime_pre_build`，在 `AgentBuilder.build()` 之前执行。优先级是 `80`。会话加载钩子的优先级是 `10`，数字更小的先执行，所以本钩子跑在旧会话载入之后，能改到已经放进上下文的 Skill 结果。

没有 `workspace_dir` 时直接跳过。

## 做了什么

### 技能版本变了

按工作区记住每个技能上次看到的版本。和上次相比版本变了，或者技能目录被删掉，就记为一次更新：

1. 丢掉该技能 `SKILL.md` 的文件快照；
2. 清空已解析的技能缓存，让接下来的构建重新读文件。

第一次见到某个技能只记录版本，不当作更新。读文件失败时跳过该技能，并打一条 warning。

版本来自 `SKILL.md` 的 frontmatter，由 QwenPaw 的 `extract_version` 取出。

### 会话里还留着旧正文

扫描当前会话上下文中的 Skill 工具结果。正文和磁盘上的 `SKILL.md` 不一致时，把结果换成当前正文，并再次清掉对应缓存。

有技能被刷新时，向本次构建注入一段提示：不要沿用本会话里更早的 Skill 工具结果，重新调用 Skill 工具，只按新正文回答。提示里会附上这些技能的当前正文。

## 日志

日志名是 `qwenpaw.plugins.qwenpaw_skill_runtime`。版本变化会打出技能名和旧版本到新版本；本轮有刷新时还会打出 `session_id` 和技能名列表。

## 文件

```text
qwenpaw-skill-runtime/
├── plugin.json    # id、版本、QwenPaw 兼容范围、入口
├── plugin.py      # 注册 PRE_AGENT_BUILD 钩子
└── README.md
```
