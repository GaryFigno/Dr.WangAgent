---
name: project-scaffold
description: 生成新 Python 项目骨架。当用户要求"初始化项目 / 新建项目 / 搭个工程"且没有现成模板时使用。产出可运行的 pyproject + 包结构 + 测试目录。
allowed-tools: [Read, Write, Bash, Glob]
---

# 新 Python 项目骨架

## 结构

```
<project>/
├── pyproject.toml          # 项目元数据 + pytest/ruff 配置
├── README.md               # 一句话说明 + 快速开始
├── <package>/              # 包名：小写、下划线
│   ├── __init__.py         # 版本号 + 一句话文档字符串
│   └── ...
└── tests/
    ├── __init__.py
    └── test_<module>.py    # 一个最小冒烟测试
```

## pyproject 要点

- `requires-python = ">=3.10"`
- 依赖尽量少；开发依赖放 `[project.optional-dependencies] dev`
- `[tool.pytest.ini_options]`：`testpaths = ["tests"]`、`asyncio_mode = "auto"`（如需 asyncio）
- `[tool.ruff]`：`line-length = 100`

## 规则

- 先问清楚：包名、用途、是否 CLI / 库 / 桌面应用，再动手
- 不引入没问过的重依赖（如 web 框架）
- 骨架建好后跑 `pytest` 确认测试通过、`ruff check` 无错
- 交付时列出生成的文件和下一步建议（装依赖、初始化 git 等）
