# incoming_pdf 使用说明

这份说明默认你已经在仓库根目录运行命令。

```cmd
cd <PowerLit 项目目录>
```

如果从其他目录运行，请把下面的相对路径换成你本机的项目路径。公开文档中不写固定个人目录。

## 先做什么

`download_assisted_pdfs.cmd` 不是直接联网搜整本期刊，它依赖本地的“卷期目录缓存”。
所以第一次跑某本期刊前，先同步一次期刊卷期目录。

期刊卷期目录同步命令：

```cmd
incoming_pdf\sync_journal_issue_catalogs.cmd --journal ieee_tsg
incoming_pdf\sync_journal_issue_catalogs.cmd --journal ieee_tpwrs
incoming_pdf\sync_journal_issue_catalogs.cmd --journal ieee_tste
incoming_pdf\sync_journal_issue_catalogs.cmd --journal pst
incoming_pdf\sync_journal_issue_catalogs.cmd --journal aeps
incoming_pdf\sync_journal_issue_catalogs.cmd --journal pcsee
incoming_pdf\sync_journal_issue_catalogs.cmd --journal applied_energy
incoming_pdf\sync_journal_issue_catalogs.cmd --journal energy
```

如果你想一次把这 8 本期刊都同步完，也可以用：

```cmd
incoming_pdf\sync_journal_issue_catalogs.cmd
```

## 批量辅助下载

### IEEE Transactions on Smart Grid

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tsg --limit 100
```

### IEEE Transactions on Power Systems

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tpwrs --limit 100
```

### IEEE Transactions on Sustainable Energy

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tste --limit 100
```

### 电网技术

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal pst --limit 100
```

### 电力系统自动化

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal aeps --limit 100
```

### 中国电机工程学报

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal pcsee --limit 100
```

### Applied Energy

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal applied_energy --limit 100
```

### Energy

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal energy --limit 100
```

### 一次下载多个期刊

```cmd
incoming_pdf\download_assisted_pdfs.cmd --journal ieee_tsg,ieee_tpwrs,ieee_tste,pst,aeps,pcsee,applied_energy,energy --limit 100
```

## IEEE / Elsevier 首次运行说明

第一次跑 IEEE 或 Elsevier 辅助下载时，程序可能会拉起一个 Edge 辅助窗口。

你需要在那个窗口里：

1. 保持窗口不要关掉。
2. 完成学校 VPN / 校园网访问或 IEEE / Elsevier 登录。
3. 等程序继续自动点击 PDF 下载。

如果你把辅助 Edge 窗口关掉了，当前这一轮会失败。
重新运行同一条命令即可。

## 单篇 DOI 下载

```cmd
incoming_pdf\download_assisted_pdfs.cmd --doi 10.1109/tpwrs.2024.3502114
incoming_pdf\download_assisted_pdfs.cmd --doi 10.1016/j.apenergy.2025.125978
incoming_pdf\download_assisted_pdfs.cmd --doi 10.13334/j.0258-8013.pcsee.242015
```

## 只下载开放获取 PDF

```cmd
incoming_pdf\download_oa_pdfs.cmd --journal ieee_tsg --limit 100
incoming_pdf\download_oa_pdfs.cmd --journal ieee_tpwrs --limit 100
incoming_pdf\download_oa_pdfs.cmd --journal ieee_tste --limit 100
incoming_pdf\download_oa_pdfs.cmd --journal applied_energy --limit 100
incoming_pdf\download_oa_pdfs.cmd --journal energy --limit 100
```

## 下载后如何入库

### 只做登记入库

`process_incoming_pdf.cmd` 当前对应 `register-incoming-pdf`，只做：

- DOI 识别
- 元数据登记
- PDF 挂接到库

```cmd
incoming_pdf\process_incoming_pdf.cmd
```

### 做完整处理

`process_incoming_pdf_full.cmd` 当前对应 `process-incoming-pdf`，会执行：

- 识别 DOI
- 补元数据
- 移动到 `literature/reference/...`
- 解析 PDF
- 生成 Obsidian 笔记
- 可选继续跑 AI 分析

```cmd
incoming_pdf\process_incoming_pdf_full.cmd
```

如果只做“入库 + 解析”，不跑 AI 分析：

```cmd
incoming_pdf\process_incoming_pdf_full.cmd --skip-analyze
```
