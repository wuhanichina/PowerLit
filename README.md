# PowerLit

> 面向电力系统研究的本地优先文献库工具，用来把论文检索、元数据归档、PDF 入库、全文解析、AI 笔记和分析结果放进一套可维护的研究工作流。

`PowerLit` 面向个人或小团队的电力系统文献管理场景，采用 `可信元数据 + 本地索引 + AI 深度整理` 的产品路线。`Crossref`、`OpenAlex`、`IEEE Xplore`、`Elsevier` 等来源提供可追溯题录，SQLite 维护本地研究资产，AI 将全文转化为 Obsidian 笔记、结构化分析和后续报告素材。

项目的核心判断是：`文献事实由学术数据库与出版商元数据托底；AI 聚焦文本理解、知识组织和研究助理工作。` 这条路线让检索、入库、解析和分析都保持可追溯、可复核、可持续扩展。

[使用场景](#使用场景) · [灵感来源](#灵感来源) · [项目特色](#项目特色) · [五分钟上手](#五分钟上手) · [PowerLit 管理哪些材料](#powerlit-管理哪些材料) · [常见任务怎么做](#常见任务怎么做) · [推荐工作流](#推荐工作流) · [目录结构](#目录结构) · [配置说明](#配置说明) · [Web 与 API](#web-与-api) · [提交边界](#提交边界) · [版本说明](#版本说明)

---

## 使用场景

PowerLit 适合下面这类文献工作：

- 围绕电力系统、优化、稳定性、调度、储能、新能源并网等方向持续维护论文库。
- 需要从 DOI、题名、期刊、年份、国标引文、下载状态一路追溯到 PDF、解析文本和 AI 分析。
- PDF 和数据库保存在本机、NAS、网盘或服务器挂载盘中，代码仓库只保存工具本身。
- 需要把人工下载、自动入库、全文解析、Obsidian 笔记和批量分析串成固定流程。
- 希望未来让 Windows 计划任务或服务器定时执行检索、下载队列、解析和报告生成。

PowerLit 的最佳位置是一套研究组内部的本地文献流水线：它连接学术元数据、PDF 文件、Markdown 笔记、AI 分析和后续报告，让文献资产在一个可控目录中持续生长。

---

## 灵感来源

PowerLit 的主要灵感来自两个 GitHub 项目：

- [SocialCatalystLab/ape-papers](https://github.com/SocialCatalystLab/ape-papers)：一个公开工作论文档案库，把每篇论文组织成独立条目，并把 PDF、LaTeX 源码、分析代码和复现数据放在同一个可浏览仓库中。它启发了 PowerLit 对“论文条目即研究资产”的理解：每篇论文都应有稳定 ID、目录位置、元数据、文件路径和后续分析记录。
- [akariasai/openscholar](https://github.com/akariasai/openscholar)：OpenScholar 展示了面向科学文献的检索增强语言模型路线，通过检索相关论文并基于来源生成回答。它启发了 PowerLit 对“文献库应服务 AI 研究助理”的理解：本地论文库不仅保存文件，也应为检索、综合、问答、RAG 和写作支持提供结构化底座。

围绕这两个核心参考，PowerLit 也吸收了 GitHub 上同类 paper database、literature manager、literature review pipeline 和 PDF parsing 项目的组织方式：

| 参考方向 | 代表项目 | 给 PowerLit 的启发 | PowerLit 的产品化落点 |
| --- | --- | --- | --- |
| 公开论文档案库 | [SocialCatalystLab/ape-papers](https://github.com/SocialCatalystLab/ape-papers) | 论文集合可以像数据库一样拥有索引、条目目录、附件和复现材料 | 用 DOI、主题包、工作区和论文卡片组织电力系统文献资产 |
| 检索增强科学文献综合 | [akariasai/openscholar](https://github.com/akariasai/openscholar) | 文献库可以成为 AI 检索、综合和回答问题的证据底座 | 将元数据、PDF 解析结果、Markdown 笔记和分析 JSON 汇入后续 RAG 工作流 |
| 本地文献数据库 | [jkitchin/litdb](https://github.com/jkitchin/litdb) | 用数据库收集、检索和持续维护 scientific literature | 以 SQLite 管理 DOI、题录、PDF 路径、解析路径、分析路径和工作区关系 |
| Living survey / paper collection | [EshaanAgg/Research-Literature-Manager](https://github.com/EshaanAgg/Research-Literature-Manager)、[papers-we-love/papers-we-love](https://github.com/papers-we-love/papers-we-love) | 用 GitHub 仓库沉淀论文集合、主题分类和长期阅读材料 | 用主题包、工作区和论文卡片承接持续更新的领域文献库 |
| PDF 转 Markdown / JSON 工具 | [opendatalab/MinerU](https://github.com/opendatalab/MinerU) | 将复杂 PDF 等文档解析为适合 LLM 工作流使用的 Markdown / JSON | 接入本地 MinerU 和官方 MinerU API，把解析产物关联到 DOI、期刊目录、Obsidian 笔记和分析记录 |

这些项目共同证明：论文库可以从“文件夹里的 PDF”升级为“可运行的研究数据库”。PowerLit 在这个谱系上聚焦电力系统方向，把元数据可信性、本地/NAS 文件管理、人工下载现实、中文期刊入口、AI 笔记和批量分析整合进同一套轻量工具。

---

## 项目特色

- **可信元数据优先**：检索事实来自学术数据库和出版商元数据，题名、DOI、期刊、年份和引文可以逐条追溯。
- **本地优先，NAS 友好**：SQLite、PDF、Markdown、JSON 和报告可以放在本机、NAS、网盘或服务器挂载盘中，代码仓库保持轻量。
- **从 DOI 到 AI 笔记的闭环**：一篇论文可以从检索入库一路走到 PDF 绑定、全文解析、Obsidian 笔记、结构化分析和论文卡片。
- **面向电力系统场景优化**：内置电力系统主题、期刊 watchlist、中文期刊辅助下载入口和中科院分区白名单流程。
- **人工下载与自动化协同**：`incoming_pdf/` 支持人工下载后的自动识别、重命名、搬运、解析和分析，适合校园网、VPN 和机构订阅环境。
- **Web UI、CLI、API 三入口**：日常浏览用 Web，批量任务用 CLI，后续集成用 HTTP API。
- **费用和状态可追踪**：解析、分析、下载、绑定、工作区、论文卡片等状态写回本地索引，AI 费用元数据可进入记录。
- **研究资产可交接**：仓库保存工具和模板，文献资产保存在受管目录，让个人电脑、NAS 和服务器之间的迁移更清晰。

---

## 五分钟上手

### 1. 克隆项目并安装依赖

```powershell
git clone https://github.com/wuhanichina/PowerLit.git
cd PowerLit
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

macOS / Linux 可使用：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

如果使用 `uv`：

```bash
uv sync --extra dev
```

### 2. 复制配置模板

```powershell
Copy-Item .env.example .env
Copy-Item config\ai.example.yml config\ai.yml
```

`.env` 和 `config/ai.yml` 已被 `.gitignore` 忽略，真实 API Key 留在本机或服务器环境中。

### 3. 配置文献库路径

本地使用时可以保留默认相对路径。若文献库已经放到 NAS 或网盘，建议在 `.env` 中显式指定：

```env
POWERLIT_LITERATURE_ROOT=/Volumes/PowerLit/literature
POWERLIT_REFERENCE_DIR=/Volumes/PowerLit/literature/reference
POWERLIT_MD_DIR=/Volumes/PowerLit/literature/md
POWERLIT_METADATA_DIR=/Volumes/PowerLit/literature/metadata
POWERLIT_OUTPUT_DIR=/Volumes/PowerLit/literature/metadata
POWERLIT_DOWNLOAD_LIST_DIR=/Volumes/PowerLit/literature/metadata/download_list
POWERLIT_DB_PATH=/Volumes/PowerLit/literature/metadata/papers.db
POWERLIT_INCOMING_PDF_DIR=/Volumes/PowerLit/incoming_pdf
POWERLIT_PARSED_OUTPUT_DIR=/Volumes/PowerLit/literature/json
POWERLIT_ANALYSIS_OUTPUT_DIR=/Volumes/PowerLit/literature/json
```

### 4. 检查命令是否可用

```powershell
.\.venv\Scripts\powerlit --help
.\.venv\Scripts\powerlit providers
```

### 5. 启动 Web UI

```powershell
.\.venv\Scripts\powerlit serve
```

浏览器打开：

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/library`
- `http://127.0.0.1:8000/queue`
- `http://127.0.0.1:8000/docs`

### 6. 跑一次最小检索

```powershell
.\.venv\Scripts\powerlit search "power system stability" --name stability-test --providers crossref,openalex --limit 20
.\.venv\Scripts\powerlit list-papers --query-pack stability-test --limit 10
```

---

## PowerLit 管理哪些材料

| 对象 | 默认位置 | 作用 |
| --- | --- | --- |
| 程序代码 | `src/powerlit/` | CLI、Web UI、API、检索、入库、解析、分析服务 |
| 配置模板 | `.env.example`、`config/*.example.*` | 提供可提交的配置样例，真实 key 留在本机 |
| 文献数据库 | `literature/metadata/papers.db` 或 NAS 路径 | 保存论文元数据、路径、状态、工作区关系 |
| PDF 原文 | `literature/reference/<journal>/<vXX>/<iXX>/` | 保存受管 PDF 文件 |
| 人工中转 | `incoming_pdf/` | 放人工下载 PDF，并通过脚本自动登记入库 |
| 解析结果 | `literature/json/`、`literature/md/` | 保存全文解析 JSON、Markdown、Obsidian 笔记 |
| AI 分析 | `literature/json/`、`literature/md/` | 保存分析 JSON / Markdown 和费用元数据 |
| 下载清单 | `literature/metadata/download_list/` | 生成人工下载用的 CSV / Markdown 队列 |
| 维护脚本 | `scripts/maintenance/` | 长任务、批处理、审计、MinerU 批量解析 |
| 项目文档 | `docs/` | 架构说明、路线图、专题指南 |

---

## 常见任务怎么做

| 任务 | 先看哪里 | 常用命令或入口 | 完成信号 |
| --- | --- | --- | --- |
| 初始化项目 | `.env.example`、`config/ai.example.yml` | `pip install -e ".[dev]"` | `powerlit --help` 正常显示 |
| 打开 Web UI | `src/powerlit/api/app.py` | `powerlit serve` | 首页、文献库、队列页可打开 |
| 检索论文 | `config/topics.power-system.yml` | `powerlit search ...`、`powerlit search-topic ...` | 数据库新增记录，导出 JSON / CSV |
| 查看文献库 | Web `/library` 或 CLI | `powerlit list-papers` | 能看到题名、DOI、引文、状态 |
| 生成下载队列 | `download_list/` | `powerlit resolve-fulltext`、`powerlit download-queue` | 生成待下载 CSV / Markdown |
| 绑定 PDF | `incoming_pdf/` | `powerlit attach-pdf` 或 `process_incoming_pdf.cmd` | PDF 移入受管目录，数据库写入 `local_pdf_path` |
| 解析 PDF | `services/pdf_parser.py` | `powerlit parse-pdf`、`parse-query-pack` | 生成解析 JSON / Markdown |
| 生成 AI 分析 | `services/ai_analysis.py` | `powerlit analyze-paper`、`analyze-query-pack` | 生成分析 JSON / Markdown |
| 建立工作区 | Web API 或 CLI | `workspace-create`、`workspace-add`、`workspace-show` | 工作区能按 DOI 或主题包聚合论文 |
| 按期刊维护 | `config/journals.watch.example.yml` | `sync-journal`、`sync-journal-bundle` | 期刊元数据和下载队列更新 |
| 排查路径 | `.env`、`settings.py` | `powerlit providers`、`list-papers` | 输出路径指向预期本地盘或 NAS |

---

## 推荐工作流

### 从检索到入库

1. 用 `search` 或 `search-topic` 从 Crossref / OpenAlex 获取元数据。
2. 元数据写入 `papers.db`，同时导出 JSON / CSV。
3. 用 `list-papers` 或 Web `/library` 检查 DOI、引文、年份和来源。
4. 用 `resolve-fulltext` 补全文候选入口。
5. 用 `download-queue` 生成人工下载清单。

### 从 PDF 到笔记

1. 把 PDF 放入 `incoming_pdf/`。
2. 运行 `incoming_pdf/process_incoming_pdf.cmd` 或 CLI `attach-pdf`。
3. PowerLit 根据 DOI 和期刊信息把 PDF 移入 `literature/reference/...`。
4. 运行 `parse-pdf` 或 `parse-query-pack`。
5. 解析结果写入 JSON / Markdown，并登记回数据库。

### 从笔记到分析

1. 运行 `analyze-paper --doi ...` 分析单篇。
2. 或运行 `analyze-query-pack --query-pack ...` 批量分析一个主题包。
3. 分析结果保存为 JSON / Markdown。
4. Web UI 和 CLI 可继续读取分析路径、费用和状态。

### 从期刊到自动维护

1. 在 `config/journals.watch.example.yml` 中维护期刊 watchlist。
2. 用 `sync-journal` 同步单个期刊，或 `sync-journal-bundle` 批量同步。
3. 可选执行 OA PDF 下载。
4. 后续统一进入 PDF 解析、AI 笔记、分析和报告流程。

---

## 目录结构

代码仓库保持轻量，只保存工具和模板：

```text
.
├── src/powerlit/              # PowerLit 核心 Python 包
│   ├── api/                   # FastAPI Web UI 和 HTTP API
│   ├── providers/             # Crossref / OpenAlex / IEEE / Elsevier 元数据源
│   ├── services/              # 检索、索引、解析、AI、下载、报告等服务
│   ├── static/                # Web UI 静态资源
│   └── templates/             # Web UI 页面模板
├── config/                    # 可提交配置模板和主题/期刊示例
├── docs/                      # 架构说明、指南和路线图
├── incoming_pdf/              # Windows 入库与下载辅助脚本
├── scripts/maintenance/       # 批量维护、审计和长任务脚本
├── tools/                     # PDF 文本提取等辅助工具
├── .env.example               # 环境变量模板
├── pyproject.toml             # Python 包配置
└── README.md                  # 项目说明页
```

真实文献库数据保留在本机、NAS 或服务器挂载盘：

```text
literature/
├── reference/                 # PDF 原文
├── md/                        # Obsidian 笔记和分析 Markdown
├── json/                      # 解析 JSON、分析 JSON、RAG 输入
├── metadata/                  # papers.db、导出文件、下载清单
├── index/                     # 向量索引或全文索引
└── reports/                   # 周报、月报、汇总报告
```

如果使用 NAS 或网盘，上面这套 `literature/` 可以完全放在挂载盘中，只在 `.env` 中指定路径。

---

## 关键目录职责

`src/powerlit/api/` 提供 Web UI 和 HTTP API。基础页面包括仪表盘、文献库、下载队列和 Swagger API 文档。真实文献库较大时，Web UI 首屏使用轻量预览，让页面保持快速打开。

`src/powerlit/providers/` 负责对接外部元数据源。当前可直接使用 `Crossref` 和 `OpenAlex`，并预留 `IEEE Xplore`、`Elsevier Scopus` 等接口。

`src/powerlit/services/index.py` 负责本地 SQLite 索引，保存论文元数据、下载状态、PDF 路径、解析路径、分析路径、工作区关系和主题包。

`incoming_pdf/` 是人工下载到正式文献库的中转站。你可以把 PDF 放入该目录，再运行对应 `.cmd`，让系统识别 DOI、补元数据、重命名、搬运、解析和分析。

`scripts/maintenance/` 放长任务和批量任务，例如全库 MinerU API 批处理、批量解析、元数据审计和 incoming_pdf 一键流水线。

`docs/` 用于放更稳定的项目文档。README 只保留入口级说明，详细设计和专题流程逐步沉淀到 `docs/`。

---

## 配置说明

### 本地路径

| 变量 | 说明 |
| --- | --- |
| `POWERLIT_LITERATURE_ROOT` | 文献库根目录 |
| `POWERLIT_REFERENCE_DIR` | PDF 受管目录 |
| `POWERLIT_MD_DIR` | Markdown 笔记目录 |
| `POWERLIT_METADATA_DIR` | 元数据目录 |
| `POWERLIT_DB_PATH` | SQLite 数据库路径 |
| `POWERLIT_INCOMING_PDF_DIR` | 人工下载 PDF 中转目录 |
| `POWERLIT_PARSED_OUTPUT_DIR` | 解析 JSON 输出目录 |
| `POWERLIT_ANALYSIS_OUTPUT_DIR` | 分析 JSON 输出目录 |
| `POWERLIT_DOWNLOAD_LIST_DIR` | 下载清单输出目录 |

### 元数据源

| 变量 | 说明 |
| --- | --- |
| `POWERLIT_CROSSREF_MAILTO` | Crossref 推荐填写联系邮箱 |
| `POWERLIT_UNPAYWALL_EMAIL` | Unpaywall 查询邮箱 |
| `POWERLIT_IEEE_API_KEY` | IEEE Xplore API Key |
| `POWERLIT_ELSEVIER_API_KEY` | Elsevier API Key |
| `POWERLIT_ELSEVIER_INSTTOKEN` | Elsevier InstToken，可选 |
| `POWERLIT_SERPAPI_API_KEY` | ResearchGate 精确链接搜索，可选 |

### AI 配置

最小 `.env` 配置：

```env
POWERLIT_AI_PROVIDER=siliconflow
POWERLIT_AI_BASE_URL=https://api.siliconflow.cn/v1
POWERLIT_AI_API_KEY=your-api-key
POWERLIT_AI_MODEL=Qwen/Qwen2.5-72B-Instruct
POWERLIT_AI_NOTE_TIMEOUT=600
POWERLIT_AI_NOTE_SOURCE_CHAR_LIMIT=90000
```

更推荐在 `config/ai.yml` 中维护多个 profile，并用任务类型选择不同模型。模板见 `config/ai.example.yml`。

---

## Web 与 API

启动：

```powershell
.\.venv\Scripts\powerlit serve
```

常用页面：

- `/`：仪表盘
- `/library`：本地文献库
- `/queue`：下载队列和 PDF 绑定入口
- `/docs`：Swagger API 文档

核心 API：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/providers` | 查看 provider 状态 |
| `GET` | `/topics` | 查看预设主题 |
| `GET` | `/papers` | 列出论文 |
| `GET` | `/download-queue` | 查看下载队列 |
| `POST` | `/search` | 执行元数据检索并入库 |
| `POST` | `/workspaces` | 创建工作区 |
| `POST` | `/workspaces/{name}/members` | 向工作区添加 DOI 或主题包 |
| `POST` | `/paper-card/build` | 生成论文卡片 |
| `POST` | `/analyze` | 调用 AI 分析单篇论文 |

---

## CLI 速查

### 检索与入库

```powershell
.\.venv\Scripts\powerlit providers
.\.venv\Scripts\powerlit topics
.\.venv\Scripts\powerlit search "power system stability" --providers crossref,openalex --limit 20
.\.venv\Scripts\powerlit search-topic stability --providers crossref,openalex --limit 20
.\.venv\Scripts\powerlit list-papers --limit 20
```

### 全文定位与下载队列

```powershell
.\.venv\Scripts\powerlit resolve-fulltext --query-pack stability --limit 20
.\.venv\Scripts\powerlit download-queue --query-pack stability --limit 20
```

### PDF 入库与解析

```powershell
.\.venv\Scripts\powerlit attach-pdf --doi 10.1109/example --file incoming_pdf\example.pdf
.\.venv\Scripts\powerlit parse-pdf --doi 10.1109/example
.\.venv\Scripts\powerlit parse-query-pack --query-pack stability --limit 20
```

### AI 分析

```powershell
.\.venv\Scripts\powerlit analyze-paper --doi 10.1109/example
.\.venv\Scripts\powerlit analyze-query-pack --query-pack stability --limit 20
```

### 工作区

```powershell
.\.venv\Scripts\powerlit workspace-create stability-review --description "stability papers"
.\.venv\Scripts\powerlit workspace-add stability-review --doi 10.1109/example
.\.venv\Scripts\powerlit workspace-show stability-review
```

### 按期刊同步

```powershell
.\.venv\Scripts\powerlit sync-journal ieee_tpwrs --journals-file config\journals.watch.example.yml --from-year 2015 --limit 200
.\.venv\Scripts\powerlit sync-journal-bundle --journals-file config\journals.watch.example.yml
```

---

## 中科院白名单

PowerLit 支持把 `中科院期刊分区表` 作为检索过滤白名单。完整分区表由使用者从官方平台导出后放到本地：

```env
POWERLIT_CAS_JOURNAL_LIST_PATH=config/cas_journal_whitelist.xlsx
POWERLIT_CAS_MAX_QUARTILE=2
```

模板文件：

- `config/cas_journal_whitelist.template.csv`

详细说明见：

- `docs/guides/cas-whitelist-import.md`

---

## 提交边界

本仓库保存程序代码、配置模板、流程脚本和项目文档。以下内容默认保留在本机、NAS 或服务器挂载盘：

- `.env`
- `config/ai.yml`
- `.venv/`
- `node_modules/`
- `literature/`
- `output/`
- `download_list/`
- `test/`
- `local/`
- SQLite 数据库、日志、PID、缓存文件

如果部署环境使用 NAS 或网盘，整理和提交本仓库文件只影响代码仓库。运行检索、入库、下载、解析、分析等业务命令时，PowerLit 会按 `.env` 中的路径读写外部文献库。

---

## 验证

提交前建议至少执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts\maintenance
.\.venv\Scripts\powerlit --help
```

需要做隔离业务测试时，可把 `POWERLIT_*` 路径临时指向 `/tmp` 或其他测试目录，让测试产物进入独立沙箱。

当前项目整理时已做过一轮隔离业务测试，覆盖 Web 页面、API 检索入库、工作区、论文卡片、PDF 绑定、本地文件访问和 CLI 基础命令。

---

## 版本说明

当前版本：`0.1.0`

这是一个本地优先的早期可用版本，已经覆盖：

- 文献元数据检索和归档
- SQLite 本地索引
- 下载队列和 PDF 绑定
- incoming_pdf 中转流程
- PDF 解析入口
- AI 笔记和分析结果生成
- 基础 Web UI 和 HTTP API
- 工作区和论文卡片

现阶段更适合作为个人或小团队的研究工作流工具使用。配置项、目录结构和部分维护脚本仍会随实际使用继续调整。

---

## 下一步

路线图见：

- `docs/roadmap.md`

近期更值得继续完善的方向：

1. 把 README 中的长流程进一步拆到 `docs/`。
2. 为 Web UI 增加更完整的业务测试。
3. 强化 NAS / Windows Server 部署说明。
4. 完善周报、月报和主题综述生成流程。

---

## License

MIT License. See `LICENSE`.
