# AGENTS.md — AI Agent 工作指引

开始任何修改前,**先读 `README.agent.md`**(AI Agent 版操作手册),它是权威文档;
人类用户文档是 `README.md`。

项目一句话:英语原著 EPUB → 每章生词卡(Anki TSV)+ 生词总库 + 标注版,支持预生成英音发音。

硬性不变式(违反前先停下向用户确认):
1. anki TSV:UTF-8 **无 BOM**、严格 **10 列**(2026-08-30 用户确认由 8 列扩列,
   增「AI解析」「词义概述」两列,可空);raw CSV:UTF-8 **带 BOM**。
2. `vocabulary/master_wordlist.csv` 跨书合并**幂等**;`known_words.txt` 的词绝不重复推荐。
3. 管线输出与总库/历史数据矛盾:**停下说明,不静默覆盖**。
4. AI 补句必须标注;发音 mp3 由 gen_audio.py 生成(缓存不入 git)。
5. 不主动提交 git。
6. 统一入口:`uv run python scripts/run.py --book <书名> [--audio] [--stage ...]`,单脚本可独立直调。
7. 脚本默认零模型调用;`ai_polish.py`/`ai_explain.py`/`ai_pick_phrases.py` 是模型阶段
   (run.py 一键管线自动调用,断点安全、失败词记 failed.json),缺 key 管线照跑。
8. 书内专名按书外置 `data/books/proper_names/<book>.txt`(pipeline/hard_words/validate 共用);
   缺文件=不排除专名。新书 EPUB 先经 `epub_to_md.py` 转换、`scan_proper.py` 扫描确认专名表,
   再跑 pipeline(2026-08-31 起,不再改 pipeline.py 里的人物表)。
9. **例句齐全**:要出卡的词(有润色)最终 TSV 的「原文例句」必须非空——由 run.py 的
   polish 阶段(ai_polish.py,无原句时模型生成例句)保证;validate 对违反者非零退出。
   pipeline 重生成 raw 时必须继承旧文件的润色/解析列(2026-08-31 起内置)。