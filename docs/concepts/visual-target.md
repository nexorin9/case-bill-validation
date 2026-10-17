# 视觉目标卡（结构化）

> 当前运行环境不具备图像生成能力，**未伪造概念图**。本卡仅作为 Phase 4 末 task 截图采集的对照靶子。
> 卡片末尾附「视觉目标 ↔ 实测截图」差异对照表，便于读者对位比较。

## 三态描述

### 起始态（对应 `docs/screenshots/01-upload.png`）

- 顶部：上传按钮（蓝色，hover 加深）+ 单文件大小 ≤ 20 MB 提示
- 中部左：空字段表骨架（列：字段名 / 抽取值 / 状态徽章 / 置信度 / 原文引用 / 操作）
- 中部右：空白原文区（占位「上传后原文将出现在这里」）
- 底部：导出按钮禁用态
- 视觉对比：实测截图分辨率 1440×900，背景中性灰白，无任何字段填充

### 首个可用瞬间（对应 `docs/screenshots/02-pipeline-running.png`）

- 顶部：进度条出现，标签 `segmenting → extracting → cross_validating → rule_checking → semantic_checking`，5 段顺序推进
- 字段表逐行填色（先灰后蓝 → 黄 → 橙 → 绿/紫）
- 原文区出现 `<mark class="field-highlight">` 高亮区间（淡黄色 + 边框）
- 视觉对比：实测截图分辨率 1440×1069，流水线 4 步已 done + 语义核 active + 32 字段表 + 原文区已加载

### 成形态（对应 `docs/screenshots/03-fields-reviewed.png` 与 `04-excel-exported.png`）

- 字段表全绿（confirmed）/ 紫（flagged）/ 红（rejected）混排
- 顶部「导出结算清单」按钮变绿可点
- 病案系统回写成功 toast 出现在右上角
- SSE 心跳 `: ping` 每 15 秒一次
- 视觉对比：03 截图分辨率 1440×1069，搜索「日期」过滤后 4 字段 + 看依据/确认/驳回三按钮可见；04 截图分辨率 1440×1069，导出成功 toast + 状态栏「已导出: exports/case-xxx.xlsx」

## 配色色值（CSS 变量 · 7 态完整）

| 状态 | 色值 | 含义 |
|------|------|------|
| `--status-pending` | `#94a3b8`（灰） | 字段尚未抽取 |
| `--status-extracting` | `#3b82f6`（蓝） | 字段正在抽取 |
| `--status-rule_checked` | `#eab308`（黄） | 字段已通过规则审查 |
| `--status-semantic_checked` | `#f97316`（橙） | 字段已通过 LLM 语义核 |
| `--status-confirmed` | `#10b981`（绿） | 结算员已确认 |
| `--status-rejected` | `#ef4444`（红） | 结算员已驳回 |
| `--status-flagged` | `#a855f7`（紫） | 字段被规则/跨字段/编码校验标疑 |

## 信息层级

1. 顶部进度条（最高优先级 · 五段标签）
2. 字段表（主区域 · 占据中央 60% 宽度）
3. 原文高亮区（右侧 · `<mark>` 包裹 + 3 秒 fade）
4. 状态徽章（小圆点 · 行首）
5. 操作按钮（行尾 · 「看依据」「确认」「驳回」三按钮可见）

## 必须可达的约束

- 仅依赖 FastAPI + 原生 HTML/CSS/JS（无前端框架）
- SSE via `EventSource`
- CSS 变量 + 行 hover / 状态点击过滤 / `<mark>` 高亮 + 3 秒 fade
- 不引入 WebGL / Canvas / 动画库
- 截图分辨率 ≥ 1280×800（实测 1440×900 / 1440×1069）

## 视觉目标 ↔ 实测截图差异对照表

| 维度 | 视觉目标（设计意图） | 实测截图（真实产物） | 差距说明 |
|------|----------------------|----------------------|----------|
| 顶部进度条 | 5 段标签 `segmenting → extracting → cross_validating → rule_checking → semantic_checking` 全亮 | 4 段已 done + 语义核 active（截图卡在第 5 段瞬时） | 截图是流水线运行中静帧，时序推进未冻结全部完成态 |
| 字段表填充率 | 起始态 0 字段 / 成形态 ≥ 80% 字段已染色 | 02 截图 32 字段逐行填色 + 03 截图 4 字段过滤后高亮 | 过滤功能可工作；实测确实看到状态徽章颜色生效 |
| 原文高亮 mark | `field-highlight` 黄底 #fef08a + 黄边 #facc15 + 3 秒 fade | 02 截图原文区已加载 + 字段值处有 mark 标记 | fade 是时序动效，截图静止帧无法展示，仅作 3 秒状态留存 |
| 确认/驳回按钮 | 行尾三按钮 `看依据 / 确认 / 驳回` | 03 截图三按钮可见可点 | 实测与目标一致 |
| 导出按钮启用态 | 成形态变绿可点 + 病案系统回写 toast | 04 截图导出按钮可点 + 状态栏「已导出: exports/case-xxx.xlsx」 | 实测与目标一致 |
| 字体渲染 | PingFang SC / Microsoft YaHei 系统中文字栈 | headless chromium 无本地中文字体，部分汉字回退到默认 sans-serif | 字体差异肉眼可辨，**不声称两者一致** |
| 色块饱和度 | 100% CSS 变量色值 | 浏览器渲染压缩 + PNG 编码 | 饱和度有 5%–10% 偏差，肉眼基本一致 |
| 动效过渡 | 行 hover 150–250ms / fade 0.5s | 截图静止帧 | 动效不可见，文字描述代替 |
| 中文排版数字 | tabular-nums 等宽对齐 | 浏览器默认字体 | 部分数字列未启用 tabular-nums，**已知差距** |

## 视觉目标的实现路径

| 视觉元素 | 实现技术 |
|----------|----------|
| 字段表行 hover | CSS `:hover` + 背景色 `#f1f5f9` |
| 状态徽章颜色 | CSS 变量 `--status-*` + class 切换 |
| 原文 mark 高亮 | `createElement('mark')` + `classList.add('field-highlight')` + `setTimeout(..., 3000)` 添加 `fading` |
| 搜索过滤 | `filter-input` `input` 事件 + `applyFilter()` 操作 `.filtered-out` |
| 状态徽章点击过滤 | `addEventListener('click', toggleStatusFilter)` |
| SSE 实时更新 | `EventSource('/api/cases/{id}/stream')` |
| 心跳 | `: ping\n\n` 每 15 秒一次（避免代理超时切断） |
