"""Skill 服务收敛目录(06-P3): 单一 services/skill/ 承载全部技能域。

布局:
- file_service.py    文件系统 Skill 目录/绑定管理
- facade.py          SkillService 外观(供 API/引擎统一调用)
- router.py          技能路由计划(resources 渐进披露建议)
- runtime.py         SkillInvocationRuntime(调用落库/契约校验)
- scheduler.py       技能发现调度 SkillDiscoveryScheduler
- mentions.py        显式提及收集
- explicit_loader.py 显式技能注入加载
- catalog.py         RuntimeSkillCatalog(会话启动技能快照)
- tool.py            RuntimeSkillTool + InvokeSkillInput
- library/           技能目录模型与解析(纯函数 + 数据类)
"""
