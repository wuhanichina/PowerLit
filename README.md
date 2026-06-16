# PowerLit

`PowerLit` 是一个面向电力系统研究的本地优先文献库工具。当前版本适合部署在学校的 Windows 服务器上，完成以下工作：

- 检索并规范化论文元数据
- 导出 `论文全名 + GB/T 7714 引文 + DOI`
- 维护本地索引库、下载队列和 PDF 绑定状态
- 调用外部 AI 服务生成结构化分析 `Markdown + JSON`

当前架构遵循一个硬约束：`检索事实来源于学术数据库与出版商元数据，AI 只负责查询扩展、结构化提取、Markdown 生成和分析总结。` 这样可以尽量降低“AI 凭空找文献”和“AI 编造引文”的风险。

## 版本说明

当前版本：`0.1.0`

这是一个本地优先的早期可用版本，已经覆盖文献检索、元数据归档、PDF 入库、全文解析、AI 笔记生成、分析结果导出和基础 Web UI。基础 Web UI 在真实文献库较大时优先使用轻量预览，避免首屏打开时扫描完整数据库。现阶段更适合作为个人或小团队的研究工作流工具使用，配置项、目录结构和部分维护脚本仍可能随实际使用继续调整。

开源仓库不包含真实文献库、个人数据库、API Key、本地测试目录和运行产物；这些内容由 `.env` 和本地挂载路径管理。

## 开源仓库说明

本仓库只保存 `PowerLit` 的程序代码、配置模板、流程脚本和项目文档。真实文献 PDF、个人数据库、AI 生成结果、下载清单、本地测试目录、虚拟环境和 API Key 配置都应留在本机或挂载盘中，不上传到 GitHub。

默认忽略的本地内容包括：

- `.env` 与 `config/ai.yml`
- `literature/`、`output/`、`download_list/`
- `test/`、`local/`
- `.venv/`、`node_modules/`

如果部署环境使用网盘或 NAS，可在 `.env` 中把 `POWERLIT_*` 路径指向挂载目录；整理或提交本仓库文件不会自动改动这些挂载盘数据，只有运行入库、下载、解析、分析等业务命令时才会按配置读写外部文献库。

## 项目文件结构

仓库里的核心文件尽量保持简单：

```text
src/powerlit/         # PowerLit 核心 Python 包、CLI、Web UI 和服务逻辑
config/               # 可提交的配置模板与示例
docs/                 # 架构说明、使用指南、路线图
incoming_pdf/         # Windows 入库流程快捷脚本
scripts/maintenance/  # 长任务、批处理、审计和维护脚本
tools/                # PDF 文本提取等辅助工具
```

本地专用目录不作为开源仓库内容：

```text
literature/           # 本地或挂载盘文献库数据
output/               # 本地运行输出
test/                 # 本地测试代码
local/                # 历史下载清单、临时脚本等本地杂项
.venv/                # Python 虚拟环境
node_modules/         # Node 工具依赖
```

## 适用部署方式

当前推荐部署目标是 `Windows Server + Python + FastAPI + 计划任务`。

推荐运行形态：

- `FastAPI` 常驻提供 Web UI 和 API
- `CLI` 负责批量检索、全文定位、分析生成
- `Windows 计划任务` 定时执行检索和分析命令
- `硅基流动` 或其他 `OpenAI 兼容 API` 负责文献分析和 Markdown 生成

## 当前能力

- 可直接使用 `Crossref` 和 `OpenAlex`
- 已预留 `IEEE Xplore` 和 `Elsevier Scopus` 的 API 接口
- 支持导入 `中科院期刊分区表` 作为期刊白名单
- 支持本地 `SQLite` 索引库
- 支持导出 `JSON / CSV / Markdown`
- 支持生成 `ResearchGate` 查找链接
- 支持解析全文候选来源
- 支持手动绑定 PDF
- 支持按 DOI 或按主题包调用 AI 生成分析结果

## 目录结构

当前默认采用下面这套结构：

```text
literature/
  reference/            # PDF 原文
    ieee_tsg/
    ieee_tpwrs/
    applied_energy/
    energy/

  md/                   # AI 生成的 Markdown、原始提取、分析结果
    ieee_tsg/
    ieee_tpwrs/

  metadata/             # 题录导出、下载清单、papers.db
    papers.db
    download_list/

  index/                # 后续向量索引或全文索引
    vector_index/

  reports/              # 自动生成综述
    weekly/
    monthly/
```

路径规则：

- 期刊名使用固定短名称，例如 `ieee_tpwrs`、`ieee_tsg`、`applied_energy`
- 如果期刊元数据本身是中文期刊名，则目录名直接使用中文期刊名
- 卷号目录使用 `vXX`，例如 `v35`、`v310`
- 期号目录使用 `iXX`，例如 `i03`、`i12`
- 如果期刊没有期号信息，则只建到卷号目录

唯一标识规则：

- 每篇论文的唯一主键是 `DOI`
- 文件名不是唯一标识
- `papers.db`、Markdown frontmatter、分析 JSON 都会保存 DOI
- 后续即使重新下载 PDF 或更换文件名，也以 DOI 作为索引和关联键

## 按期刊自动化流程

当前已经支持按期刊而不是按关键词来维护文献库。推荐流程如下：

1. 在 `config/journals.watch.example.yml` 中维护监控期刊。
2. 使用 `sync-journal` 或 `sync-journal-bundle` 按 `期刊 + ISSN + 年份` 抓取论文元数据。
3. 调用 `OpenAlex / Unpaywall / publisher landing page` 解析开放获取状态。
4. 如果存在可直接访问的 OA PDF，可选执行自动下载。
5. PDF 自动归档到 `literature/reference/<journal>/vXX/iXX/`。
6. 后续统一执行 `parse-pdf / analyze-paper`，结果写入 `literature/md/<journal>/vXX/iXX/`。

## incoming_pdf 中转流程

`incoming_pdf/` 现在作为“人工下载到正式文献库”的中转站使用：

1. 你先把 PDF 下载到 `incoming_pdf/`
2. 运行 `incoming_pdf/process_incoming_pdf.cmd`
3. 系统自动完成：
   - 识别 DOI
   - 必要时通过 DOI 补元数据
   - 自动重命名
   - 移动到 `literature/reference/<journal>/vXX/iXX/`
   - 解析 PDF
   - 生成完整中文版 Obsidian 笔记
   - 生成分析 Markdown / JSON

说明：

- 如果 PDF 文件名中已经带有 `__10-xxxx` 这类 DOI 后缀，会优先使用它识别
- 如果文件名里没有 DOI，会尝试从 PDF 前几页文本中提取 DOI
- 成功处理后，文件会从 `incoming_pdf/` 移走，不会保留副本
- 失败的文件会继续留在 `incoming_pdf/`，便于你人工检查

示例 watchlist 已经放在：

- `config/journals.watch.example.yml`

默认包含：

- `ieee_tsg`
- `ieee_tpwrs`
- `applied_energy`
- `energy`

每条期刊配置至少包含：

- `short_name`
- `title`
- `issns`
- `providers`
- `from_year`
- `limit`

## 文档编码规则

所有由 `PowerLit` 自动生成的 Markdown 文档（`.md`）必须统一遵守以下规则：

- 编码格式：`UTF-8`
- BOM：`不带 BOM`
- 换行符：`LF (\n)`
- 文件末尾：`必须保留结尾换行`

该规则已经写入项目根目录的 [`.editorconfig`](.editorconfig)。

## 安装

项目要求 `Python >= 3.12`。

推荐安装方式：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

如果你更偏好 `uv`，也可以使用：

```powershell
uv sync --extra dev
Copy-Item .env.example .env
```

## Windows 环境自检

完成安装后，正式跑检索前，建议先执行一轮最小自检：

```powershell
where python
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
.\.venv\Scripts\powerlit --help
.\.venv\Scripts\powerlit providers
```

你可以按下面的标准判断是否正常：

- `where python` 能看到本机 Python 路径
- `.\.venv\Scripts\python.exe --version` 显示 `3.12.x`
- `.\.venv\Scripts\powerlit --help` 能列出命令
- `.\.venv\Scripts\powerlit providers` 能列出 `crossref/openalex/ieee/elsevier/siliconflow`

如果你想连测试一起做，直接再跑：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

## 一步一步怎么用

下面按 `Windows 服务器` 场景写最短使用路径。你第一次用时，直接照这个顺序跑。

### 第 1 步：进入项目目录

```powershell
cd D:\OneDrive\【文献库】\AI专用文献库
```

如果你在 Windows 默认控制台里看到中文或作者名乱码，先执行一次：

```powershell
chcp 65001
```

### 第 2 步：创建虚拟环境并安装依赖

如果你还没有可用的 Windows 虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

如果你已经装过依赖，后续只需要：

```powershell
.\.venv\Scripts\Activate.ps1
```

### 第 3 步：配置 `.env`

第一次使用时执行：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，至少填这几项：

```env
POWERLIT_IEEE_API_KEY=你的IEEE密钥
POWERLIT_ELSEVIER_API_KEY=你的Elsevier密钥
POWERLIT_AI_PROVIDER=siliconflow
POWERLIT_AI_BASE_URL=https://api.siliconflow.cn/v1
POWERLIT_AI_API_KEY=你的硅基流动密钥
POWERLIT_AI_MODEL=Qwen/Qwen2.5-72B-Instruct
```

如果你要启用“只保留中科院二区及以上期刊”的检索约束，再配置：

```env
POWERLIT_CAS_JOURNAL_LIST_PATH=config/cas_journal_whitelist.xlsx
POWERLIT_CAS_MAX_QUARTILE=2
```

说明：

- `POWERLIT_CAS_JOURNAL_LIST_PATH` 指向你从官方平台导出的 `xlsx/csv`
- 白名单启用后，检索结果只保留 `journal article`
- 会议论文会被自动排除

### 第 4 步：检查配置是否生效

```powershell
.\.venv\Scripts\powerlit providers
```

正常情况下你应该能看到：

- `crossref: ready`
- `openalex: ready`
- `ieee: ready`
- `elsevier: ready`
- `siliconflow: ready`

### 第 5 步：先跑一轮文献检索

这是最常见的单次检索命令：

```powershell
.\.venv\Scripts\powerlit search "power system stability" --name stability-test --providers crossref,openalex --limit 20
```

这条命令会同时做三件事：

- 联网检索论文元数据
- 写入本地数据库 `literature/metadata/papers.db`
- 导出 `literature/metadata/stability-test.json`、`literature/metadata/stability-test.csv`、`literature/metadata/stability-test.md`

### 第 6 步：查看已经入库的论文

```powershell
.\.venv\Scripts\powerlit list-papers --query-pack stability-test --limit 10
```

你会看到：

- 论文题名
- DOI
- 国标引文
- 下载状态
- ResearchGate 查找链接

### 第 7 步：如果需要，补全文下载线索

```powershell
.\.venv\Scripts\powerlit resolve-fulltext --query-pack stability-test --limit 20
.\.venv\Scripts\powerlit download-queue --query-pack stability-test --limit 20
```

这一步不会自动爬网页下载 PDF，它只会：

- 尝试解析开放获取和出版商入口
- 在 `literature/metadata/download_list/` 目录下生成一个待下载清单

默认情况下，`download-queue` 会输出：

- `literature/metadata/download_list/<query-pack>-download-list.csv`
- `literature/metadata/download_list/<query-pack>-download-list.md`

其中 `.md` 文件是给人工逐篇下载直接使用的，里面会明确列出：

- 题名
- 国标引文
- DOI
- 出版商链接
- ResearchGate 页面或查找链接
- 建议文件名
- 目标 PDF 路径

这里的“出版商链接”现在优先使用 `DOI 落地页`，不再写成 `api.elsevier.com` 这类接口地址。

### 第 8 步：绑定 PDF，提取原文，并整理成 Obsidian 笔记

如果你已经从校园网或 VPN 环境下载好了 PDF，先绑定到库里：

```powershell
.\.venv\Scripts\powerlit attach-pdf --doi 10.1109/example --file incoming_pdf\example.pdf
```

`attach-pdf` 会把论文登记到受管路径下：

- 如果 PDF 已经位于正确的 `literature/reference/期刊短名/卷号/期号/` 目录，只做绑定，不重复复制
- 如果 PDF 还在 `incoming_pdf`，会自动移动到对应的期刊/卷/期目录，并按论文题名 + DOI 自动重命名
- 如果 PDF 在其他目录，会自动复制到对应的期刊/卷/期目录，再写入数据库

如果你不想手工逐篇执行 `attach-pdf`，可以直接运行：

```powershell
.\incoming_pdf\process_incoming_pdf.cmd
```

这个 `.cmd` 会先自动切换到仓库根目录，再执行 `process-incoming-pdf`，所以即使你从 `C:\Windows\System32` 这类目录直接调用它，也会正确读取 `.env` 和 `incoming_pdf/`。

这个脚本会自动处理 `incoming_pdf/` 中的全部 PDF。只做搬运和解析、不跑 AI 分析时：

```powershell
.\incoming_pdf\process_incoming_pdf.cmd
.\incoming_pdf\process_incoming_pdf.cmd --skip-analyze
```

脚本执行结束后会明确输出一行结果：

- `[PowerLit] 结果：成功`
- 或 `[PowerLit] 结果：失败（退出码 X）`

然后执行解析：

```powershell
.\.venv\Scripts\powerlit parse-pdf --doi 10.1109/example
```

如果该论文还没写入 `local_pdf_path`，也可以显式传 PDF 路径：

```powershell
.\.venv\Scripts\powerlit parse-pdf --doi 10.1109/example --file incoming_pdf\example.pdf
```

`parse-pdf` 现在会自动做三件事：

1. 从 PDF 提取原始全文文本
2. 保留一份原始提取版 Markdown 供回溯
3. 调用 AI 整理成适合 Obsidian 阅读的“完整中文版” Markdown

解析结果会输出到：

- `literature/md/<journal>/<vXX>/<iXX>/*.md`
  这是 AI 整理后的 Obsidian 兼容完整中文版笔记，适合人直接阅读

每次 `parse-pdf` 还会在终端输出：

- 当前使用的是 `AI 全文整理` 还是 `fallback`
- 本次整理消耗的 `prompt tokens / completion tokens`
- 本次整理的 `估算费用`

### 第 9 步：对单篇论文做 AI 分析

```powershell
.\.venv\Scripts\powerlit analyze-paper --doi 10.1109/example
```

现在 `analyze-paper` 会按这个优先级自动取证据：

1. 你显式传入的 `--source-file`
2. 已生成的 `parsed_md_path`
3. 摘要和元数据

如果你想强制指定某个外部文本文件，也可以继续这样传：

```powershell
.\.venv\Scripts\powerlit analyze-paper --doi 10.1109/example --source-file parsed\example.md
```

分析结果会输出到：

- `literature/md/<journal>/<vXX>/<iXX>/*.analysis.json`
- `literature/md/<journal>/<vXX>/<iXX>/*.analysis.md`

每次 `analyze-paper` 也会在终端输出：

- `prompt tokens / completion tokens / total tokens`
- 本次分析的 `估算费用`

### 第 10 步：按主题包批量解析和分析

先批量解析已经绑定 PDF 的论文：

```powershell
.\.venv\Scripts\powerlit parse-query-pack --query-pack stability-test --limit 20
```

如果你要强制重生已经存在的全文中文版笔记，追加 `--force`：

```powershell
.\.venv\Scripts\powerlit parse-query-pack --query-pack stability-test --limit 20 --force
```

再批量分析：

```powershell
.\.venv\Scripts\powerlit analyze-query-pack --query-pack stability-test --limit 20
```

如果你要按最新解析文本重跑分析，也可以追加 `--force`：

```powershell
.\.venv\Scripts\powerlit analyze-query-pack --query-pack stability-test --limit 20 --force
```

这个命令适合挂到 Windows 计划任务里做自动化批处理。

如果你之前已经在旧的 `output/` 路径下积累了 PDF、解析结果或分析结果，可以执行一次路径迁移：

```powershell
.\.venv\Scripts\powerlit migrate-library --limit 500
```

这个命令不会调用 AI，只会把已有文件整理到新的 `literature/reference` 和 `literature/md` 结构下，并更新数据库中的路径记录。

### 第 11 步：打开 Web 界面

```powershell
.\.venv\Scripts\powerlit serve
```

然后在浏览器里打开：

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/library`
- `http://127.0.0.1:8000/queue`
- `http://127.0.0.1:8000/docs`

## 概率最优潮流测试示例

如果你现在就想跑“概率最优潮流 / chance-constrained OPF”这一类文献，直接用下面这组命令：

### 1. 检索

```powershell
.\.venv\Scripts\powerlit search "chance-constrained optimal power flow probabilistic optimal power flow" --name chance-opf-test --providers crossref,openalex --limit 8
```

### 2. 查看结果

```powershell
.\.venv\Scripts\powerlit list-papers --query-pack chance-opf-test --limit 10
```

### 3. 批量分析

```powershell
.\.venv\Scripts\powerlit analyze-query-pack --query-pack chance-opf-test --limit 10
```

### 4. 如果已经拿到 PDF，继续接解析链路

```powershell
.\.venv\Scripts\powerlit attach-pdf --doi 10.1109/tpwrs.2022.3146873 --file incoming_pdf\chance-opf.pdf
.\.venv\Scripts\powerlit parse-pdf --doi 10.1109/tpwrs.2022.3146873
.\.venv\Scripts\powerlit analyze-paper --doi 10.1109/tpwrs.2022.3146873
```

### 5. 看输出文件

重点看这些路径：

- `output/chance-opf-test.json`
- `output/chance-opf-test.csv`
- `output/chance-opf-test.md`
- `download_list/chance-opf-test-download-list.md`
- `output/parsed/`
- `output/analysis/`

## 常见工作流

你平时最常用的是下面这条链：

1. `search` 或 `search-topic`
2. `list-papers`
3. `resolve-fulltext`
4. `download-queue`
5. 手动下载 PDF
6. `attach-pdf`
7. `parse-pdf` 或 `parse-query-pack`
8. AI 自动把原始提取整理成 `Obsidian` 笔记
9. `analyze-paper` 或 `analyze-query-pack`

## 关键配置

复制 [`.env.example`](.env.example) 后，至少关注这些变量：

- `POWERLIT_OUTPUT_DIR`
- `POWERLIT_DOWNLOAD_LIST_DIR`
- `POWERLIT_CAS_JOURNAL_LIST_PATH`
- `POWERLIT_CAS_MAX_QUARTILE`
- `POWERLIT_DB_PATH`
- `POWERLIT_INCOMING_PDF_DIR`
- `POWERLIT_PARSED_OUTPUT_DIR`
- `POWERLIT_ANALYSIS_OUTPUT_DIR`
- `POWERLIT_API_HOST`
- `POWERLIT_API_PORT`

元数据源配置：

- `POWERLIT_IEEE_API_KEY`
- `POWERLIT_ELSEVIER_API_KEY`
- `POWERLIT_ELSEVIER_INSTTOKEN`
- `POWERLIT_UNPAYWALL_EMAIL`

CAS 期刊白名单配置：

- `POWERLIT_CAS_JOURNAL_LIST_PATH`
- `POWERLIT_CAS_MAX_QUARTILE`

你可以先复制模板文件再替换为官方导出表：

- `config/cas_journal_whitelist.template.csv`
- 建议正式文件名：`config/cas_journal_whitelist.xlsx`

外部 AI 配置：

- `POWERLIT_AI_PROVIDER`
- `POWERLIT_AI_BASE_URL`
- `POWERLIT_AI_API_KEY`
- `POWERLIT_AI_MODEL`
- `POWERLIT_AI_TEMPERATURE`
- `POWERLIT_AI_TIMEOUT`
- `POWERLIT_AI_NOTE_TIMEOUT`
- `POWERLIT_AI_SOURCE_CHAR_LIMIT`
- `POWERLIT_AI_NOTE_SOURCE_CHAR_LIMIT`
- `POWERLIT_AI_CURRENCY`
- `POWERLIT_AI_INPUT_PRICE_PER_MTOKENS`
- `POWERLIT_AI_OUTPUT_PRICE_PER_MTOKENS`

如果你使用硅基流动，默认这组配置即可：

```env
POWERLIT_AI_PROVIDER=siliconflow
POWERLIT_AI_BASE_URL=https://api.siliconflow.cn/v1
POWERLIT_AI_API_KEY=你的密钥
POWERLIT_AI_MODEL=Qwen/Qwen2.5-72B-Instruct
POWERLIT_AI_NOTE_TIMEOUT=600
POWERLIT_AI_NOTE_SOURCE_CHAR_LIMIT=90000
```

说明：

- `POWERLIT_AI_NOTE_TIMEOUT=600` 是“全文中文版 Obsidian 笔记”的推荐值，避免长论文因为超时回退成简化版。
- `POWERLIT_AI_NOTE_SOURCE_CHAR_LIMIT=90000` 用于尽量保留全文上下文；如果后续成本过高，再按需下调。

如果你想显式覆盖单价，也可以在 `.env` 中配置：

```env
POWERLIT_AI_CURRENCY=CNY
POWERLIT_AI_INPUT_PRICE_PER_MTOKENS=4.13
POWERLIT_AI_OUTPUT_PRICE_PER_MTOKENS=4.13
```

Web 界面当前会展示：

- 累计 AI 费用
- 每篇论文的笔记费用
- 每篇论文的分析费用
- 每篇论文的总费用
- `Obsidian 笔记 / 原始提取 / PDF / 分析` 打开入口

## 常用命令

### 启动服务

```powershell
.\.venv\Scripts\powerlit serve
```

浏览器入口：

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/library`
- `http://127.0.0.1:8000/queue`
- `http://127.0.0.1:8000/docs`

### 基础检索

```powershell
.\.venv\Scripts\powerlit providers
.\.venv\Scripts\powerlit topics
.\.venv\Scripts\powerlit journals --journals-file config\journals.watch.example.yml
.\.venv\Scripts\powerlit search "power system stability" --providers crossref,openalex --limit 20
.\.venv\Scripts\powerlit search-topic stability --providers crossref,openalex --limit 20
.\.venv\Scripts\powerlit list-papers --limit 20
```

### 按期刊同步

同步单个期刊：

```powershell
.\.venv\Scripts\powerlit sync-journal ieee_tpwrs --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200
```

如果你希望自动下载开放获取 PDF：

```powershell
.\.venv\Scripts\powerlit sync-journal ieee_tpwrs --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200 --download-oa
```

批量同步 watchlist，并生成本周新增论文报告：

```powershell
.\.venv\Scripts\powerlit sync-journal-bundle --journals-file config\journals.watch.example.yml --download-oa
```

输出位置：

- 元数据导出：`literature/metadata/journals/<short_name>.json/.csv/.md`
- 下载清单：`literature/metadata/download_list/<short_name>-download-list.md`
- 周报：`literature/reports/weekly/*.md`

### 全文定位与下载队列

```powershell
.\.venv\Scripts\powerlit resolve-fulltext --query-pack stability --limit 20
.\.venv\Scripts\powerlit download-queue --query-pack stability --limit 20
.\.venv\Scripts\powerlit attach-pdf --doi 10.1109/example --file incoming_pdf\example.pdf
.\.venv\Scripts\powerlit process-incoming-pdf
```

`download-queue` 现在会同时生成：

- `download_list/<query-pack>-download-list.csv`
- `download_list/<query-pack>-download-list.md`

其中 `download_list/*.md` 就是你要人工下载的论文清单。

## 中科院白名单说明

当前代码支持把 `中科院期刊分区表` 作为检索白名单，但我没有直接把“完整版分区表”放进仓库，原因是：

- 官方平台要求机构账号或个人授权访问
- 官方明确声明未授权任何个人或机构公开发布分区数据

所以当前推荐流程是：

1. 你从官方平台导出 `xlsx/csv`
2. 放到 `config/cas_journal_whitelist.xlsx`
3. 重新执行检索命令

白名单匹配默认使用 `期刊名 -> 分区`，保留 `1区/2区` 期刊。

### PDF 解析

```powershell
.\.venv\Scripts\powerlit parse-pdf --doi 10.1109/example
.\.venv\Scripts\powerlit parse-query-pack --query-pack stability --limit 20
```

### AI 分析

按 DOI 分析单篇论文：

```powershell
.\.venv\Scripts\powerlit analyze-paper --doi 10.1109/example
```

如果你已经把全文转成文本或 Markdown，可以把提取结果一并送入分析：

```powershell
.\.venv\Scripts\powerlit analyze-paper --doi 10.1109/example --source-file parsed\example.md
```

按主题包批量分析：

```powershell
.\.venv\Scripts\powerlit analyze-query-pack --query-pack stability --limit 20
```

## 输出内容

每次检索默认会在 `output/` 下生成：

- `*.json`
- `*.csv`
- `*.md`

每次 AI 分析默认会在 `output/analysis/` 下生成：

- `*.json`
- `*.md`

每次 PDF 解析默认会在 `output/parsed/` 下生成：

- `*.md`

其中：

- `*.md` 是 AI 整理后的 Obsidian 兼容完整中文版笔记

每次导出下载清单默认会在 `download_list/` 下生成：

- `*.csv`
- `*.md`

当前常见字段包括：

- `title`
- `gbt7714_citation`
- `doi`
- `publisher_url`
- `researchgate_url`
- `researchgate_lookup_url`
- `acquisition_method`
- `acquisition_stage`
- `download_status`
- `local_pdf_path`
- `parsed_md_path`
- `analysis_md_path`
- `analysis_json_path`

## AI 分析输出结构

当前 AI 分析 Markdown 固定包含这些板块：

- 元数据
- 研究问题
- 电力系统场景
- 方法
- 数据与算例
- 关键发现
- 局限性
- 相关性
- 关键词
- 证据摘录
- 注意事项

如果提供的信息不足，系统会要求模型明确写 `unknown`，而不是自行补全。
如果你没有先绑定并解析 PDF，分析结果通常会更保守，`unknown` 会明显更多。

## Web API

当前提供的核心接口：

- `GET /health`
- `GET /providers`
- `GET /topics`
- `GET /papers`
- `GET /download-queue`
- `GET /analysis?doi=...`
- `POST /search`
- `POST /analyze`

## 推荐的 Windows 计划任务

可以在服务器上拆成两条计划任务：

1. 每天上午做检索和全文定位

```powershell
cd D:\OneDrive\【文献库】\AI专用文献库
.\.venv\Scripts\powerlit search-topic stability --providers crossref,openalex --limit 20
.\.venv\Scripts\powerlit resolve-fulltext --query-pack stability --limit 20
```

2. 每天晚上做分析

```powershell
cd D:\OneDrive\【文献库】\AI专用文献库
.\.venv\Scripts\powerlit analyze-query-pack --query-pack stability --limit 20
```

## 使用建议

- `IEEE / Elsevier` 建议继续作为权威元数据和全文入口，不要让 AI 替代数据库检索事实。
- `硅基流动` 更适合做结构化摘要、对比分析、周报生成和 Markdown 生成。
- 真正的“全文分析”建议等 `PDF -> Markdown / Text` 链路稳定后再批量开启。
- `Windows Server` 阶段保持 `SQLite` 足够；如果后面变成多人协作和高频定时任务，再迁到 `PostgreSQL`。

## 下一步

当前版本已经适合做：

- 电力系统文献检索与入库
- DOI / 国标引文整理
- 全文下载队列管理
- 调用外部 AI 生成结构化分析 Markdown

后续建议继续推进两块：

1. `PDF -> Markdown / Text` 自动解析
2. `周报 / 月报` 自动汇总与邮件推送

## 常用期刊下载命令

下面这几条命令是目前可以直接使用的期刊下载入口。PDF 会先下载到 `incoming_pdf/`，后续再跑入库和转写流程。

### 中文期刊

中国电机工程学报：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal pcsee --limit 100
```

电网技术：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal pst --limit 100
```

电力系统自动化：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal aeps --limit 100
```

单篇按 DOI 下载：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --doi 10.13334/j.0258-8013.pcsee.241273
```

说明：
- `pcsee / pst / aeps` 走的是官网或 CNKI 辅助下载。
- 如果本地浏览器 cookie 不能被程序直接复用，程序会自动打开一个辅助 Edge 窗口；你只需要在那个窗口里完成一次 CNKI/VPN 登录，后续会继续自动下载。
- 建议一次先跑一个期刊，`--limit 100` 作为默认批量上限更稳。

### IEEE 期刊

已登录 IEEE 后，IEEE 期刊现在可以走浏览器辅助下载链路，支持非 OA 全文。

IEEE Transactions on Power Systems：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tpwrs --limit 100
```

IEEE Transactions on Smart Grid：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tsg --limit 100
```

IEEE Transactions on Sustainable Energy：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tste --limit 100
```

也可以一次跑多个 IEEE 刊：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tpwrs,ieee_tsg,ieee_tste --limit 100
```

如果你只想下载 OA，可以继续使用：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_oa_pdfs.cmd --journal ieee_tpwrs,ieee_tsg,ieee_tste --limit 100
```

### Elsevier 期刊

Applied Energy：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal applied_energy --limit 100
```

Energy：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --journal energy --limit 100
```

单篇按 DOI 下载：
```bat
D:\OneDrive\【文献库】\AI专用文献库\incoming_pdf\download_assisted_pdfs.cmd --doi 10.1016/j.apenergy.2025.125978
```

说明：
- IEEE 和 Elsevier 的第一篇非 OA 文献通常会在辅助 Edge 窗口里跳到登录页；你手动登录一次后，同一进程里的后续文献会继续自动下载。
- Elsevier 按期刊批量下载依赖 `reference` 目录里已有对应 `_issue_catalog.json`；如果当前还没同步目录，先用 `--doi` 测单篇，或先运行期刊目录同步脚本。
- `download_assisted_pdfs.cmd` 是“已登录后抓取非 OA + OA”的主入口。
- `download_oa_pdfs.cmd` 仍然保留，适合只抓开放获取文献。

## 可直接复制的文献检索命令（2026-03-23 已核对）

下面这组命令已经按当前代码和 CLI 参数重新核对过，可以直接复制运行。

### 1. 先看当前支持的 provider / topic / journal

```powershell
.\.venv\Scripts\powerlit providers
.\.venv\Scripts\powerlit topics
.\.venv\Scripts\powerlit journals --journals-file config\journals.watch.example.yml
```

### 2. 关键词检索：概率最优潮流

```powershell
.\.venv\Scripts\powerlit search "chance-constrained optimal power flow probabilistic optimal power flow" --name chance-opf-test --providers crossref,openalex --limit 30 --from-date 2023-01-01
```

### 3. 用预设主题检索

当前 `config/topics.power-system.yml` 里可直接用的主题名包括：

- `stability`
- `optimal-power-flow`
- `renewable-integration`
- `distribution-network`
- `microgrid`
- `protection-control`
- `resilience`
- `storage`

常用示例：

```powershell
.\.venv\Scripts\powerlit search-topic optimal-power-flow --providers crossref,openalex --limit 50 --from-date 2022-01-01
.\.venv\Scripts\powerlit search-topic stability --providers crossref,openalex --limit 50 --from-date 2022-01-01
.\.venv\Scripts\powerlit search-topic distribution-network --providers crossref,openalex --limit 50 --from-date 2022-01-01
```

### 4. 按期刊检索：IEEE TSG / TPWRS / Applied Energy / Energy

```powershell
.\.venv\Scripts\powerlit sync-journal ieee_tsg --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200
.\.venv\Scripts\powerlit sync-journal ieee_tpwrs --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200
.\.venv\Scripts\powerlit sync-journal applied_energy --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200
.\.venv\Scripts\powerlit sync-journal energy --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200
```

如果要一次同步 watchlist 里的全部期刊：

```powershell
.\.venv\Scripts\powerlit sync-journal-bundle --journals-file config\journals.watch.example.yml
```

### 5. 查看检索结果和待下载清单

```powershell
.\.venv\Scripts\powerlit list-papers --query-pack chance-opf-test --limit 20
.\.venv\Scripts\powerlit resolve-fulltext --query-pack chance-opf-test --limit 20
.\.venv\Scripts\powerlit download-queue --query-pack chance-opf-test --limit 20
```

### 6. 批量下载 IEEE TSG PDF 到 incoming_pdf

如果你已经登录可访问 IEEE 的校园网 / VPN 环境，可直接跑：

```powershell
.\incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tsg --limit 100
```

等价的 CLI 写法是：

```powershell
.\.venv\Scripts\powerlit download-assisted-pdfs --journal ieee_tsg --limit 100 --echo-each
```

### 7. 把 incoming_pdf 中的 PDF 入库并解析

```powershell
.\incoming_pdf\process_incoming_pdf.cmd
```

如果只做识别、入库、解析，不跑 AI 分析：

```powershell
.\incoming_pdf\process_incoming_pdf.cmd --skip-analyze
```

### 8. 单篇 DOI 下载

```powershell
.\.venv\Scripts\powerlit download-assisted-pdfs --doi 10.1016/j.apenergy.2025.125978
.\.venv\Scripts\powerlit download-assisted-pdfs --doi 10.1109/tpwrs.2024.3502114
```

### 9. 常用组合流程

先按主题检索：

```powershell
.\.venv\Scripts\powerlit search-topic optimal-power-flow --providers crossref,openalex --limit 50 --from-date 2022-01-01
```

再生成待下载清单：

```powershell
.\.venv\Scripts\powerlit resolve-fulltext --query-pack optimal-power-flow --limit 50
.\.venv\Scripts\powerlit download-queue --query-pack optimal-power-flow --limit 50
```

再把下载好的 PDF 扔进 `incoming_pdf/` 后统一入库：

```powershell
.\incoming_pdf\process_incoming_pdf.cmd
```
