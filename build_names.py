#!/usr/bin/env python3
"""Build the T-UI offline place-name files from USGS GNIS (USA) and GeoNames (Europe + world cities).

    python build_names.py            # reads raw/, writes out/names/...

Output
------
out/names/<lat>/<lon>.tnm   one file per 1x1 degree square (lat/lon = floor of the corner, e.g.
                            names/47/-122.tnm covers 47..48N, 122..121W). Folders by latitude so
                            no card directory holds more than a few hundred files.
out/names/cities.tnm        world cities of 15,000+ people - searched from anywhere.
out/names/index.json        which squares exist, with sizes - for the website and for tooling.

TNM1 format (all little-endian) - the firmware reader in PlaceNames.cpp must match this exactly
--------------------------------------------------------------------------------------------------
header, 32 bytes:
    char     magic[4]      "TNM1"
    uint32   recordCount
    uint32   indexCount
    uint32   recordsOffset (always 32)
    uint32   indexOffset
    int32    cellLat, cellLon   (-999 for cities.tnm)
    uint32   flags         bit 0 = RANKED (cities.tnm): every record has the rank byte below, and
                           records are stored biggest population first
records, variable length, one per place:
    int32    lat * 1e6
    int32    lon * 1e6
    uint8    kind          (KINDS below)
    uint8    rank          ONLY when RANKED: population on a log scale, round(25 * log10(pop))
    uint8    nameLen       display name, UTF-8, at most 63 bytes
    char     name[nameLen]
    uint8    keyLen        the search form: lowercase ASCII words joined by single spaces; for big
    char     key[keyLen]   places then '|' and each alternate name in the same form ("munich|munchen")
index, 16-byte entries sorted by (word, recordOffset):
    char     word[12]      one search word, lowercase ASCII, zero-padded (first 12 chars)
    uint32   recordOffset  absolute file offset of the record
A record appears once in the index for each distinct word of its name (stopwords excluded), so
typing any word of a name finds it: "serene" -> "Lake Serene".

Licences: GNIS is public domain (USGS). GeoNames is CC BY 4.0 - the device and the website credit it.
"""
import collections, io, json, math, os, struct, sys, unicodedata, zipfile

RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'raw')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out', 'names')

# Kinds - index = the byte stored. MUST match kKindNames[] in PlaceNames.cpp.
KINDS = ['place', 'town', 'lake', 'reservoir', 'pond', 'river', 'spring', 'peak', 'ridge', 'valley',
         'pass', 'flat', 'island', 'bay', 'cape', 'swamp', 'falls', 'glacier', 'park', 'forest', 'beach',
         'canal', 'area', 'camp', 'airport', 'station', 'landmark', 'trail', 'basin', 'cliff', 'rapids',
         'crossing', 'military', 'range', 'city']
K = {n: i for i, n in enumerate(KINDS)}

GNIS_KIND = {
    'Populated Place': 'town', 'Lake': 'lake', 'Reservoir': 'reservoir', 'Stream': 'river',
    'Spring': 'spring', 'Summit': 'peak', 'Ridge': 'ridge', 'Valley': 'valley', 'Gap': 'pass',
    'Flat': 'flat', 'Plain': 'flat', 'Bench': 'flat', 'Island': 'island', 'Bar': 'island',
    'Bay': 'bay', 'Sea': 'bay', 'Cape': 'cape', 'Isthmus': 'cape', 'Swamp': 'swamp', 'Falls': 'falls',
    'Glacier': 'glacier', 'Park': 'park', 'Woods': 'forest', 'Beach': 'beach', 'Canal': 'canal',
    'Channel': 'river', 'Gut': 'river', 'Arroyo': 'river', 'Bend': 'river', 'Rapids': 'rapids',
    'Civil': 'area', 'Area': 'area', 'Basin': 'basin', 'Crater': 'basin', 'Cliff': 'cliff',
    'Slope': 'ridge', 'Pillar': 'landmark', 'Arch': 'landmark', 'Lava': 'flat', 'Levee': 'landmark',
    'Crossing': 'crossing', 'Military': 'military', 'Range': 'range',
    # 'Census' (census-designated statistical areas) duplicates Populated Place - left out.
}

# GeoNames feature codes -> kind. Anything not listed is left out (buildings, admin units, etc).
GN_KIND = {}
for codes, kind in [
    ('PPL PPLA PPLA2 PPLA3 PPLA4 PPLA5 PPLC PPLF PPLG PPLL PPLR PPLS PPLX STLMT', 'town'),
    ('LK LKS LKI LKN LKO LKC LKX LGN LGNS', 'lake'), ('PND PNDS PNDI PNDN PNDSN', 'pond'),
    ('RSV RSVT RSVI', 'reservoir'), ('STM STMS STMI STMB STMC STMD STMH STMM STMSB STMX RVN WAD CHN CHNM', 'river'),
    ('SPNG SPNS SPNT', 'spring'), ('MT MTS PK PKS HLL HLLS VLC MND MNDS BUTE MESA DUNE DUNS', 'peak'),
    ('RDGE SPUR CRQ CRQS', 'ridge'), ('VAL VALS VALX GRGE CNYN', 'valley'), ('PASS GAP', 'pass'),
    ('PLN PLAT FLTT UPLD HTH MOOR', 'flat'), ('ISL ISLS ISLT ISLET ISLX ATOL', 'island'),
    ('BAY BAYS COVE FJD FJDS SD INLT GULF', 'bay'), ('CAPE PT PTS PEN PENX HDLD', 'cape'),
    ('SWMP MRSH BOG FEN MRSHN', 'swamp'), ('FLLS FLLSX CSCD', 'falls'), ('GLCR ICECAP', 'glacier'),
    ('PRK RES RESN RESW RESF RESH RESV RESP', 'park'), ('FRST FRSTF GRVO GRVE', 'forest'),
    ('BCH BCHS', 'beach'), ('CNL CNLA CNLD CNLI CNLN CNLQ CNLSB CNLX', 'canal'),
    ('CMP CMPL CMPQ HUT HUTS', 'camp'), ('AIRP AIRF AIRH', 'airport'), ('RSTN RSTNQ MTRO', 'station'),
    ('CSTL MNMT RUIN LTHSE TOWR PAL', 'landmark'), ('TRL', 'trail'), ('BSN BSNU CRTR', 'basin'),
    ('CLF CLFS', 'cliff'), ('RPDS', 'rapids'),
]:
    for c in codes.split():
        GN_KIND[c] = kind

EUROPE = ('AD AL AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE IM IS IT JE LI '
          'LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SJ SK SM TR UA VA XK').split()
# Russia and Turkey only as far as Europe goes.
EU_BOX = (34.0, 81.5, -32.0, 60.0)  # lat min/max, lon min/max

STOP = set('the of de la le les du des di del della der die das am im an en y et and von van da do dos '
           'al el lo los las st'.split())

FOLD = str.maketrans({'ß': 'ss', 'æ': 'ae', 'Æ': 'ae', 'ø': 'o', 'Ø': 'o', 'œ': 'oe', 'Œ': 'oe', 'ł': 'l',
                      'Ł': 'l', 'đ': 'd', 'Đ': 'd', 'ð': 'd', 'Ð': 'd', 'þ': 'th', 'Þ': 'th', 'ı': 'i',
                      "'": '', '’': ''})


def search_form(name):
    """Lowercase ASCII words joined by spaces. The firmware folds what you TYPE the same way."""
    s = unicodedata.normalize('NFKD', name.translate(FOLD))
    s = ''.join(ch for ch in s if not unicodedata.combining(ch)).lower()
    out, word = [], []
    for ch in s:
        if 'a' <= ch <= 'z' or '0' <= ch <= '9':
            word.append(ch)
        elif word:
            out.append(''.join(word)); word = []
    if word:
        out.append(''.join(word))
    return ' '.join(out)


def index_words(key):
    ws = []
    for w in key.split():
        if len(w) >= 2 and w not in STOP and w not in ws:
            ws.append(w)
    if not ws:  # a name made only of stopwords/short words is still findable
        ws = [w for w in key.split() if w][:2]
    return ws


def utf8_trim(s, n):
    b = s.encode('utf-8')
    if len(b) <= n:
        return b
    b = b[:n]
    while b and (b[-1] & 0xC0) == 0x80:
        b = b[:-1]
    if b and b[-1] >= 0xC0:
        b = b[:-1]
    return b


def pop_rank(pop):
    """Population on a log scale in one byte: 25 per tenfold. 15,000 -> 104, 1M -> 150, 10M -> 175."""
    return max(0, min(255, int(round(25 * math.log10(max(pop, 1))))))


def write_tnm(path, cell_lat, cell_lon, places, ranked=False):
    """places: list of (lat, lon, kind, name, key, extra_words[, population]).

    ranked (header flag 1, cities.tnm): each record carries a population-rank byte after its kind,
    and records are stored BIGGEST FIRST - so the device can suggest "San Francisco, San Diego..."
    for "san" by reading the top of the file, and within any one index word the entries also
    come out biggest first."""
    if ranked:
        places.sort(key=lambda p: (-p[6], p[4]))
    else:
        places.sort(key=lambda p: (p[4], p[0], p[1]))
    recs = bytearray()
    offsets = []
    for p in places:
        lat, lon, kind, name, key, extra = p[:6]
        offsets.append(32 + len(recs))
        nb = utf8_trim(name, 63)
        kb = '|'.join([key] + list(extra)).encode('ascii')[:255]
        recs += struct.pack('<iiB', int(round(lat * 1e6)), int(round(lon * 1e6)), kind)
        if ranked:
            recs += struct.pack('<B', pop_rank(p[6]))
        recs += struct.pack('<B', len(nb)) + nb
        recs += struct.pack('<B', len(kb)) + kb
    idx = []
    for p, off in zip(places, offsets):
        lat, lon, kind, name, key, extra = p[:6]
        ws = []
        for seg in [key] + list(extra):
            for w in index_words(seg):
                if w not in ws:
                    ws.append(w)
        for w in ws:
            idx.append((w.encode('ascii')[:12].ljust(12, b'\0'), off))
    idx.sort()
    index_off = 32 + len(recs)
    with open(path, 'wb') as f:
        f.write(b'TNM1' + struct.pack('<IIIIiiI', len(places), len(idx), 32, index_off, cell_lat, cell_lon,
                                      1 if ranked else 0))
        f.write(recs)
        for w, off in idx:
            f.write(w + struct.pack('<I', off))
    return len(places), len(idx), 32 + len(recs) + 16 * len(idx)


def read_gnis(cells):
    z = zipfile.ZipFile(os.path.join(RAW, 'DomesticNames_National_Text.zip'))
    f = io.TextIOWrapper(z.open('Text/DomesticNames_National.txt'), encoding='utf-8-sig', errors='replace')
    hdr = f.readline().rstrip('\r\n').split('|')
    col = {h: i for i, h in enumerate(hdr)}
    n = kept = 0
    for line in f:
        p = line.rstrip('\r\n').split('|')
        n += 1
        kind = GNIS_KIND.get(p[col['feature_class']])
        if not kind:
            continue
        try:
            lat = float(p[col['prim_lat_dec']]); lon = float(p[col['prim_long_dec']])
        except ValueError:
            continue
        if lat == 0 and lon == 0:
            continue
        name = p[col['feature_name']].strip()
        key = search_form(name)
        if not key:
            continue
        cells[(math.floor(lat), math.floor(lon))].append((lat, lon, K[kind], name, key, []))
        kept += 1
    print('GNIS: %d read, %d kept' % (n, kept))


LOCAL = {}  # geonameid -> the place's names in its own country's languages, best first


def load_local_names():
    """GeoNames files a famous place under its ENGLISH name - Vienna, Prague, Florence, Warsaw - and
    keeps the local one ("Wien", "Praha", "Firenze", "Warszawa") only among hundreds of untagged
    alternates. Tested 2026-09-30: standing in Vienna, "wien" found a river; "firenze" never found
    Florence at all. The per-country alternatenames files tag each name with its language, and
    countryInfo.txt says which languages each country speaks - so every place gets the names its
    own people use. Preferred names first, then short ones; colloquial and historic ones are left
    out ("Leningrad" is not where St Petersburg is now)."""
    langs = {}
    for line in open(os.path.join(RAW, 'countryInfo.txt'), encoding='utf-8'):
        if line.startswith('#'):
            continue
        p = line.rstrip('\n').split('\t')
        if len(p) > 15:
            langs[p[0]] = {l.split('-')[0] for l in p[15].split(',') if l}
    for c in EUROPE:
        want = langs.get(c, set())
        z = zipfile.ZipFile(os.path.join(RAW, 'alt', c + '.zip'))
        f = io.TextIOWrapper(z.open(c + '.txt'), encoding='utf-8', errors='replace')
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) < 8 or p[2] not in want or p[6] == '1' or p[7] == '1':
                continue
            LOCAL.setdefault(int(p[1]), []).append((p[4] != '1', p[5] != '1', p[3]))
    for k, v in LOCAL.items():
        v.sort()
        LOCAL[k] = [n for _, _, n in v]
    print('local-language names for %d European places' % len(LOCAL))


def read_geonames_zip(zname, member, cells, box=None, kinds=GN_KIND, force_kind=None, extra_min_pop=None, collect=None):
    z = zipfile.ZipFile(os.path.join(RAW, zname))
    f = io.TextIOWrapper(z.open(member), encoding='utf-8', errors='replace')
    kept = 0
    for line in f:
        p = line.rstrip('\n').split('\t')
        if len(p) < 15:
            continue
        code = p[7]
        kind = force_kind or kinds.get(code)
        if not kind:
            continue
        lat, lon = float(p[4]), float(p[5])
        if box and not (box[0] <= lat <= box[1] and box[2] <= lon <= box[3]):
            continue
        name = p[1].strip()
        key = search_form(name) or search_form(p[2])
        if not key:
            continue
        extra = []
        for ln in LOCAL.get(int(p[0]), ()):  # its own-language names come first, up to three
            f2 = search_form(ln)
            if f2 and f2 != key and f2 not in extra and len(extra) < 3 and                     len(key) + sum(len(e) + 1 for e in extra) + len(f2) + 1 <= 255:
                extra.append(f2)
        pop = int(p[14] or 0)
        if extra_min_pop is not None and pop >= extra_min_pop:
            # Big places are searched by more than one name - and GeoNames files the big cities under
            # their ENGLISH name ("Munich"), with the local one ("Munchen") only among the untagged
            # alternates. Each Latin-script alternate is folded to its search form and kept WHOLE, as
            # its own '|' segment of the key: the match check needs one name that contains every
            # typed word, and "is this exactly what was typed" needs whole names to compare. Other
            # scripts fold to nothing and drop out. Caught by testing: searching "munchen" near
            # Garmisch found stations called Munchen-something, and not the city.
            # ⭐ CHOSEN BY AGREEMENT, not by list order. The list is unordered and full of oddities
            # ("lungsod ng muenchen", "muc", "minca") that used up the slots before "munchen" was
            # reached. München, Munchen and Múnchen all fold to "munchen", so the forms that the most
            # languages agree on come first - which is what people actually type.
            counts = collections.Counter()
            for alt in p[3].split(','):
                a = alt.strip()
                if not a or sum(1 for c in a if ord(c) < 0x250) < len(a):
                    continue  # not Latin script
                if len(a) <= 4 and a.isupper():
                    continue  # an airport or station code ("MUC"), not a name anyone types
                f2 = search_form(a)
                if f2 and f2 != key and f2 not in extra:
                    counts[f2] += 1
            room = 255 - len(key) - sum(len(e) + 1 for e in extra)
            # World cities travel to every device as ONE file, so there the spellings are trimmed: at
            # least two languages must agree on a form, and a town of 20,000 gets fewer than a
            # capital. Took cities.tnm from 5.7MB to under half that - it downloads over the
            # T-Deck's own wi-fi, so size is minutes.
            # Small towns rarely have an English name of their own, so they keep only spellings two
            # languages agree on. Big cities keep their top few whatever the count: GeoNames lists
            # each spelling once, so "Wien", "Praha" and "Lisboa" each have a count of ONE, and a
            # strict agreement rule threw exactly those away (tested: "wien" stopped finding Vienna).
            cap, need = 10, 1
            if force_kind == 'city':
                cap, need = (8, 1) if pop >= 1000000 else (4, 1) if pop >= 100000 else (2, 2)
            cap += len(extra)
            for f2, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0]))):
                if len(extra) >= cap:
                    break
                if cnt < need:
                    continue
                if len(f2) + 1 <= room:
                    extra.append(f2); room -= len(f2) + 1
        rec = (lat, lon, K[kind], name, key, extra, pop)
        if collect is not None:
            collect.append(rec)
        else:
            cells[(math.floor(lat), math.floor(lon))].append(rec)
        kept += 1
    return kept


def cells_items(index, region):
    for k, v in index['cells'].items():
        if v['region'] == region:
            la, lo = k.split('/')
            yield (int(la), int(lo)), v


def main():
    load_local_names()
    cells = collections.defaultdict(list)
    read_gnis(cells)
    us_cells = set(cells)
    total = 0
    for c in EUROPE:
        n = read_geonames_zip(c + '.zip', c + '.txt', cells, box=EU_BOX, extra_min_pop=50000)
        total += n
    print('GeoNames Europe: %d kept' % total)
    cities = []
    read_geonames_zip('cities15000.zip', 'cities15000.txt', None, force_kind='city', extra_min_pop=0,
                      collect=cities)
    print('world cities: %d' % len(cities))

    if os.path.isdir(OUT):
        import shutil
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    index = {'format': 'TNM1', 'cells': {}, 'sources': ['USGS GNIS (public domain)', 'GeoNames (CC BY 4.0)']}
    sizes = []
    for (la, lo), places in sorted(cells.items()):
        d = os.path.join(OUT, str(la))
        os.makedirs(d, exist_ok=True)
        n, ni, sz = write_tnm(os.path.join(d, '%d.tnm' % lo), la, lo, places)
        index['cells']['%d/%d' % (la, lo)] = {'n': n, 'bytes': sz, 'region': 'us' if (la, lo) in us_cells else 'eu'}
        sizes.append(sz)
    n, ni, sz = write_tnm(os.path.join(OUT, 'cities.tnm'), -999, -999, cities, ranked=True)
    index['cities'] = {'n': n, 'bytes': sz}
    with open(os.path.join(OUT, 'index.json'), 'w') as f:
        json.dump(index, f, separators=(',', ':'))
    # One small list per region - "lat lon" per line - for the T-Deck's "All of the USA / Europe"
    # download. index.json is 150KB of JSON; the device only needs to know which squares exist.
    for r in ('us', 'eu'):
        cells = sorted((la, lo) for (la, lo), _ in cells_items(index, r))
        with open(os.path.join(OUT, '%s.lst' % r), 'w', newline='\n') as f:
            f.write(''.join('%d %d\n' % c for c in cells))
    sizes.sort()
    print('cells: %d  total %.1f MB  median %d KB  largest %d KB  cities.tnm %d KB' % (
        len(sizes), sum(sizes) / 1e6, sizes[len(sizes) // 2] // 1024, sizes[-1] // 1024, sz // 1024))


if __name__ == '__main__':
    main()
