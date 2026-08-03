# Spec: 阶段 01 — 配置系统

> 分支:`feat/01-config`。依据主规格 `docs/specs/codesage.md`(阶段 01)。

## Objective

配置系统是全部后续阶段的地基:settings 三层(user/project/local)、全局配置、原子写、AGENTS.md 路径发现。模型指针(阶段 02)、权限规则(阶段 05)、hooks 配置(阶段 09)都从这里取配置。

## 对照保留清单

- #18 配置双轨:settings 三层覆盖(user < project < local)+ 全局配置;AGENTS.md 仅作上下文,不参与权限
- #19 AGENTS.md 支持:git root → cwd 逐层收集 + override 文件(本阶段只做路径发现,内容处理在阶段 08)
- #14 原子写:tmp+rename

## 范围

**做**:
1. `settings.json` 三层加载与深合并(permissions/hooks 字段为后续阶段预留)
2. 全局配置 `config.json`(projects 按绝对路径 key;modelProfiles/mcpServers 骨架)
3. 原子写工具(通用,阶段 04/17 复用)
4. AGENTS.md 路径发现(git root → cwd,override 优先,纯文件系统不依赖 git 命令)
5. 环境变量覆盖(`CODESAGE_CONFIG_DIR` 等路径类;`CODESAGE_*` 设置类)

**不做**:AGENTS.md 内容读取与注入(阶段 08);权限规则解析(阶段 05);hooks 执行(阶段 09);模型指针解析(阶段 02)。

## 项目结构(本阶段新建)

```
codesage/
  pyproject.toml            # 项目名 codesage,依赖 pydantic
  codesage/
    __init__.py
    config/
      __init__.py           # 公开 API
      paths.py              # 数据根/配置文件路径(env 覆盖)
      settings.py           # Settings 模型 + 三层加载/合并/mtime 缓存
      global_config.py      # GlobalConfig 模型 + 原子读写
      atomic.py             # 原子写(tmp+rename)
      agents_md.py          # AGENTS.md 路径发现
  tests/
    conftest.py
    test_settings.py
    test_global_config.py
    test_atomic.py
    test_agents_md.py
```

## Commands

```bash
pytest tests/ -q                    # 全量单测
python -c "from codesage.config import load_settings; print(load_settings())"  # 冒烟
```

## Code Style

pydantic 模型 + 全量类型注解 + 模块 docstring。风格样例见主规格。

## Testing Strategy

- 三层覆盖优先级(含深合并:dict 递归、列表追加)
- 原子写:tmp+rename 语义、内容一致
- AGENTS.md 发现:临时 git 仓库(git init 可接受,或手造 .git 目录 —— 手造目录更稳,不依赖 git 可用性)
- 环境变量覆盖:monkeypatch

## Boundaries

- **Always**: 新增配置项时写默认值;路径解析归一化(Windows 大小写)
- **Ask first**: 新增依赖;改变配置文件名/层级
- **Never**: 读 `.claude/` 旧配置(CodeSage 无兼容层);提交任何真实 key

## Success Criteria

- [ ] 三层 settings 覆盖优先级正确,深合并行为符合定义
- [ ] 全局配置读写原子化,损坏 JSON 降级默认不崩溃
- [ ] AGENTS.md 发现返回 git root → cwd 有序列表,override 优先
- [ ] 全部单测绿
