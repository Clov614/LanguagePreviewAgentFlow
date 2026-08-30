"""批量生成单词/例句英音 mp3(阶段 7.5,在 cards.py 之前跑)。

用法:  uv run python scripts/gen_audio.py --book little_women            # 全书
       uv run python scripts/gen_audio.py --book little_women --chapter 1
输出:  data/output/<book>/anki/audio/*.mp3,并自动拷入 Anki collection.media
      (单配置自动定位;多配置用 --media-dir;--no-copy 只生成不拷贝)
特点:  UDP式免费 edge-tts(微软 Edge 神经语音),无需 key;
       已有文件跳过(缓存),--force 重生成;限流 + 重试;失败词条汇总报告。
"""
import argparse
import asyncio
import csv
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent.parent

import edge_tts  # noqa: E402

from tts_paths import (  # noqa: E402
    DEFAULT_VOICE,
    audio_dir_for_book,
    sent_audio_name,
    word_audio_name,
)

CONCURRENCY = 4
RETRIES = 3


def load_rows(book: str, chapter: int | None):
    """与 cards.py 同源:单词 raw + 表达 raw(chapter_XX_phrase_raw.csv,仅例证句音频),
    utf-8-sig,只取已润色的行。"""
    raw_dir = BASE / "data" / "output" / book / "raw"
    word_files = sorted(f for f in raw_dir.glob("chapter_*_raw.csv")
                        if not f.name.endswith("_phrase_raw.csv"))
    phrase_files = sorted(raw_dir.glob("chapter_*_phrase_raw.csv"))
    if chapter is not None:
        word_files = [f for f in word_files if f.name.startswith(f"chapter_{chapter:02d}_")]
        phrase_files = [f for f in phrase_files if f.name.startswith(f"chapter_{chapter:02d}_")]
    rows = []
    for f in word_files + phrase_files:
        ch = int(f.stem.split("_")[1])
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("cn_mean"):
                    rows.append((ch, r))
    return rows


async def gen_one(text: str, path: Path, voice: str, rate: str, force: bool) -> str:
    if path.exists() and not force:
        return "skip"
    for attempt in range(RETRIES):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
            return "ok"
        except Exception as exc:  # 网络抖动/服务端限流,退避重试
            if attempt == RETRIES - 1:
                return f"fail: {exc}"
            await asyncio.sleep(2**attempt)
    return "fail: unknown"  # 不可达


async def run_generation(text_path_pairs, voice, rate, force):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def worker(pair):
        async with sem:
            return await gen_one(*pair, voice, rate, force)

    return await asyncio.gather(*[worker(p) for p in text_path_pairs])


def resolve_media_dir(args) -> Path | None:
    if args.media_dir:
        return Path(args.media_dir)
    if args.no_copy:
        return None
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / "Anki2"
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Roaming" / "Anki2"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Anki2"
    else:                                   # Linux / *BSD
        base = Path(os.environ.get("XDG_DATA_HOME",
                                   Path.home() / ".local" / "share")) / "Anki2"
    if not base.exists():
        print("[WARN] 未找到 Anki 2 目录,跳过拷贝(--media-dir 可手动指定)", flush=True)
        return None
    profiles = [p for p in base.iterdir() if (p / "collection.anki2").exists()]
    if len(profiles) == 1:
        media = profiles[0] / "collection.media"
        media.mkdir(exist_ok=True)
        return media
    if len(profiles) > 1:
        names = ", ".join(p.name for p in profiles)
        print(f"[WARN] 发现多个 Anki 配置({names}),请用 --media-dir 指定后拷贝", flush=True)
    return None


def main():
    ap = argparse.ArgumentParser(description="生成单词/例句英音 mp3(edge-tts 免费神经语音)")
    ap.add_argument("--book", required=True)
    ap.add_argument("--chapter", type=int, help="只处理指定章(格式同 cards.py)")
    ap.add_argument("--voice", default=DEFAULT_VOICE,
                    help=f"英音语音(默认 {DEFAULT_VOICE});如 en-GB-RyanNeural")
    ap.add_argument("--rate", default="+0%", help="语速,如 -10% / +20%")
    ap.add_argument("--force", action="store_true", help="忽略已有缓存重新生成")
    ap.add_argument("--no-copy", action="store_true", help="不拷贝到 Anki collection.media")
    ap.add_argument("--media-dir", help="Anki collection.media 路径(多配置时必填)")
    ap.add_argument("--list-voices", action="store_true", help="列出可用英音语音后退出")
    args = ap.parse_args()

    if args.list_voices:
        voices = asyncio.run(edge_tts.list_voices())
        for v in sorted(x["ShortName"] for x in voices if x["ShortName"].startswith("en-GB")):
            print(v)
        return

    # 语音存在性检查(离线时仅警告,不阻断)
    try:
        known = {v["ShortName"] for v in asyncio.run(edge_tts.list_voices())}
        if args.voice not in known:
            alts = sorted(v for v in known if v.startswith("en-GB"))
            print(f"[FAIL] 语音 {args.voice} 不存在。可用英音: {', '.join(alts[:12])}", flush=True)
            sys.exit(1)
    except Exception as exc:
        print(f"[WARN] 语音列表获取失败(忽略,继续生成): {exc}", flush=True)

    audio_dir = audio_dir_for_book(BASE, args.book)
    audio_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.book, args.chapter)
    if not rows:
        print("[FAIL] 没有可用的 raw 行(先跑 pipeline + apply_polish?)", flush=True)
        sys.exit(1)

    # 单词音频跨章去重;例句音频按(章, 词)。
    # 表达卡(pos=phrase)无词级音频,cards.py 同约定:发音只依赖例证句音频。
    word_jobs, seen_words = [], set()
    sent_jobs = []
    for ch, r in rows:
        word = (r.get("word") or "").strip()
        if not word:
            continue
        if r.get("pos") != "phrase" and word not in seen_words:
            seen_words.add(word)
            word_jobs.append((word, audio_dir / word_audio_name(word)))
        sent = (r.get("sent") or "").strip()
        if sent:
            sent_jobs.append((sent, audio_dir / sent_audio_name(ch, word)))

    print(f"[OK] 单词音频 {len(word_jobs)} | 例句音频 {len(sent_jobs)} | 语音 {args.voice} {args.rate}",
          flush=True)
    results = asyncio.run(run_generation(word_jobs + sent_jobs, args.voice, args.rate, args.force))

    counts = {"ok": 0, "skip": 0, "fail": 0}
    fails = []
    for (text, path), res in zip(word_jobs + sent_jobs, results):
        counts[res if res in counts else "fail"] += 1
        if res.startswith("fail"):
            fails.append(f"{path.name} <- {text[:60]!r}: {res[5:]}")
    print(f"[OK] 生成 {counts['ok']} | 跳过缓存 {counts['skip']} | 失败 {counts['fail']}", flush=True)
    for f in fails[:20]:
        print(f"  ✗ {f}", flush=True)
    if len(fails) > 20:
        print(f"  … 其余 {len(fails) - 20} 条失败略", flush=True)

    media = resolve_media_dir(args)
    if media is not None:
        copied = 0
        for _, path in word_jobs + sent_jobs:
            if path.exists():
                shutil.copy2(path, media / path.name)
                copied += 1
        print(f"[OK] 已拷贝 {copied} 个 mp3 -> {media}", flush=True)
    else:
        print("[SKIP] 未拷贝(音频在 data/output/<book>/anki/audio/ 下,可手动处理)", flush=True)

    if counts["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()