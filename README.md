# ChatGPT 降智检测器（chatgpt-downgrade-detector）

> 检测本地 Clash 系代理各节点的 ChatGPT「降智」情况，自动把干净节点写入顶级规则，让 ChatGPT 流量只走这些节点。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 为什么需要它

OpenAI 会按 IP 对未登录访问做风控分级：

| 判定 | 表现 | IP 状态 |
|---|---|---|
| **LUNA**（GPT-5.6 Luna） | 匿名可正常对话，回复「GPT-5.6 Luna」 | ✅ 干净 |
| **MINI**（GPT-5.5-mini） | 匿名只能用小模型 | ⚠️ 半干净 |
| **LOGIN_WALL**（Sign in is required） | 匿名被强制登录 | ❌ 降智名单 |

机房/机场 IP 普遍在降智名单内。本工具遍历本地代理的全部真实节点，找出当前干净的节点，并把 `openai.com / chatgpt.com / oaiusercontent.com / oaistatic.com` 的流量**只**指向这些节点。

## 特性

- 🔍 **全节点检测**：遍历本地 Clash/mihomo 全部真实节点，未登录三判定（LUNA/MINI/LOGIN_WALL）
- 🖥️ **Web GUI**：本地网页界面（127.0.0.1 自动打开），节点勾选、实时进度、统计、规则应用
- 📐 **顶级规则生成**：干净节点 → `ChatGPT-LUNA` Selector 组 + 4 条 OpenAI 域名规则（规则置顶，优先于订阅自带规则）
- 🔌 **Clash Verge Rev 集成**（`--verge` / GUI 按钮）：把组/规则写入当前订阅链的扩展文件，重新激活订阅后生效
- 🌐 **浏览器插件配套**：ChatGPT 指纹核验 + 自动改时区扩展（v3.1.1），解决「IP 干净但时区指纹不匹配」的降智
- 🛡️ **环境自动恢复**：检测完自动恢复原模式与节点选择

## 快速开始

### 桌面 GUI

```bash
pip install -r requirements.txt
python3 detector/gui.py                # 自动打开 http://127.0.0.1:8899
python3 detector/gui.py --no-open      # 不自动打开浏览器
```

界面功能：
- **节点勾选**：列表可勾选，只测选中的节点（默认全选，支持全选/取消全选）
- **统计**：干净（LUNA）/ 半干净（MINI）/ 降智（LOGIN_WALL）/ 异常（ERROR）实时计数
- **节点列表**：按状态着色，支持筛选（全部/干净/半干净/降智/异常）
- **检测控制**：开始 / 停止（停止后自动恢复 Clash 环境）
- **规则应用**：检测完成后生成顶级规则 → 复制到剪贴板 / 写入 Clash Verge 扩展
- **浏览器插件**：界面显示插件 zip 与目录入口（安装方法见 [docs/extension-install.md](docs/extension-install.md)）

macOS 桌面入口：双击 `start-gui.command`（自动打开浏览器），或运行 `python3 detector/gui.py`。

### 命令行

```bash
git clone https://github.com/lixiaoshuang79/chatgpt-downgrade-detector.git
cd chatgpt-downgrade-detector
pip install -r requirements.txt

# 检测（自动探测 Clash 控制端：Clash Verge Rev unix socket / TCP 9090 / 7890）
python3 detector/main.py
```

输出示例：

```
[1/5] 连接 Clash/mihomo 控制端 ...
      控制端 OK（当前模式=rule, GLOBAL=DIRECT）
[2/5] 待测节点 43 个
[3/5] 切换 global 模式，启动 headless Chrome...
  [1/43] LUNA        韩国KR-HY2  GPT-5.6 Luna
  [2/43] LOGIN_WALL  日本-优化3  Sign in is required to continue.
  ...
[4/5] 环境已恢复（mode=rule）
[5/5] 完成: 干净 8 | 半干净 4 | 降智 31 | 异常 0
      结果: results/verdicts-20260825-130000.tsv
      规则片段: results/chatgpt-clean-rules-20260825-130000.yaml

proxy-groups:
  - name: ChatGPT-LUNA
    type: select
    proxies:
      - 韩国KR-HY2
      ...
rules:
  - DOMAIN-SUFFIX,openai.com,ChatGPT-LUNA
  - DOMAIN-SUFFIX,chatgpt.com,ChatGPT-LUNA
  - DOMAIN-SUFFIX,oaiusercontent.com,ChatGPT-LUNA
  - DOMAIN-SUFFIX,oaistatic.com,ChatGPT-LUNA
```

### 让规则生效（两种方式）

**方式 A：Clash Verge Rev 自动写入（推荐）**

```bash
python3 detector/main.py --verge
```

工具会把 `ChatGPT-LUNA` 组写入当前订阅链的 **groups 扩展**、4 条规则写入 **rules 扩展**。然后在 Clash Verge 订阅页点「**重新激活订阅**」即可。之后无论切哪个订阅，ChatGPT 都只走干净节点。

**方式 B：手动粘贴**（任意 Clash 系客户端）

把生成的 `chatgpt-clean-rules-*.yaml` 中的 `proxy-groups` 段合并进你的 `proxy-groups`，`rules` 段放到 `rules` 列表**最顶部**。

## 浏览器插件（配套）

`extension/` 目录包含 **ChatGPT 指纹核验 + 自动改时区** 扩展（v3.1.1）：

- 进站自动核验浏览器时区与出口 IP 是否匹配（IP 查询走后台服务，绕开页面 CSP）
- 不匹配时在页面脚本执行前自动把浏览器时区改为与 IP 一致（`chrome.debugger` + `Emulation.setTimezoneOverride`）
- 解决「IP 干净但时区对不上」导致的降智——时区指纹 × IP 地理交叉比对是 OpenAI 0821 大规模降智的主要变量

安装（未打包版）：`chrome://extensions` → 开启开发者模式 → 「加载已解压的扩展程序」→ 选择 `extension/` 目录。
分发：`extension/chatgpt-tz-fix-extension-v3.1.1.zip` 为打包版（安装方法见 [docs/extension-install.md](docs/extension-install.md)）。

> ⚠️ 使用 `chrome.debugger` 改时区后，浏览器顶部会出现「xxx 正在调试此浏览器」提示条——这是 debugger API 的正常副作用（保持附加时区才生效），不影响使用；浏览器重启后扩展会自动重新附加。

## 工作原理

```
┌──────────────────────── 检测阶段 ────────────────────────┐
│ Clash API (PUT /proxies/GLOBAL) 逐节点切换 (global 模式) │
│        ↓                                                  │
│ headless Chrome (独立 profile + 伪装 UA + 清会话)          │
│   └─ 未登录访问 chatgpt.com，发送「你是什么模型？」        │
│   └─ 读取回复 → LUNA / MINI / LOGIN_WALL                  │
└────────────────────────────────────────────────────────────┘
                          ↓ 干净节点列表
┌──────────────────────── 生效阶段 ────────────────────────┐
│ proxy-groups: ChatGPT-LUNA (select, 干净节点)             │
│ rules: DOMAIN-SUFFIX,chatgpt.com,ChatGPT-LUNA (置顶)     │
│   → Clash Verge 扩展自动写入 / 手动粘贴                  │
└────────────────────────────────────────────────────────────┘
```

### 关键实现细节（全部实战踩坑验证）

1. **headless Chrome**：`--headless=new` + CDP（Chrome 151+ 的 `/json/new` 必须用 PUT）
2. **过 Cloudflare**：headless 默认 UA 带 `HeadlessChrome` 标记会被 CF 拦 → 伪装 UA + 关闭自动化标记
3. **防会话污染（最重要）**：匿名 ChatGPT 会话存 localStorage，跨 tab 共享——首节点测出 LUNA 后，后续节点会读到残留回复全部假 LUNA。因此每节点测试前强制 `clearBrowserCookies` + `localStorage.clear()` 再重新导航，测完关闭自己的 tab
4. **React 受控输入**：textarea 需原生 setter + input 事件 + 等待状态同步后才能点发送按钮
5. **macOS 无 GNU timeout**：脚本内部自管超时

## 项目结构

```
chatgpt-downgrade-detector/
├── detector/
│   ├── main.py            # CLI（检测 + 规则生成 + --verge 写入）
│   ├── gui.py             # Web GUI 入口（本地服务 + 自动打开浏览器）
│   ├── gui_server.py      # GUI 后端（HTTP API + 后台检测线程）
│   ├── gui_static/        # 前端界面（单文件）
│   ├── clash_api.py       # Clash/mihomo REST 封装（unix socket + TCP 自适应）
│   ├── cdp_tester.py      # headless Chrome CDP 三判定
│   ├── rules.py           # 规则生成器（片段 / Clash Verge 扩展）
│   └── config.example.yaml
├── extension/             # 浏览器插件（指纹核验 + 自动改时区 v3.1.1）
├── docs/extension-install.md
└── examples/              # 配置示例与结果样例
```

## 高级用法

```bash
# 只测指定节点（快速验证）
python3 detector/main.py --nodes "韩国KR-HY2,法国FR-A"

# 指定配置
python3 detector/main.py --config config.yaml

# 跳过特定协议类型（如机场的信息型/倍率节点）
python3 detector/main.py --config config.yaml   # 在 config 里配 skip-types: [ss]

# 测完不恢复环境（排查时用）
python3 detector/main.py --no-restore

# 自定义 Chrome 路径 / 代理端口
python3 detector/main.py --chrome "/Applications/Google Chrome.app/..." --proxy-port 7897
```

## 常见问题

- **报「未找到可用的 Clash/mihomo 控制端」**：确认 Clash 已开 external-controller；Clash Verge Rev 用 unix socket 自动探测；其他客户端在 config.yaml 里配 `clash-api.host/port`
- **检测到干净节点为 0**：你的机场可能全线降智（常见）。换机场/换 IP 池后重测；也检查浏览器时区指纹（用配套插件）
- **测试很慢**：每节点约 40–90 秒（页面加载 + 回复等待），43 节点约 1 小时。可用 `--nodes` 只测重点节点
- **测试期间网络变慢**：检测时 Clash 处于 global 模式（所有流量走被测节点）——可中途 Ctrl+C（工具会恢复环境）
- **「重新激活订阅」后规则没生效**：确认写入的是当前激活订阅链的扩展（`--verge` 会自动解析 profiles.yaml 当前链）

## 免责声明

本项目仅用于**技术学习与研究**，帮助用户理解 OpenAI 的 IP 风控机制。请遵守：

- 所在国家/地区的法律法规（中国大陆用户使用代理访问境外服务需自行评估合规风险）
- OpenAI 使用条款；请勿将干净 IP 用于滥用、批量注册、账号交易等行为
- 项目不提供任何代理服务、不售卖 IP、与 OpenAI 无任何关联

## License

[MIT](LICENSE)
