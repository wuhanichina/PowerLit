# 中科院期刊白名单导入说明

`PowerLit` 现在支持把中科院期刊分区表作为检索白名单使用。

## 目标

- 只保留 `journal article`
- 只保留 `1区 / 2区` 期刊
- 自动排除会议论文和不在白名单内的期刊

## 放置位置

默认文件路径：

```text
config/cas_journal_whitelist.xlsx
```

也可以在 `.env` 里改成别的路径：

```env
POWERLIT_CAS_JOURNAL_LIST_PATH=config/cas_journal_whitelist.xlsx
POWERLIT_CAS_MAX_QUARTILE=2
```

## 支持格式

- `.xlsx`
- `.csv`

## 最低字段要求

白名单文件至少要能识别出两列：

- 期刊名
- 分区

当前代码会自动尝试识别这些常见表头：

### 期刊名列

- `journal_title`
- `source_title`
- `期刊名称`
- `刊名`
- `刊名（英文）`

### 分区列

- `大类分区`
- `升级版大类分区`
- `基础版大类分区`
- `分区`
- `小类分区`
- `quartile`
- `cas_quartile`

## 分区值支持

下面这些写法都能识别：

- `1区`
- `2区`
- `Q1`
- `Q2`
- `一区`
- `二区`

## 模板

项目里已经放了一个最小模板：

```text
config/cas_journal_whitelist.template.csv
```

你可以先用这个模板理解字段，再替换成官方导出的正式文件。

## 启用方式

只要 `POWERLIT_CAS_JOURNAL_LIST_PATH` 指向的文件存在，检索时就会自动启用白名单过滤。

启用后会发生两件事：

1. 非期刊文献被排除
2. 不在 `1区/2区` 白名单内的期刊被排除

## 当前限制

当前版本默认按 `期刊名` 匹配白名单。

这对主流英文期刊基本够用，但如果后续你发现个别刊名存在别名、缩写或格式差异，下一步可以继续补 `ISSN` 精确匹配。
