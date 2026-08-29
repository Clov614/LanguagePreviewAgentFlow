# 语言预习工作流 (Language Preview Agent Flow)

> 参考《skill+美剧 学英语也太高效了》的方法论: **不要背单词,要识别单词**。
> 先提取生词预习、建立短期记忆,再在阅读中识别 —— 把"追剧学英语"流程迁移到"读原著学英语"。

## 第一本书已完成:《Little Women》(Louisa May Alcott)

### 交付物

| 产物 | 位置 | 说明 |
|---|---|---|
| **Anki 卡片 ×47 章,696 张** | `data/output/little_women/chapter_XX_anki.tsv` | 每章一个 TSV,Anki 直接导入 |
| **生词标注版** | `data/output/little_women/annotated/chapter_XX.md` | 刷完卡,读书时**识别**高亮词 |
| **章节词表速查** | `annotated/chapter_XX_词表.md` | 每章生词 + 释义一览 |
| **推荐报告** | `data/output/little_women/recommend_report.md` | TOP 40 + 使用说明 |
| **生词总库** | `vocabulary/master_wordlist.csv` | 704 词,跨书累积、去重、状态流转 |

### 卡片内容(TSV 8 列)

```
单词 | 音标 | 词性 | 中文释义 | CEFR | 原文例句 | 例句译文 | 来源
```
- 例句全部取自**书中原句**(含该词的上下文),配人工润色译文 —— 与"看剧识别台词"同构
- 级别:B2/C1(考研英语二基线:排除 A1–A2,B1 少量,靠 BNC 词频过滤常见词)

### Anki 导入方法

1. 打开 Anki 桌面版 → File → Import
2. 选择 `chapter_XX_anki.tsv`(编码选 UTF-8,分隔符 Tab)
3. 字段映射:按顺序对应即可(首行有字段名)
4. 建议:每章建一个牌组,或全部导入同一牌组按来源筛选

### 推荐学习节奏(与视频一致)

1. **刷卡 15 分钟**:导入该章卡片,刷到认识为止(短期记忆)
2. **读书识别**:读对应章节(参考 `annotated/` 高亮版),在语境中认出背过的词
3. **标记掌握**:在 `known_words.txt` 追加已掌握词,后续不再重复推荐

### 管线(第二本书怎么用)

```bash
# 1. EPUB 放入 data/books/,运行核心管线(分词、CEFR 判定、选词、例句)
uv run python scripts/pipeline.py --book <书名>

# 2. 输出工作单 → 模型润色中文释义 + 例句译文(polish_*.json)
# 3. 合并润色
uv run python scripts/apply_polish.py --book <书名> --polish <json> [--ai-en <json>]

# 4. 生成 Anki TSV + 更新生词总库(自动跨书去重)
uv run python scripts/cards.py --book <书名>

# 5. 生成生词标注版 + 推荐报告
uv run python scripts/annotate.py --book <书名>
uv run python scripts/report.py --book <书名>
```

### 资源与结构

- `resources/`: Oxford 3000/5000 CEFR 词表、ECDICT 340 万词条本地词典(sqlite)
- `scripts/`: pipeline / apply_polish / cards / annotate / report / build_dict_db
- `tools/wenyi/`: 参考项目(MIT),未来美剧模式复用其 SRT 说明书
- 语言适配层:日语等新语言只需新增 adapter(见 PLAN.md §7)

---

## 环境准备:词表资源获取

> 大型词表文件(数百 MB)不入仓库,clone 后按下面两步准备,之后全程**离线可用**。

### 1. 本地词典 `resources/ecdict.db`(音标/释义/词频,约 294MB)

管线依赖此 sqlite 词典,由 ECDICT 的 stardict 数据本地构建:

```bash
# ① 下载 https://github.com/skywind3000/ECDICT/releases 的「ECDICT - 28」版本
#    → 资产 ecdict-stardict-28.zip(约 70MB,MIT 许可)
# ② 解压到 resources/stardict-ecdict-2.4.2/(含 .idx / .dict / .ifo 三个文件)
# ③ 构建词典数据库
uv run python scripts/build_dict_db.py   # 340 万词条 → resources/ecdict.db
```

### 2. 已随仓库的资源(无需下载)

| 资源 | 来源 | 说明 |
|---|---|---|
| `resources/oxford3000-5000/` | github.com/chunzhng/Oxford-3000-5000 | oxford-5000.csv 含 A1–C1 全部级别(3000 的超集),CEFR 判定主词表 |
| `resources/ecdict.mini.csv` | ECDICT(skywind3000, MIT) | 精简词表,快速分词/词频辅助 |

### 数据与第三方许可

- 示例书《Little Women》(1868–1869, Louisa May Alcott)**公有领域**,随仓库作为演示数据与例句来源
- `tools/wenyi/`:BigDawnGhost/wenyi, **MIT**,快照随仓库(含 LICENSE)
- ECDICT 词表数据:**MIT**(skywind3000/ECDICT)
- 牛津词表:来源为公开 GitHub 仓库,仅用于个人学习

### 已知限制

- 级别标注重镜像词表校准过,个别词级别可能有偏差(以 BNC 词频校准)
- 例句抽取优先短句,个别例句非最优上下文;补句(AI 生成)已在卡片中标注
- 生僻度打分是启发式的,如需更严/更松可在 `scripts/pipeline.py` 调整配比