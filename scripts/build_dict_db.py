"""一次性构建本地词典 sqlite: stardict-ecdict → resources/ecdict.db
条目格式(ECDICT stardict):
    *[eɪ]                  ← 音标
    na. 一                  ← 释义行
    ...
    [时态] abandoned...     ← 跳过
    (高研四六托宝 2182/2057) ← 考试标签 + bnc/frq 词频
纯标准库,无第三方依赖。
"""
import os, struct, sqlite3, sys, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(BASE, 'resources', 'stardict-ecdict-2.4.2')
OUT = os.path.join(BASE, 'resources', 'ecdict.db')
IDX = os.path.join(D, 'stardict-ecdict-2.4.2.idx')
DICT = os.path.join(D, 'stardict-ecdict-2.4.2.dict')

if os.path.exists(OUT):
    os.remove(OUT)

print('reading idx/dict ...', flush=True)
# ECDICT .dict 为数百 MB 二进制,此处全量读入内存一次性建库(一次性脚本,
# 建库时需 1GB+ 空闲内存;完成后 ecdict.db 为压缩 sqlite,运行期按需查询不吃内存)
data = open(DICT, 'rb').read()
idx = open(IDX, 'rb').read()

con = sqlite3.connect(OUT)
con.execute('PRAGMA journal_mode=OFF')
con.execute('PRAGMA synchronous=OFF')
con.execute('CREATE TABLE dict (word TEXT PRIMARY KEY, phonetic TEXT, translation TEXT, tags TEXT, bnc INTEGER, frq INTEGER)')

re_ph = re.compile(r'^\*\[([^\]]+)\]')
re_nums = re.compile(r'(\d+)/(\d+)')

pos, n = 0, 0
batch = []
while True:
    end = idx.find(b'\x00', pos)
    if end < 0:
        break
    word = idx[pos:end].decode('utf-8', errors='replace').lower()
    off = struct.unpack('>I', idx[end + 1:end + 5])[0]
    size = struct.unpack('>I', idx[end + 5:end + 9])[0]
    raw = data[off:off + size].decode('utf-8', errors='replace')
    lines = raw.split('\n')

    phonetic = ''
    m = re_ph.match(lines[0].strip()) if lines else None
    if m:
        phonetic = m.group(1)

    trans_lines, tags, bnc, frq = [], '', 0, 0
    for ln in lines[1:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('['):          # [时态]/[网络] 等元信息行
            continue
        if s.startswith('('):
            nums = re_nums.findall(s)
            if nums:
                bnc, frq = int(nums[0][0]), int(nums[0][1])
                tags = s[1:].split()[0] if ' ' in s else s[1:]
            continue
        trans_lines.append(s)
        if len(trans_lines) >= 6:
            break

    batch.append((word, phonetic, '\n'.join(trans_lines), tags, bnc, frq))
    n += 1
    if len(batch) >= 20000:
        con.executemany('INSERT OR REPLACE INTO dict VALUES (?,?,?,?,?,?)', batch)
        batch.clear()
        if n % 300000 == 0:
            print(f'{n} entries ...', flush=True)
    pos = end + 9

if batch:
    con.executemany('INSERT OR REPLACE INTO dict VALUES (?,?,?,?,?,?)', batch)
con.commit()
con.execute('CREATE INDEX idx_word ON dict(word)')
con.close()
print(f'DONE: {n} entries -> {OUT}', flush=True)