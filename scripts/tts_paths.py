"""TTS 音频路径/文件名约定(gen_audio 与 cards 共用)。

不放 edge_tts 依赖:cards.py 离线运行时不需导入该库。
文件名规范(Anki media 安全:无空格、大小写不敏感系统不冲突):
  - 单词音频跨章复用:w_<sanitize>_<hash8>.mp3(hash 防同形词冲突)
  - 例句音频按(章, 词)区分:s_ch<NN>_<sanitize>_<hash8>.mp3
"""
import hashlib
import re
from pathlib import Path

# 默认英音神经语音(edge-tts 免费,与微软 Edge "大声朗读"同款)
DEFAULT_VOICE = "en-GB-SoniaNeural"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def audio_dir_for_book(base, book: str) -> Path:
    """data/output/<book>/anki/audio/(base 兼容 Path 与 str)"""
    return Path(base) / "data" / "output" / book / "anki" / "audio"


def sanitize(text: str) -> str:
    return _UNSAFE.sub("_", str(text or ""))


def _tag(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def word_audio_name(word: str) -> str:
    return f"w_{sanitize(word)}_{_tag(word)}.mp3"


def sent_audio_name(chapter: int, word: str) -> str:
    key = f"ch{chapter:02d}:{word}"
    return f"s_ch{chapter:02d}_{sanitize(word)}_{_tag(key)}.mp3"