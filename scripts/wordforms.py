"""共享词形判定:卡片例句高亮(cards.py)与标注版(annotate.py)共用。
目标词在例句/正文中常以屈折形态出现(slip→slipping、glance→glancing、
anxiety→anxieties、blind→blindest、creep→crept),简单后缀正则覆盖不了:
- 规则屈折由 word_forms 规则生成:直接加后缀、去 e、辅音双写、y 变 i、比较级/最高级
- 不规则动词(crept/clung/forbade...)由 IRREGULAR 内置表补充(稳定封闭集合)
- token_regex 把全部形态合成一个词边界正则,单遍匹配不重叠"""
import re

# 常见不规则动词:lemma(小写) → 该词有意义的全部屈折形态(小写)
IRREGULAR = {
    'arise': {'arose', 'arisen', 'arising', 'arises'},
    'awake': {'awoke', 'awoken', 'awaking', 'awakens'},
    'bear': {'bore', 'borne', 'bearing', 'bears'},
    'beat': {'beat', 'beaten', 'beating'},
    'become': {'became', 'becoming', 'becomes'},
    'begin': {'began', 'begun', 'beginning', 'begins'},
    'bend': {'bent', 'bending', 'bends'},
    'bind': {'bound', 'binding', 'binds'},
    'bite': {'bit', 'bitten', 'biting', 'bites'},
    'blow': {'blew', 'blown', 'blowing', 'blows'},
    'break': {'broke', 'broken', 'breaking', 'breaks'},
    'bring': {'brought', 'bringing', 'brings'},
    'build': {'built', 'building', 'builds'},
    'burst': {'burst', 'bursting', 'bursts'},
    'buy': {'bought', 'buying', 'buys'},
    'catch': {'caught', 'catching', 'catches'},
    'choose': {'chose', 'chosen', 'choosing', 'chooses'},
    'cling': {'clung', 'clinging', 'clings'},
    'come': {'came', 'coming', 'comes'},
    'cost': {'cost', 'costing', 'costs'},
    'creep': {'crept', 'creeping', 'creeps'},
    'cut': {'cut', 'cutting', 'cuts'},
    'dig': {'dug', 'digging', 'digs'},
    'draw': {'drew', 'drawn', 'drawing', 'draws'},
    'dream': {'dreamt', 'dreamed', 'dreaming', 'dreams'},
    'drink': {'drank', 'drunk', 'drinking', 'drinks'},
    'drive': {'drove', 'driven', 'driving', 'drives'},
    'eat': {'ate', 'eaten', 'eating', 'eats'},
    'fall': {'fell', 'fallen', 'falling', 'falls'},
    'feed': {'fed', 'feeding', 'feeds'},
    'feel': {'felt', 'feeling', 'feels'},
    'fight': {'fought', 'fighting', 'fights'},
    'find': {'found', 'finding', 'finds'},
    'flee': {'fled', 'fleeing', 'flees'},
    'fling': {'flung', 'flinging', 'flings'},
    'fly': {'flew', 'flown', 'flying', 'flies'},
    'forbid': {'forbade', 'forbad', 'forbidden', 'forbidding', 'forbids'},
    'forget': {'forgot', 'forgotten', 'forgetting', 'forgets'},
    'forgive': {'forgave', 'forgiven', 'forgiving', 'forgives'},
    'freeze': {'froze', 'frozen', 'freezing', 'freezes'},
    'get': {'got', 'gotten', 'getting', 'gets'},
    'give': {'gave', 'given', 'giving', 'gives'},
    'go': {'went', 'gone', 'going', 'goes'},
    'grow': {'grew', 'grown', 'growing', 'grows'},
    'hang': {'hung', 'hanging', 'hangs'},
    'hear': {'heard', 'hearing', 'hears'},
    'hide': {'hid', 'hidden', 'hiding', 'hides'},
    'hit': {'hit', 'hitting', 'hits'},
    'hold': {'held', 'holding', 'holds'},
    'hurt': {'hurt', 'hurting', 'hurts'},
    'keep': {'kept', 'keeping', 'keeps'},
    'kneel': {'knelt', 'kneeling', 'kneels'},
    'know': {'knew', 'known', 'knowing', 'knows'},
    'lay': {'laid', 'laying', 'lays'},
    'lead': {'led', 'leading', 'leads'},
    'lean': {'leant', 'leaned', 'leaning', 'leans'},
    'leave': {'left', 'leaving', 'leaves'},
    'lend': {'lent', 'lending', 'lends'},
    'lie': {'lay', 'lain', 'lying', 'lies'},
    'light': {'lit', 'lighted', 'lighting', 'lights'},
    'lose': {'lost', 'losing', 'loses'},
    'make': {'made', 'making', 'makes'},
    'mean': {'meant', 'meaning', 'means'},
    'meet': {'met', 'meeting', 'meets'},
    'pay': {'paid', 'paying', 'pays'},
    'put': {'put', 'putting', 'puts'},
    'read': {'read', 'reading'},
    'ride': {'rode', 'ridden', 'riding', 'rides'},
    'ring': {'rang', 'rung', 'ringing', 'rings'},
    'rise': {'rose', 'risen', 'rising', 'rises'},
    'run': {'ran', 'running', 'runs'},
    'say': {'said', 'saying', 'says'},
    'see': {'saw', 'seen', 'seeing', 'sees'},
    'seek': {'sought', 'seeking', 'seeks'},
    'sell': {'sold', 'selling', 'sells'},
    'send': {'sent', 'sending', 'sends'},
    'set': {'set', 'setting', 'sets'},
    'shake': {'shook', 'shaken', 'shaking', 'shakes'},
    'shine': {'shone', 'shining', 'shines'},
    'shoot': {'shot', 'shooting', 'shoots'},
    'show': {'showed', 'shown', 'showing', 'shows'},
    'shut': {'shut', 'shutting', 'shuts'},
    'sing': {'sang', 'sung', 'singing', 'sings'},
    'sink': {'sank', 'sunk', 'sinking', 'sinks'},
    'sit': {'sat', 'sitting', 'sits'},
    'sleep': {'slept', 'sleeping', 'sleeps'},
    'slide': {'slid', 'sliding', 'slides'},
    'speak': {'spoke', 'spoken', 'speaking', 'speaks'},
    'spend': {'spent', 'spending', 'spends'},
    'spin': {'spun', 'spinning', 'spins'},
    'spread': {'spread', 'spreading', 'spreads'},
    'stand': {'stood', 'standing', 'stands'},
    'steal': {'stole', 'stolen', 'stealing', 'steals'},
    'stick': {'stuck', 'sticking', 'sticks'},
    'sting': {'stung', 'stinging', 'stings'},
    'strike': {'struck', 'stricken', 'striking', 'strikes'},
    'swear': {'swore', 'sworn', 'swearing', 'swears'},
    'sweep': {'swept', 'sweeping', 'sweeps'},
    'swim': {'swam', 'swum', 'swimming', 'swims'},
    'swing': {'swung', 'swinging', 'swings'},
    'take': {'took', 'taken', 'taking', 'takes'},
    'teach': {'taught', 'teaching', 'teaches'},
    'tear': {'tore', 'torn', 'tearing', 'tears'},
    'tell': {'told', 'telling', 'tells'},
    'think': {'thought', 'thinking', 'thinks'},
    'throw': {'threw', 'thrown', 'throwing', 'throws'},
    'understand': {'understood', 'understanding', 'understands'},
    'wake': {'woke', 'woken', 'waking', 'wakes'},
    'wear': {'wore', 'worn', 'wearing', 'wears'},
    'weep': {'wept', 'weeping', 'weeps'},
    'win': {'won', 'winning', 'wins'},
    'wind': {'wound', 'winding', 'winds'},
    'write': {'wrote', 'written', 'writing', 'writes'},
    'shear': {'sheared', 'shorn', 'shearing', 'shears'},
}


def word_forms(word):
    """目标词可能出现的全部屈折形态(小写 frozenset):原形 + 规则屈折 + 不规则表。"""
    w = word.strip().lower()
    forms = {w}
    # 直接加后缀
    for suf in ('s', 'es', 'ed', 'ing', 'd', 'er', 'est'):
        forms.add(w + suf)
    # 去 e:glance→glancing / skate→skating
    if len(w) > 2 and w.endswith('e'):
        stem = w[:-1]
        for suf in ('s', 'es', 'ed', 'ing', 'd', 'er', 'est'):
            forms.add(stem + suf)
    # 辅音双写:slip→slipping / trot→trotted / confer→conferred
    if len(w) >= 3 and w[-1] not in 'aeiouy' and w[-2] in 'aeiou':
        dub = w + w[-1]
        for suf in ('s', 'es', 'ed', 'ing', 'er', 'est'):
            forms.add(dub + suf)
    # y 变 i:anxiety→anxieties / embody→embodied / shabby→shabbier
    if len(w) > 1 and w.endswith('y') and w[-2] not in 'aeiou':
        stem = w[:-1]
        for suf in ('ies', 'ied', 'ier', 'iest'):
            forms.add(stem + suf)
    forms.update(IRREGULAR.get(w, ()))
    return frozenset(forms)


def token_regex(words):
    """把多个目标词的全部形态合成一个词边界正则,单遍匹配、天然不重叠。"""
    forms = set()
    for w in words:
        forms |= word_forms(w)
    alts = sorted(forms, key=len, reverse=True)
    return re.compile(r'\b(?:' + '|'.join(re.escape(f) for f in alts) + r')\b', re.I)