#!/usr/bin/env python3
"""Search the TNM1 place-name files the way the T-Deck will - a PC reference for the firmware.

    python search_names.py <lat> <lon> <query words...>

Two groups, like the device shows them:
  Nearby  - the 1-degree square under (lat, lon) and the rings around it, nearest first
            (from 3 typed letters)
  Cities  - suggestions from cities.tnm: the biggest matching cities, bigger and closer first
            (from 2 typed letters) - "san" -> San Jose, San Francisco, San Diego...
The firmware reader (PlaceNames.cpp) follows the same steps, so this is also how its results are
checked.
"""
import math, os, struct, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out', 'names')
KINDS = ['place', 'town', 'lake', 'reservoir', 'pond', 'river', 'spring', 'peak', 'ridge', 'valley',
         'pass', 'flat', 'island', 'bay', 'cape', 'swamp', 'falls', 'glacier', 'park', 'forest', 'beach',
         'canal', 'area', 'camp', 'airport', 'station', 'landmark', 'trail', 'basin', 'cliff', 'rapids',
         'crossing', 'military', 'range', 'city']
STOP = set('the of de la le les du des di del della der die das am im an en y et and von van da do dos '
           'al el lo los las st'.split())
TOP_CITIES = 4000     # cities.tnm is biggest-first: these are the ones scanned whatever they are called
RANK_BIG = 125        # 100,000 people: a city this big may go above the nearby results
MAX_NEAR, MAX_CITIES = 9, 4


def words(q):
    ws, cur = [], ''
    for ch in q.lower().replace("'", ''):
        if 'a' <= ch <= 'z' or '0' <= ch <= '9':
            cur += ch
        elif cur:
            ws.append(cur); cur = ''
    if cur:
        ws.append(cur)
    return ws


class Tnm:
    def __init__(self, path):
        self.f = open(path, 'rb')
        h = self.f.read(32)
        assert h[:4] == b'TNM1'
        self.nrec, self.nidx, self.recoff, self.idxoff, self.clat, self.clon, flags = struct.unpack('<IIIIiiI', h[4:])
        self.ranked = bool(flags & 1)
        self.reads = 0

    def idx(self, i):
        self.f.seek(self.idxoff + 16 * i)
        self.reads += 1
        e = self.f.read(16)
        return e[:12], struct.unpack('<I', e[12:])[0]

    def lower(self, pfx):  # first index entry whose word >= pfx
        lo, hi = 0, self.nidx
        while lo < hi:
            mid = (lo + hi) // 2
            if self.idx(mid)[0][:len(pfx)] < pfx:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def range(self, w):
        p = w.encode()[:12]
        return self.lower(p), self.lower(p[:-1] + bytes([p[-1] + 1]))

    def _parse(self, buf, pos):
        lat, lon, kind = struct.unpack_from('<iiB', buf, pos)
        pos += 9
        rank = 0
        if self.ranked:
            rank = buf[pos]; pos += 1
        nl = buf[pos]; pos += 1
        name = buf[pos:pos + nl].decode('utf-8', 'replace'); pos += nl
        kl = buf[pos]; pos += 1
        key = buf[pos:pos + kl].decode('ascii'); pos += kl
        return (lat / 1e6, lon / 1e6, kind, name, key, rank), pos

    def rec(self, off):
        self.f.seek(off)
        self.reads += 1
        return self._parse(self.f.read(10 + 1 + 255 + 1 + 255), 0)[0]

    def scan(self, n):
        """The first n records in file order, with their offsets."""
        self.f.seek(self.recoff)
        buf = self.f.read(min(self.idxoff - self.recoff, n * 400))
        self.reads += 1 + len(buf) // 4096
        pos, out = 0, []
        while len(out) < n and pos < len(buf):
            r, nxt = self._parse(buf, pos)
            out.append((self.recoff + pos, r))
            pos = nxt
        return out


def matches(key, qws):
    """The place's own name must contain every typed word, each as the start of a word, in any
    order ("washington lake" finds Lake Washington). Its OTHER names ('|' segments after the
    first) count only from 4 typed letters, and only read from their first word in order:
    GeoNames alternates are full of oddities - "sari" for Surrey, "fi sang" for Philadelphia -
    that otherwise turn up as suggestions for "sa" and "san f"."""
    segs = key.split('|')
    kws = segs[0].split()
    if all(any(k.startswith(q) for k in kws) for q in qws):
        return True
    if len(''.join(qws)) < 4:
        return False
    for seg in segs[1:]:
        kws = seg.split()
        if len(qws) <= len(kws) and all(kws[i].startswith(q) for i, q in enumerate(qws)):
            return True
    return False


def walk(t, qws, cap=250):
    probe = [w for w in qws if w not in STOP] or qws
    best = None
    for w in probe:  # the most selective word decides which slice of the index to walk
        a, b = t.range(w)
        if best is None or b - a < best[1] - best[0]:
            best = (a, b)
    out, seen = [], set()
    a, b = best
    for i in range(a, min(b, a + cap)):
        off = t.idx(i)[1]
        if off in seen:
            continue
        seen.add(off)
        r = t.rec(off)
        if matches(r[4], qws):
            out.append((off, r))
    return out


def dist_km(la1, lo1, la2, lo2):
    x = math.radians(lo2 - lo1) * math.cos(math.radians((la1 + la2) / 2))
    y = math.radians(la2 - la1)
    return 6371 * math.hypot(x, y)


def search(lat, lon, q, rings=2):
    qws = words(q)
    full = ' '.join(qws)
    letters = len(''.join(qws))
    reads = files = 0

    near = {}
    if letters >= 3:
        c0, c1 = math.floor(lat), math.floor(lon)
        raw = 0
        for r in range(rings + 1):
            for dla in range(-r, r + 1):
                for dlo in range(-r, r + 1):
                    if max(abs(dla), abs(dlo)) != r:
                        continue
                    p = os.path.join(ROOT, str(c0 + dla), '%d.tnm' % (c1 + dlo))
                    if not os.path.exists(p):
                        continue
                    t = Tnm(p)
                    for _, h in walk(t, qws):
                        raw += 1
                        near[(h[3], round(h[0], 3), round(h[1], 3))] = h
                    reads += t.reads; files += 1
            if raw >= 12:
                break

    def near_score(h):
        # Nearest first - but a name that IS what you typed beats a longer name that merely
        # contains it, and a town or city beats a creek named after it.
        d = dist_km(lat, lon, h[0], h[1]) + 0.5
        if full in h[4].split('|'):
            d /= 8
        if KINDS[h[2]] in ('town', 'city'):
            d /= 3
        return d
    near = sorted(near.values(), key=near_score)[:MAX_NEAR]

    cities = {}
    if letters >= 2:
        t = Tnm(os.path.join(ROOT, 'cities.tnm'))
        for off, h in t.scan(TOP_CITIES):       # the biggest, whatever they are called
            if matches(h[4], qws):
                cities[off] = h
        for off, h in walk(t, qws):             # and smaller ones, through the index
            cities[off] = h
        reads += t.reads; files += 1
    # Keep the best 24, as the device does (its list is a fixed size).
    best = sorted(cities.items(), key=lambda kv: kv[1][5] - dist_km(lat, lon, kv[1][0], kv[1][1]) / 200,
                  reverse=True)[:24]
    cities = dict(best)
    # A city already in the nearby list is not suggested twice - it is marked a city there instead.
    for i, n in enumerate(near):
        for off, c in list(cities.items()):
            if c[3] == n[3] and abs(c[0] - n[0]) < 0.01 and abs(c[1] - n[1]) < 0.01:
                near[i] = n[:2] + (34,) + n[3:]
                del cities[off]

    def city_score(h):  # higher is better: ten times the people outweighs 5,000 km
        return h[5] - dist_km(lat, lon, h[0], h[1]) / 200
    cities = sorted(cities.values(), key=city_score, reverse=True)[:MAX_CITIES]

    # Cities go on top when the best one is big and nothing nearby is exactly what was typed.
    top = near[0] if near else None
    near_wins = top is not None and full in top[4].split('|')
    cities_first = bool(cities) and cities[0][5] >= RANK_BIG and not near_wins
    return near, cities, cities_first, reads, files


if __name__ == '__main__':
    lat, lon = float(sys.argv[1]), float(sys.argv[2])
    near, cities, cities_first, reads, files = search(lat, lon, ' '.join(sys.argv[3:]))
    print('%d files, %d reads' % (files, reads))
    groups = [('Cities', cities), ('Nearby', near)]
    if not cities_first:
        groups.reverse()
    for title, hs in groups:
        if hs:
            print(' ', title)
        for la, lo, k, name, key, rank in hs:
            print('    %-32s %-9s %7.1f km%s' % (name[:32], KINDS[k], dist_km(lat, lon, la, lo),
                                               '  rank %d' % rank if rank else ''))
