# PowerLit Full-Text Acquisition Architecture

本文档定义 `PowerLit` 下一阶段的全文获取架构。目标不是把浏览器自动化当作主下载器，而是在合法访问边界内，把全文获取做成一条分级、可观测、可回退的工程流水线。

## 1. 设计定位

全文获取采用四级优先级策略：

1. `Level 1`: 结构化开放接口
2. `Level 2`: 开放版本定位
3. `Level 3`: 落地页规则提取
4. `Level 4`: 浏览器自动化回退

`Level 4` 的正式定义是：

> 当文献元数据已确定、合法访问路径已存在、但标准化接口无法稳定拿到目标文件时的补救机制。

因此浏览器自动化只负责“最后一公里获取失败”，不承担主下载链路。

## 2. 推荐流水线

```text
OpenAlex
  ↓
期刊 / 关键词 / 年份 / OA 状态筛选
  ↓
优先取 OpenAlex 可用全文链接或内容对象
  ↓
若失败，则 DOI → Unpaywall
  ↓
若仍失败，则访问 publisher landing page / institutional access page
  ↓
若页面存在可合法点击获取的 PDF 按钮，但静态程序无法稳定解析
  ↓
启动本地 browser-agent 执行下载
  ↓
PDF / TEI / XML
  ↓
GROBID（仅 PDF）
  ↓
Markdown
  ↓
LLM 结构化分析
```

## 3. 模块划分

建议将全文子系统拆成下面 6 个可独立实现和测试的模块。

### 3.1 `step1_search_openalex`

- 输入：`QuerySpec`
- 输出：去重后的 `PaperRecord`
- 职责：
  - 调用 `OpenAlex` / `Crossref` / 出版商 API 获取元数据
  - 完成去重、合并和本地索引写入
  - 只负责“找到论文”，不负责下载文件

当前代码映射：

- `src/powerlit/services/search.py`
- `src/powerlit/providers/openalex.py`
- `src/powerlit/providers/crossref.py`

### 3.2 `step2_resolve_fulltext`

- 输入：已入库 `PaperRecord`
- 输出：可下载的全文候选集合 `FullTextCandidate[]`
- 优先级：
  - `OpenAlex` 全文对象 / 链接
  - `DOI -> Unpaywall`
  - `publisher landing page`
- 职责：
  - 解析 `PDF / TEI / XML` 候选
  - 标记访问来源、格式、是否需要浏览器参与
  - 更新 `acquisition_stage = fulltext_resolved`

当前实现：

- `src/powerlit/services/fulltext_resolver.py`

### 3.3 `step3_download_rule_based`

- 输入：`FullTextCandidate`
- 输出：本地文件路径
- 职责：
  - 用确定性规则下载可直接获取的 `PDF / TEI / XML`
  - 处理常见重定向、文件类型校验和文件名规范化
  - 如果落地页只能靠浏览器点击，则把任务投入 `fallback_queue`

建议未来新增模块：

- `src/powerlit/services/downloader_rule_based.py`

### 3.4 `step4_download_browser_fallback`

- 输入：`fallback_queue`
- 输出：本地文件路径或失败日志
- 职责：
  - 仅处理主链路失败但仍存在合法访问路径的条目
  - 使用 `Playwright` 执行固定动作模板
  - 只在规则失效时引入本地 LLM 做页面辅助理解
  - 回写下载结果、最终 URL、页面标题、失败原因

建议未来新增模块：

- `src/powerlit/services/browser_fallback.py`
- `src/powerlit/services/fallback_queue.py`

### 3.5 `step5_parse_grobid`

- 输入：本地 `PDF`
- 输出：结构化 `TEI` 和解析产物
- 职责：
  - 仅对 `PDF` 执行 `GROBID`
  - 如果原始获取结果已经是 `TEI / XML`，则跳过 `GROBID`

建议未来新增模块：

- `src/powerlit/services/parse_grobid.py`

### 3.6 `step6_build_markdown_card`

- 输入：结构化全文对象
- 输出：Markdown 知识卡片 + LLM 分析结果
- 职责：
  - 生成适合阅读和检索的 Markdown
  - 保留引用证据和段落锚点

建议未来新增模块：

- `src/powerlit/services/build_markdown_card.py`

## 4. 数据契约

为保证各模块之间可以独立演进，`papers` 主表至少记录下面这些获取字段：

- `acquisition_method`
- `acquisition_stage`
- `acquisition_source_url`
- `download_status`
- `local_pdf_path`

推荐的 `acquisition_method` 取值：

- `openalex_pdf`
- `openalex_tei`
- `unpaywall_pdf`
- `publisher_direct`
- `browser_agent`
- `manual`

推荐的 `acquisition_stage` 取值：

- `metadata_indexed`
- `fulltext_resolved`
- `downloaded`
- `parsed`
- `markdown_built`
- `analyzed`

## 5. 队列设计

浏览器自动化应作为独立 worker，而不是嵌入主下载器内部。

```text
主下载器失败
  ↓
记录失败原因与目标 URL
  ↓
进入 fallback_queue
  ↓
browser-agent 消费队列
  ↓
模拟人工访问并下载
  ↓
回写状态与文件路径
```

建议 `fallback_queue` 至少记录：

- `paper_dedupe_key`
- `doi`
- `landing_page_url`
- `attempt_reason`
- `last_error`
- `retry_count`
- `status`
- `worker_name`
- `updated_at`

## 6. 浏览器回退的适用场景

- 开放页面存在 PDF 按钮，但链接经 JavaScript 渲染后才出现
- 学校统一认证链路复杂，需要正常浏览器跳转和点击
- 静态请求被站点风控拦截，但用户本身具有合法访问权
- 少量高价值尾部样本需要补抓，不适合重新跑整批规则下载

## 7. 控制策略

浏览器回退不建议完全交给自由式 agent。推荐采用“规则优先，AI 辅助”的混合模式：

- 浏览器控制：`Playwright`
- 高优先级动作：固定规则模板
- 下载完成判定：监控下载目录 + MIME + 文件大小
- 异常页面处理：本地 LLM 辅助页面理解

默认动作模板应包括：

- 等待页面稳定加载
- 查找 `PDF` / `Download PDF` / `View PDF`
- 点击按钮
- 监控下载目录
- 校验文件类型和大小

## 8. 可观测性

后续统计与优化应围绕获取链路展开，建议至少输出：

- 各获取路径成功率
- 各来源平均耗时
- 各出版社对 `browser_agent` 的依赖度
- 常见失败原因分布
- `fallback_queue` 消费成功率和重试率

## 9. 当前落地原则

本仓库当前仍以“检索、索引、手动绑定 PDF”为已实现能力。引入自动化下载时，必须遵守下面两条边界：

- 只在合法授权与开放访问路径内自动化，不绕过授权
- 浏览器自动化始终是末级回退，而不是默认主链路
