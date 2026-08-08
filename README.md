<img src="assets/icon-128.png" width="96" align="right" alt="招财">

# Dr.Wang Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-Windows-important.svg)]()
[![License](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-important.svg)](LICENSE)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4%EF%B8%8F%20Dr.Wang%20Agent-ff69b4.svg)](https://github.com/sponsors/GaryFigno)

[English](README.en.md) · 简体中文

本地桌面编码 Agent（内部包名 / 命令仍为 `aiharness` / `aih`）。跑在**任意
OpenAI 兼容 API** 上，并且自带 **Codex CLI 与 Claude Code CLI 的可视化面板**
—— 一个窗口同时管三套 Agent。

核心区别：**模型**和**API 账号**是两回事。同一个模型可以挂多个账号（同一个 URL
不同 key，或者不同网关），你可以在选模型时直接指定用哪个账号，也可以让路由器
自动挑选并在失败时切换。Codex / Claude 面板同样可以直接换 API 接入：界面里选
供应商模板、填 `base_url` 和 key，自动生成 CLI 配置，密钥只进系统凭据库。

```
/model ds-reasoner@deepseek-b     # 指定模型 + 指定账号
/effort high                      # 推理强度
/context 131072                   # 上下文档位
/mode auto                        # 权限模式
```

---
## 主要功能

*Local desktop coding agent for **any OpenAI-compatible LLM API**. Model and
account are decoupled, multi-agent workflows are built in, and the UI is
Chinese-first. Free desktop app — sponsorship keeps it going.*

- **多账号路由** —— 一个模型挂多个账号 / 网关，自动挑选、故障切换，坏账号不反复重试
- **多 Agent 工作流** —— `Orchestrate` 拆解 → 并发 → 对抗审查 → 自动修复 → 验证；
  `Challenge` 两段式裁定（对抗攻击 + 回读代码逐条核实）；`Research` 并发调研；`Delegate` 派活给便宜模型
- **Plan 模式** —— 复杂请求自动进计划，写操作被权限引擎**物理拦截**，批准才解锁
- **上下文可视化压缩** —— 压缩留痕可展开、`/uncompact` 全文还原；占用按来源拆分（消息 / 工具 / MCP / 提示词）
- **多 Agent 团队** —— `SpawnAgent` 独立身份 / 模型 / 持久化会话，`owns` 文件归属防互相踩踏
- **桌面控制 + 内置浏览器** —— 截屏 / 点击 / 输入与内嵌浏览器（默认关闭，逐级审批）
- **MCP 生态** —— 直接接入现成 MCP 服务器，工具占用按 server 细分
- **Skill 系统** —— 内置 Skill 装载；工作流学习把重复套路自动沉淀成 skill
- **定时任务 + 心跳自迭代** —— 轮数 / 花费 / 时长 / 失败次数四道硬上限
- **Codex / Claude 可视化面板** —— 原生 Codex CLI、Claude Code CLI 直接装进窗口：
  多会话隔离、审批浮窗、图片输入、自动回收孤儿进程，CLI 的体验补齐成桌面 GUI
- **免改配置换 API** —— Codex / Claude 面板内选供应商模板（Kimi、智谱 GLM、Gemini、
  Grok、OpenAI、Anthropic…），填 `base_url` + key 即生成 CLI 配置；协议不兼容时自动
  做 Responses ↔ Chat 转换，密钥只进系统凭据库
- **双界面** —— 桌面窗口（WebView2）+ 终端 TUI，同一后端
- **隐私优先** —— 只绑 `127.0.0.1` + 一次性 token；密钥走系统凭据库，不入配置文件
- **吉祥物桌宠** —— 招财猫动画跟随 agent 状态（思考 / 干活 / 搞定 / 出错 / 整理记忆）

---
## 三个工作区：Agent / Codex / Claude

窗口顶部可以切换三种面板，共用同一个后端与权限模型，但会话、工作区、进程
完全隔离，不会互相串扰：

| 面板 | 运行的是什么 | 典型用途 |
|---|---|---|
| **Agent** | 本项目自研的 agent 循环（`providers/` 路由 + `agent/` 主循环） | 多账号路由、多 Agent 工作流、Plan 模式、压缩、定时任务 |
| **Codex** | 原生 [Codex CLI](https://github.com/openai/codex)（app-server 协议） | 用 Codex 干活，但保留窗口、多会话和审批界面 |
| **Claude** | 原生 Claude Code CLI（`claude` 二进制） | 用 Claude Code 干活，支持订阅登录或第三方 API key |

### Codex 面板

- **多供应商模板**：Kimi（Moonshot）、智谱 GLM、Gemini、xAI Grok、OpenAI，
  每个模板自带 `base_url` 与已知模型表；也可以手填任意 OpenAI 兼容地址
- **换 API 零配置**：在界面上选模板 / 填 `base_url` + key，自动生成 Codex
  `config.toml`；key 存系统凭据库，模板里只写 `${ENV}` 引用，不落盘
- **协议自动转换**：Codex 走 Responses API，而 Kimi 等国内模型只有 chat
  接口 —— 内置转换层在中间做映射（`aiharness/gui/responses_bridge.py`），界面无感
- **一键导入**：把 Agent 面板里配好的账号直接导入 Codex profile，模型名、密钥、
  地址一次带齐；`/models` 拉不到时回退到内置模型表
- **多会话隔离**：每个会话独立进程 + 独立 `thread_id`，可并发；会话状态持久化，
  孤儿进程启动时自动回收

### Claude 面板

- **两种接入方式**：Anthropic 订阅登录（走 `claude auth login`），或第三方
  API key（Kimi Coding 等走 Anthropic 兼容协议）—— 后者在界面里填 key 即可
- **薄宿主设计**：不自己实现 agent 循环，只做转发（文本 + 图片）、把审批 /
  问题浮到 UI、隔离会话，保证行为和原版 CLI 一致
- **自动修复**：失效的 profile（供应商改地址 / 改模型名）会按已知模板自动修复，
  不用手工翻配置文件

---

## 安装

**下载版**（不需要装 Python）：解压 `Dr.Wang-x.y.z-windows.zip`，双击
`Dr.Wang.exe` —— 打开的是**桌面窗口**，不是命令行。手测清单见 [`SMOKE.md`](SMOKE.md)。

两套界面共用同一个后端：

| 命令 | 界面 |
|---|---|
| `Dr.Wang.exe` / `aih` / `aih gui` | 桌面窗口（默认） |
| `aih tui` | 终端界面 |
| `aih gui --serve` | 只起本地服务，用浏览器打开（方便调前端） |
| `aih -p "..."` | 无界面，跑完就退 |

窗口用 **WebView2** 渲染 —— 那就是 Chromium，Windows 11 自带，所以不用像
Electron 那样再捆一份浏览器。前端是纯 HTML/CSS/JS，将来想换 Electron 直接搬。

服务只绑 `127.0.0.1`，每次启动生成一次性 token，没有 token 的连接一律拒绝。
这条不是形式主义：这个 socket 能执行 shell 命令、能改文件，机器上任何够得着它
的东西都会继承 agent 的权限。

**源码版**：

```bash
uv venv && uv pip install -e .
```

## 首次配置

**软件不预置任何模型，也不去猜你的环境**——不会读你没指定的环境变量，不会
自动探测本地 API。这是有意的：猜错的后果是第一次请求就用了你没打算用的账号、
计费到你没选的模型上。

启动后走三步：

```
/accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY
/models add ds
/role main <你挑的模型>
```

第三个参数是**环境变量名**，不是 key 本身——密钥永远不会写进配置文件（存的是
`${VAR}` 引用）。加账号时会立刻联网验证凭据，验证不过就不保存。

`/models add ds` 列出的是**那个账号真正提供的模型**，从它的 `/models` 接口拉的。
给角色指派一个没配置过的模型会被拒绝，而不是静默存下去。

`/setup` 看还缺什么，`/config save` 写盘。

## 打包

```bash
python packaging/build.py --clean
```

产出 `dist/Dr.Wang/`（约 60 MB）和 `Dr.Wang-*-windows.zip`（约 39 MB）。用
PyInstaller 单目录模式，不用单文件——单文件每次启动都要解压到临时目录，慢一秒，
还容易被杀毒软件误报。桌面控制、浏览器、行情这些可选依赖不打进包里，用到时才提示安装。

**安装向导**（Windows，可选）：先安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)，再：

```bash
python packaging/build.py --clean --installer
```

得到 `dist/Dr.Wang-*-windows-setup.exe`（开始菜单 / 桌面快捷方式 / 卸载项）。

---

## 支持作者 / Donate

软件免费使用，靠自愿赞助维持。喜欢它就给颗 ⭐，愿意的话请我们喝杯咖啡 ☕：

[![Sponsor on GitHub](https://img.shields.io/badge/GitHub_Sponsors-%E2%9D%A4%EF%B8%8F-ff69b4?logo=github)](https://github.com/sponsors/GaryFigno)

赞助完全自愿，不解锁功能。

| 方式 | 怎么做 |
|---|---|
| **GitHub Sponsors** | 见下方「开通步骤」；链接写在 `aiharness/support.py` 的 `GITHUB_SPONSORS_URL`，以及 `.github/FUNDING.yml` |
| **支付宝收款码** | 支付宝 App → 收钱 → 保存收款码 → 存为 `aiharness/gui/web/donate/alipay.png` |
| **微信收款码** | 微信 → 我 → 服务 → 收付款 / 二维码收款 → 保存 → 存为 `aiharness/gui/web/donate/wechat.png` |
| **爱发电等** | 把 URL 加进 `aiharness/support.py` 的 `CUSTOM_DONATE_URLS` |

应用内：**设置 → 关于与支持 →「支持作者」**。README 也可直接贴 Sponsors 徽章与收款码图。

### 开通 GitHub Sponsors

1. 用要收款的 GitHub 账号登录 → [github.com/sponsors](https://github.com/sponsors) → **Get started** / 加入 waitlist（部分地区需审核）
2. 填写收款资料（常见走 Stripe）、税务信息、赞助档位
3. 通过后主页会出现 **Sponsor**；链接形如 `https://github.com/sponsors/<你的用户名>`
4. 把该 URL 填进 `aiharness/support.py`，并把 `.github/FUNDING.yml` 里的 `github:` 改成你的用户名（README 顶部的 Sponsors 徽章链接同理）
5. 仓库 Settings → Features 确认 Sponsors / Funding 已启用

软件免费与收赞助不冲突：免费不禁止自愿打赏。

### 支付宝 / 微信收款码

**支付宝**：App → **收钱** → 保存收款码 → `aiharness/gui/web/donate/alipay.png`  
**微信**：App → **我** → **服务** → **收付款** / **二维码收款** → 保存 → `aiharness/gui/web/donate/wechat.png`

可裁剪边框，二维码保持清晰。重新打包或刷新 GUI 后，设置「支持作者」会显示已放入的图。

> **收款码不进公开仓库。** 两个文件已被 `.gitignore` 排除（`alipay.png` /
> `wechat.png`），只存在于本地用于打包，避免收款码和个人信息被公开仓库永久
> 收录（一旦推上去，fork 里永远删不掉）。**切勿**把 API Key、`.env`、私人
> skill 一并提交。

---

## 许可证与商标

本仓库采用 **PolyForm Noncommercial License 1.0.0**（`LICENSE`）：

- ✅ 允许：个人 / 学习 / 研究 / 内部试用等**非商业**用途的查看、使用、修改、分发
- ❌ 不允许：**商业用途**（含用本代码构建商业产品或服务）。需要商业授权请通过
  GitHub Sponsors 或仓库 Issue 联系作者

**「Dr.Wang Agent」名称与招财猫 logo 是作者的商标，不在本许可证授权范围内。**
派生作品必须改名、换标识，不得暗示与原作者有关。

## 密钥安全（提交前必读）

本仓库已带 `.gitignore`，会排除：

- `.env`、`credentials.json`、本机 `config.yaml` / `.aiharness.yaml`
- 项目运行时目录 `.aiharness/`、`.claude/`
- **含 API 的 skill**：请放到 `skills.private/`（整目录忽略），或 skill 内的 `.env` / `secrets/` / `*api*key*`

本地 `skill_paths`（如 `C:\ClaudeProjects\skills`）里已单独加 `.gitignore`，排除依赖外部 API 的 skill（见下表）。公开 skill 正文只写「从环境变量读密钥」，不要写真实 key。

| 排除（依赖 API / 密钥） | 原因 |
|---|---|
| `openai-image-api-i2i` | `OPENAI_API_KEY` |
| `tripo3d-pipeline` | `TRIPO_API_KEY` |
| `hunyuan3d-pipeline` | 腾讯云 `SECRET_ID` / `SECRET_KEY` |
| `image2-queue` | 走外部图像 API / Codex 队列 |
| `dreamina-i2v-workflow` | Dreamina 商业工作流 |
| `ai-visual-generation-workflow` | 路由到上述 API skill |
| `.system/`（含 `imagegen`） | CLI 回退需 `OPENAI_API_KEY` |

可公开保留示例：`gdscript-google-style`、`capture-reusable-tools`（不绑 API Key）。

首次建库示例：

```bash
git init
git add .
git status   # 确认没有 .env、config.yaml、skills.private、alipay 以外的密钥文件
git commit -m "Initial public release"
```

---

## 配置模型

```yaml
accounts:
  - id: deepseek-a
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY}
    priority: 10

  - id: deepseek-b                    # 同一个 URL，不同账号
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY_2}
    priority: 20

models:
  - id: ds-reasoner
    model: deepseek-reasoner
    accounts: [deepseek-a, deepseek-b]   # 两个账号都能服务这个模型
    context_windows: [32768, 65536, 131072]
    default_context: 65536
    supports_temperature: false
    effort:
      mode: reasoning_effort           # 各家参数名不同，这里配，不写死
      levels: {low: low, medium: medium, high: high}
    pricing: {input: 0.55, output: 2.19}
```

API key 从环境变量展开（`${VAR}` / `${VAR:-默认值}`），不落盘。

### 角色：谁干什么活

```yaml
roles:
  main:       {model: ds-reasoner, effort: high}
  fast:       {model: ds-chat}
  cheap:      {model: qwen-turbo}      # 简单活走便宜模型
  verifier:   {model: ds-chat}
  adversary:  {model: kimi}            # 对抗审查换一个模型，避免同源盲区
  researcher: {model: ds-chat}
  compactor:  {model: qwen-turbo}      # 压缩上下文用最便宜的
```

主模型可以把活派给这些角色，不必所有事都自己做。

---

## 工作流

| 工具 | 做什么 |
|---|---|
| `Delegate` | 把机械活交给便宜模型（批量改名、读长文件、写 commit message） |
| `Task` | 指定模型 + 指定账号跑一个子 agent |
| `Research` | 多个模型**并发**调研同一问题，再合成、标出分歧 |
| `Challenge` | 对抗模型攻击你的方案，再由另一个模型逐条裁定 CONFIRMED / WRONG |
| `Verify` | 跑项目检查命令 + 审查模型判定 PASS / FAIL |
| `Orchestrate` | 拆任务 → 并发执行 → 对抗审查 → 自动修复 → 验证 |

`Challenge` 的两段式是关键：对抗模型倾向于编造问题，所以第二段让另一个模型
**回去读代码**逐条裁定，只有 CONFIRMED 的才值得动手。

`Orchestrate` 每个阶段的模型都能单独指定：

```
Orchestrate(goal="...", planner_model="ds-reasoner",
            worker_model="ds-chat@deepseek-b", reviewer_model="kimi")
```

---

## 上下文与压缩

**压缩是本软件调 LLM 做的，不是模型自己做的。** 估算 token 超过阈值时，调
`compactor` 角色的便宜模型把旧消息压成一份交接笔记。

**不会丢内容。** 存储分两层：

- **记录**：`messages.jsonl`，只追加、永不改写
- **视图**：实际发给模型的子集，压缩时才变小

压缩只写一条标记说"0..N 条由这份摘要代表"。`/uncompact` 丢掉标记就恢复全文，
`/history` 随时看原始记录。只有 `/clear` 和 `/delete` 会真正删数据。

**而且压缩是看得见的。** 压缩发生的位置会在对话流里留下一条分隔条：

```
━━ 上下文已压缩 · 40 条消息 → 摘要 · 47,231 → 12,880 tokens（省 34,351）━━
                点击展开摘要 · /uncompact 还原全文
```

点一下展开完整交接笔记，重开旧会话时这些分隔条也会按原位置重现。压缩是唯一
一件"agent 在你没要求的情况下改变了自己知道什么"的事，所以它必须留痕，
而不是只打一行日志。`/markers off` 可以关掉。

---

## 上下文明细

`ctrl+g` 或 `/context` 打开：

```
Context window  691.0k / 1.0M (69%)
████████████████████████████████████████████

■ Messages          673.4k   67.3%
■ System tools        7.3k    0.7%
■ MCP tools           6.9k    0.7%
■ System prompt       3.5k    0.4%
■ Skills              3.3k    0.3%
■ Free space        309.0k   30.9%

MCP tools by server
   github             3.8k
   playwright         2.1k
```

一个"69% 满"只告诉你有麻烦，不告诉你为什么。**按来源拆开才能动手**：四十个
你从没调用过的 MCP 工具，和一段确实该压缩的对话，是两个不同的问题、两种不同
的修法。MCP 那一项还会按 server 细分——意外膨胀通常藏在那里。

## 心跳自迭代

```
/heartbeat 让整个测试套件通过 --iterations 20 --cost 3 --minutes 60
```

agent 会被定时推着往目标走，不用你敲二十次"继续"。它也扛掉线：某一拍失败就
退避重试，而不是把已经落盘的工作丢在半路。

**这是整个软件里最危险的功能**，因为它把人从"花钱 + 改文件"的回路里拿掉了。
所以有四道硬性上限，全部在代码里强制、不是求模型自觉：轮数、花费（美元）、
挂钟时间、连续失败次数。谁先到谁停，绝不自动续期。模型也可以自己声明目标达成
或需要人工介入来提前结束——但它必须说清楚验证了什么，提示词里专门压了这一条。

## 外观

- `/theme` 六套配色：`zhaocai`（默认，取自吉祥物的奶油+陶土红）、`zhaocai-light`、
  `midnight`、`nord`、`matcha`、`mono`。`ctrl+t` 循环切换，选择会记住。
- 上下文容量条随占用**变色**：绿 → 黄 → 橙 → 红，快满之前你就看得出来，
  不用等它压缩完才知道。
- 缓存命中率低于 30% 会标黄，提示前缀被打乱了。
- `/pet` 桌宠招财猫，蹲在侧栏（`ctrl+b`），表情跟着 agent 状态走：
  思考 / 干活 / 搞定 / 出错 / 整理记忆 / 打盹。`/pet emoji` 换成单字符版，
  `/pet off` 关掉。

外观偏好存在独立的 `ui.json`，不会重写你手工编辑的 `config.yaml`。

---

## 大小任务分流 · 澄清 · Plan 模式

每条请求先在**便宜模型**上打个 1–10 分（几毫厘、一秒）：

- **1–2 小问题** → 直接答，不列 todo、不写 plan、不问问题
- **3–4 常规任务** → 直接做
- **5–10 项目** → 自动进入 **plan 模式**

不清楚的地方会**带选项提问**（永远附一个"其他"让你自己写）。只在答案会改变
"要造什么"时才问 —— 代码里能查到的、有明显默认值的，一律不问。

Plan 模式下**所有写操作被权限引擎拦死**，连 yolo 也不例外。这是代码强制的，
不是靠提示词求模型自觉：一个确信自己方案正确的模型，光靠嘴是拦不住的。
Bash 只放行确定只读的命令（`git status` 可以，`git push` 不行）。

Agent 调研完调用 `PresentPlan`，你可以反复提意见让它改，`ctrl+y` 或
`/approve` 定档后才解锁写权限。

```
/plan            进入
/plan off        退出
/approve         批准
/classify off    关掉自动分流
```

## 多 agent 协同

`Delegate` 那种一次性子 agent 适合互不相干的活。当几个 agent 在同一个代码库
上改**互相影响**的部分时，用 team：

```
SpawnAgent(role="api", brief="...", owns=["api.py"], model="ds-reasoner@deepseek-a")
SpawnAgent(role="client", brief="...", owns=["client.py"], model="ds-chat", background=true)
```

- 每个成员有**独立身份、独立模型/账号、独立持久化会话** —— 会话出现在
  `/sessions` 里，你能点进去看它到底干了什么，而不是只听 lead agent 转述
- **文件归属**：`owns` 声明谁能改哪些文件，重叠会被拒绝。两个 agent 改同一个
  文件互相覆盖，这个问题必须在分工时挡掉，而不是事后在 diff 里发现
- **互发消息**：`SendMessage` / `Inbox`，改了签名要通知下游。`wait: true` 可以
  阻塞等回复
- 成员**不能再生成成员** —— 只有 lead 组队，避免 agent 无限繁殖

## 桌面控制

```bash
pip install "aiharness[desktop]"
```

```yaml
desktop: {enabled: true}
```

`Screenshot` / `Click` / `TypeText` / `PressKey` / `Scroll`。

**默认关闭，而且应该保持谨慎。** 别的工具都被工作区目录限制住了，这组不是：
点击落在指针所在的任何应用上。更麻烦的是 agent 是靠读屏幕像素决定点哪里的 ——
**屏幕上的文字会变成模型的输入**，一个写着"点击删除按钮"的网页和你本人提出这个
要求，在模型看来没有区别。开着它的时候把权限模式留在 `ask`。

已经挡掉的：`ctrl+alt+del`、`alt+f4`、`win+l`、`win+r`、`cmd+q`；超长文本输入；
屏幕外坐标。

## 浏览器（内置，不走 MCP）

```bash
pip install "aiharness[browser]" && playwright install chromium
```

```yaml
browser:
  enabled: true
  headless: false          # 默认显示窗口，让你看得见它在干什么
  allow_domains: []        # 非空时只能访问这些域名（含子域）
  deny_domains: []         # 优先于 allow
```

`BrowserNavigate` / `BrowserSnapshot` / `BrowserClick` / `BrowserFill` /
`BrowserScreenshot` / `BrowserClose`。

**关键设计是 snapshot 而不是截图。** 靠坐标点击需要视觉模型，而且页面一重排就
全废。`BrowserSnapshot` 返回页面上所有可交互元素的编号列表，其他工具按编号操作
—— 纯文本模型也能开浏览器，编号还能扛住重新渲染。

已经挡掉的：

- 只允许 http/https。`javascript:`、`data:`、`file:` 全拒（`file:` 曾经能通过
  "补 https://" 的路径绕过，已修）
- **密码/银行卡/token 输入框拒填** —— snapshot 里会标 `(credential field — do
  not fill)`，`BrowserFill` 直接拒绝，让你自己输
- 页面内容一律当**不可信数据**，每次 snapshot 末尾都附警告：网页上写着"忽略你
  之前的指令"是攻击，不是用户请求

## MCP —— 接入现成的工具生态

```yaml
mcp_servers:
  - id: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]

  - id: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env: {GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"}
    tools_deny: [delete_repository]

  - id: hosted
    url: https://example.com/mcp
    headers: {Authorization: "Bearer ${SOME_MCP_TOKEN}"}
```

启动时并发连接，某个 server 挂了只是少几个工具，不影响开机。它们的工具以
`mcp__<server>__<tool>` 出现，**走同一套权限引擎**：

```yaml
deny: ["mcp__github__*"]      # 整个 server 一刀切
allow: ["mcp__filesystem__*"]
```

`/mcp` 看状态，`/mcp tools` 列工具，`/mcp reconnect` 重连。

---

## 高缓存命中

各家的前缀缓存都是**逐字节匹配**，一个字节变了整段缓存作废。所以：

- system prompt **不含日期、git 状态、随机 ID** —— 这些volatile 信息挂在最新一条
  user 消息上（`build_environment_note`）
- 工具定义顺序固定排序
- 消息只追加不改写
- 压缩少而狠 —— 每次压缩必然废掉缓存

状态栏实时显示 `cache 87%`，做对没做对一眼能看到。

---

## 权限

三种模式，`/mode` 随时切：

| 模式 | 行为 |
|---|---|
| `ask` | 写文件、跑命令都要确认；可加白名单 |
| `auto` | 工作区内读写自由，危险命令仍然拦 |
| `yolo` | 不问；但 `block_catastrophic` 仍然拒绝 `rm -rf /`、格式化磁盘、fork bomb 等 |

规则语法同 Claude Code：

```yaml
allow:
  - "Read(*)"
  - "Bash(git diff:*)"      # 前缀匹配
  - "Bash(npm run *)"       # glob
deny:
  - "Bash(sudo:*)"
```

审批弹窗里按 `a` 可以把这条规则加到本次会话。

---

## 定时任务

```
/job add 依赖检查 | weekly mon,thu 09:30 | 检查过期依赖，把结论写进 NOTES.md
/job add 日报     | daily 18:00          | 总结今天的 git 提交
/job add 巡检     | every 4h             | 跑一遍测试，失败就写进 FAILURES.md
/job add 精细     | cron */15 9-18 * * 1-5 | ...
```

支持 `daily` / `weekly`（周几 + 时刻，可多个）/ `every Nm|Nh` / 五段 `cron` /
`once`。中文星期也认（`周一`、`星期四`）。

- `/job list` `/job show <id>` `/job run <id>` `/job on|off <id>` `/job rm <id>`
- 每个任务独立 workspace、独立模型、独立权限模式、独立会话
- 定时任务无人应答，所以 `ask` 会自动降级为 `auto`
- 错过的窗口在 5 分钟宽限期内补跑，超过就顺延到下一次

无 UI 常驻：

```bash
aih daemon
```

---

## Skill

放一个含 `SKILL.md` 的文件夹到 `.aiharness/skills/`、`.claude/skills/` 或用户
skill 目录即可。兼容 Claude Code 的现成 skill。

```markdown
---
name: pdf-forms
description: 填写和提取 PDF 表单字段。当用户提到 .pdf 表单、AcroForm 时使用。
allowed-tools: [Read, Write, Bash]
---

# 正文只在 skill 被调用时才载入
```

渐进式披露：system prompt 里只放 name + description，正文由 `Skill` 工具按需
加载。装一百个 skill 也几乎不占上下文（有测试守着：40 个 skill、每个正文 1 万字，
注入 prompt 的清单仍然小于 8KB）。

**已有的 Claude Code skill 库直接能用**，不用迁移。内置扫描路径已经包含
`~/.claude/skills` 和 `<workspace>/.claude/skills`；共享库指过去就行：

```yaml
skill_paths:
  - C:\ClaudeProjects\skills
```

同名时项目内的覆盖共享库的。SKILL.md 支持 UTF-8 / GBK / GB18030 —— 中文
Windows 编辑器默认存 GBK，只试 UTF-8 会让那些 skill **静默消失**。

## 工作流学习

```
/learn                  扫描历史会话，找出重复出现的习惯
/learn save <name>      把某个候选写成 .aiharness/skills/<name>/SKILL.md
/reload                 让它生效
```

原理：读历史会话，提取每次的请求、命令、碰过的文件，交给模型找**跨会话重复**的
模式。两条硬约束：

- **只提议，不安装。** 从推测出的习惯里偷偷写一条 skill，会在你不知情的情况下改
  变 agent 行为，而且极难排查。每个候选都要你点头才落盘。
- **按会话计数，不按次数。** 一下午跑了 12 次 pytest 是**一个**习惯不是 12 个。
  默认要在 3 个不同会话里出现过才够格。

找不到东西是正常结果，不是失败 —— 编一条触发条件模糊的 skill，比没有更糟。

---

## 为什么它不卡

- Textual TUI，没有 Chromium，常驻约 60MB
- **不建索引、不做 embedding、不监听文件** —— 靠 ripgrep 按需搜
- 流式输出节流到 20fps；Markdown 只在一轮结束时解析一次
- 会话 JSONL 追加写，不整文件重写
- 子进程强制超时 + 输出截断
- 每个 API 账号一个连接池，复用连接

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `/model [模型[@账号]]` | 查看 / 切换模型和账号 |
| `/models` `/accounts` | 列出模型 / 账号及实时状态 |
| `/effort` `/context` | 推理强度 / 上下文档位 |
| `/mode` `/allow` | 权限模式 / 加白名单 |
| `/new` `/sessions` `/resume` | 会话管理 |
| `/clear` `/delete [id\|all]` | 清空 / 删除（会二次确认） |
| `/history` `/compact` `/uncompact` | 完整记录 / 立即压缩 / 恢复全文 |
| `/cost` | 用量、缓存命中、按模型和角色的花费 |
| `/job ...` | 定时任务 |
| `/skills` `/reload` | Skill |
| `/mcp [tools\|reconnect]` | MCP 服务器与工具 |
| `/plan [on\|off]` `/approve` | Plan 模式 / 批准计划 |
| `/classify [on\|off]` | 自动复杂度分流 |
| `/learn [save <name>]` | 从历史会话提炼 skill |
| `/theme` `/pet` `/markers` | 配色 / 桌宠 / 压缩标记 |
| `/doctor` | 探测所有账号 |

快捷键：`ctrl+c` 打断 · `ctrl+d` 退出 · `ctrl+b` 侧栏 · `ctrl+r` 显示/隐藏思考
· `ctrl+l` 清屏（不删对话）

## 命令行

```bash
aih                          # 交互式
aih -p "修好登录的 bug"       # 单次运行，输出到 stdout
aih -m ds-chat@deepseek-b     # 指定模型和账号启动
aih --mode yolo               # 覆盖权限模式
aih sessions                  # 列出会话
aih jobs / aih run-job <id>   # 定时任务
aih daemon                    # 只跑调度器
aih doctor                    # 体检
```

---

## 代码结构

```
aiharness/
  constants.py       所有阈值集中在这里，不允许散落魔法数字
  config/            账号、模型、角色、权限、工作流的配置模型与加载
  providers/         OpenAI 兼容适配器 + 路由/故障转移/计费
  permissions.py     三模式权限引擎 + 危险命令分级
  tools/             文件/shell/todo/skill + 工作流、团队、浏览器、桌面控制
  agent/             主循环、上下文压缩、子 agent、提示词、复杂度分流、agent mesh
  workflows/         Orchestrate 编排器、工作流学习
  session/           append-only 会话存储
  scheduler/         cron 解析、任务定义、后台调度
  mcp/               MCP 客户端：stdio / HTTP 传输、工具代理
  ui/                Textual 界面、主题、桌宠、斜杠命令、弹窗
assets/
  gen/icon_v2_1.png  源插画（gpt-image-2 基于招财形象生成）
  build_icons.py     图标构建流程
  icon.svg           备用扁平矢量标（纯代码绘制，无需素材）
```

图标构建：

```bash
python assets/build_icons.py
```

两件事决定成败，任一做错就会糊成一坨棕色：

1. **源图要为图标而画** —— 主体撑满画面、轮廓带深色描边、对比度推到全尺寸下看着
   略过头。精细插画完全能当图标，但必须是照着图标画的，不是裁出来的。
2. **缩放必须渐进** —— 1024 一步 LANCZOS 到 32，恰好丢掉承载形状的边缘。要反复
   折半，最后加 unsharp mask。
