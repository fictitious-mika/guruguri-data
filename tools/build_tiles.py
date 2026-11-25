#!/usr/bin/env python3
"""
ぐるり散歩 用の区画データを作る。

流れ
  1. Geofabrik の日本全域データ(.osm.pbf)から、指定の範囲だけを切り出す        (osmium extract)
  2. 道路 / 公園・水辺・木・施設 を取り出して GeoJSON にする                    (osmium tags-filter, export)
  3. 0.02度(約2km)四方の区画ごとの小さな JSON ファイルに分けて、site/ に書く      (このスクリプト)

出力(site/)
  index.json            どの区画があるか、対象範囲、作成日
  r/<iy>/<ix>.json      道路の区画
  e/<iy>/<ix>.json      公園・水辺・木・施設の区画
  c/<cy>/<cx>.json      大きな公園・森・田畑(0.1度四方の粗い区画)

座標は 10^-6 度の整数。道路の交点は「座標が同じ点」として、アプリ側でつなぎ直す。
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from array import array
from datetime import datetime, timezone

G = 20000      # 細かい区画: 0.02度 (単位は 10^-6 度)
CG = 100000    # 粗い区画:   0.1度

# 切り出す範囲 (西, 南, 東, 北)。範囲を足すときは、ここに1行足す
AREAS = {
    'test':  (139.0, 35.0, 140.95, 36.1),    # 千葉・東京東部・埼玉南部・横浜の一部（試験用）
    'kanto': (138.35, 34.85, 141.0, 37.2),   # 関東1都6県を含む長方形
}

# ---------- 道路 ----------
ROAD_VALUES = {'footway', 'path', 'pedestrian', 'cycleway', 'track', 'steps', 'bridleway', 'living_street',
               'residential', 'service', 'unclassified', 'road', 'tertiary', 'tertiary_link', 'secondary',
               'secondary_link', 'primary', 'primary_link', 'trunk', 'trunk_link'}
ROAD_KEEP = ('highway', 'foot', 'access', 'sidewalk', 'maxspeed', 'width', 'oneway', 'lanes',
             'motor_vehicle', 'motorcar', 'vehicle', 'footway', 'crossing', 'service')
KOKUDO = re.compile('国道')
NODE_KEEP = ('highway', 'crossing', 'crossing:signals')


def e6(x):
    return int(round(x * 1e6))


def cell(lat6, lon6):
    return (lat6 // G, lon6 // G)


def road_tags(p):
    h = p.get('highway')
    if h not in ROAD_VALUES or p.get('area') == 'yes':
        return None
    if p.get('service') in ('driveway', 'parking_aisle', 'drive-through'):
        return None
    d = {k: p[k] for k in ROAD_KEEP if k in p}
    for k in ('name', 'name:ja', 'official_name', 'ref'):       # 国道かどうかの判定にしか使わないので、該当するときだけ残す
        if p.get(k) and KOKUDO.search(p[k]):
            d['name'] = p[k]
            break
    return d


def node_tags(p):
    if p.get('highway') in ('traffic_signals', 'crossing') or p.get('crossing') or p.get('crossing:signals') == 'yes':
        return {k: p[k] for k in NODE_KEEP if k in p}
    return None


# ---------- 公園・水辺・施設 ----------
GREEN_LEISURE = {'park', 'garden', 'nature_reserve'}
GREEN_LANDUSE = {'forest', 'grass', 'meadow', 'recreation_ground', 'village_green'}
GREEN_NATURAL = {'wood', 'scrub', 'grassland', 'wetland'}
FARM_LANDUSE = {'farmland', 'orchard', 'allotments', 'paddy', 'vineyard'}
POI_TYPES = ['toilet', 'vending', 'conv', 'bench', 'cafe', 'food', 'super', 'drug']   # アプリ側の並びと同じにすること


def area_code(p):
    if p.get('natural') == 'water' or p.get('waterway') == 'riverbank':
        return 'A'
    if p.get('landuse') in FARM_LANDUSE:
        return 'F'
    if p.get('natural') == 'wood' or p.get('landuse') == 'forest':
        return 'W'
    if p.get('leisure') in GREEN_LEISURE or p.get('landuse') in GREEN_LANDUSE or p.get('natural') in GREEN_NATURAL:
        return 'G'
    return None


def line_code(p):
    if p.get('natural') == 'coastline':
        return 'C'
    if p.get('waterway') in ('river', 'stream', 'canal'):
        return 'R'
    if p.get('natural') == 'tree_row':
        return 'T'
    return 'A' if area_code(p) == 'A' else None


def poi_type(p):
    a, s = p.get('amenity'), p.get('shop')
    t = None
    if a == 'toilets': t = 'toilet'
    elif a == 'vending_machine' and not re.search('ticket|parking|excrement|bicycle|public_transport', p.get('vending') or ''): t = 'vending'
    elif s == 'convenience': t = 'conv'
    elif a == 'bench': t = 'bench'
    elif a == 'cafe': t = 'cafe'
    elif a in ('restaurant', 'fast_food'): t = 'food'
    elif s == 'supermarket': t = 'super'
    elif s in ('chemist', 'drugstore') or a == 'pharmacy': t = 'drug'
    if not t:
        return None
    if p.get('access') == 'private' or (t == 'toilet' and p.get('access') == 'customers'):
        return None
    return POI_TYPES.index(t)


# ---------- 共通の部品 ----------
def iter_features(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip('\x1e \t\r\n')              # geojsonseq は行頭に RS(0x1e) が付くことがある
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def to_pts(coords):
    """[[lon,lat],...] → [(lat6,lon6),...]（同じ点の連続は1つにする）"""
    out = []
    for c in coords:
        q = (e6(c[1]), e6(c[0]))
        if not out or out[-1] != q:
            out.append(q)
    return out


MAX_SEG_M = 400.0     # これより長い線分は、途中に点を足して分ける（始点の区画にしか入らず、取りこぼすのを防ぐ）


def densify(pts):
    out = [pts[0]]
    for q in pts[1:]:
        p = out[-1]
        dy = (q[0] - p[0]) * 0.110574
        dx = (q[1] - p[1]) * 0.0925         # 日本の緯度での 10^-6 度あたりの東西の長さ(m)。多少ずれても区画分けには影響しない
        n = int(((dx * dx + dy * dy) ** 0.5) // MAX_SEG_M) + 1
        for k in range(1, n):
            out.append((p[0] + (q[0] - p[0]) * k // n, p[1] + (q[1] - p[1]) * k // n))
        out.append(q)
    return out


def flat(pts, head=()):
    """点の列 → [*head, lat0, lon0, dlat1, dlon1, ...]"""
    a = array('i', head)
    pl, po = pts[0]
    a.append(pl); a.append(po)
    for la, lo in pts[1:]:
        a.append(la - pl); a.append(lo - po)
        pl, po = la, lo
    return a


def split_runs(pts):
    """線を、各線分の始点がある区画ごとの連続した部分に分ける。1つの線分は必ず1つの区画にだけ入る"""
    out, i, n = [], 0, len(pts)
    while i < n - 1:
        c = cell(*pts[i]); j = i
        while j < n - 1 and cell(*pts[j]) == c:
            j += 1
        out.append((c, pts[i:j + 1]))
        i = j
    return out


def ring_of(coords):
    pts = to_pts(coords)
    if len(pts) >= 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts if len(pts) >= 4 else None


def outer_rings(g):
    if g['type'] == 'Polygon':
        return [g['coordinates'][0]] if g['coordinates'] else []
    if g['type'] == 'MultiPolygon':
        return [poly[0] for poly in g['coordinates'] if poly]
    return []


def centroid6(pts):
    la = [p[0] for p in pts]; lo = [p[1] for p in pts]
    return ((min(la) + max(la)) // 2, (min(lo) + max(lo)) // 2)


# ---------- 道路の区画 ----------
def build_roads(path, stat):
    tiles = {}

    def tile(c):
        t = tiles.get(c)
        if t is None:
            t = tiles[c] = {'k': {}, 'w': [], 'n': []}
        return t

    for f in iter_features(path):
        g, p = f.get('geometry') or {}, f.get('properties') or {}
        if g.get('type') == 'LineString':
            tags = road_tags(p)
            if not tags:
                continue
            pts = to_pts(g['coordinates'])
            if len(pts) < 2:
                continue
            pts = densify(pts)
            key = json.dumps(tags, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            stat['road_ways'] += 1
            for c, run in split_runs(pts):
                t = tile(c)
                idx = t['k'].setdefault(key, len(t['k']))
                t['w'].append(flat(run, (idx,)))
                stat['road_pieces'] += 1
        elif g.get('type') == 'Point':
            nt = node_tags(p)
            if nt:
                lo, la = g['coordinates'][:2]
                la, lo = e6(la), e6(lo)
                tile(cell(la, lo))['n'].append((la, lo, json.dumps(nt, ensure_ascii=False, separators=(',', ':'))))
    return tiles


def road_json(t):
    keys = sorted(t['k'].items(), key=lambda kv: kv[1])
    ways = ','.join('[' + ','.join(map(str, w)) + ']' for w in t['w'])
    nodes = ','.join('[%d,%d,%s]' % n for n in t['n'])
    return '{"k":[%s],"w":[%s],"n":[%s]}' % (','.join(k for k, _ in keys), ways, nodes)


# ---------- 公園・水辺・施設の区画 ----------
def build_env(path, stat):
    fine, coarse = {}, {}

    def ft(c):
        t = fine.get(c)
        if t is None:
            t = fine[c] = {'l': [], 'a': [], 't': array('i'), 'p': []}
        return t

    def add_lines(code, pts):
        if len(pts) < 2:
            return
        for c, run in split_runs(densify(pts)):
            ft(c)['l'].append((code, flat(run)))
            stat['env_lines'] += 1

    def add_poly(code, pts):
        la = [p[0] for p in pts]; lo = [p[1] for p in pts]
        iy0, iy1, ix0, ix1 = min(la) // G, max(la) // G, min(lo) // G, max(lo) // G
        key = '%d_%d_%d' % (pts[0][0], pts[0][1], len(pts))
        rec = (code, key, flat(pts))
        stat['env_polys'] += 1
        if (iy1 - iy0 + 1) * (ix1 - ix0 + 1) <= 4:
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    ft((iy, ix))['a'].append(rec)
        else:
            cy0, cy1, cx0, cx1 = min(la) // CG, max(la) // CG, min(lo) // CG, max(lo) // CG
            if (cy1 - cy0 + 1) * (cx1 - cx0 + 1) > 400:
                stat['env_skipped_huge'] += 1
                return
            for cy in range(cy0, cy1 + 1):
                for cx in range(cx0, cx1 + 1):
                    coarse.setdefault((cy, cx), []).append(rec)
            stat['env_coarse'] += 1

    def add_poi(ti, p, la, lo):
        name = (p.get('name') or p.get('name:ja') or '')[:40]
        ft(cell(la, lo))['p'].append((la, lo, ti, name))
        stat['pois'] += 1

    for f in iter_features(path):
        g, p = f.get('geometry') or {}, f.get('properties') or {}
        gt = g.get('type')
        if gt == 'Point':
            lo, la = g['coordinates'][:2]
            la, lo = e6(la), e6(lo)
            if p.get('natural') == 'tree':
                ft(cell(la, lo))['t'].extend((la, lo)); stat['trees'] += 1
                continue
            ti = poi_type(p)
            if ti is not None:
                add_poi(ti, p, la, lo)
        elif gt == 'LineString':
            pts = to_pts(g['coordinates'])
            ti = poi_type(p)
            if ti is not None and len(pts) >= 2:
                add_poi(ti, p, *centroid6(pts))
            code = line_code(p)
            if code:
                add_lines(code, pts)
        elif gt in ('Polygon', 'MultiPolygon'):
            rings = [r for r in (ring_of(c) for c in outer_rings(g)) if r]
            if not rings:
                continue
            ti = poi_type(p)
            if ti is not None:
                add_poi(ti, p, *centroid6(rings[0]))
            code = area_code(p)
            if code == 'A':
                for r in rings:
                    add_lines('A', r)                       # 水面の中に道はないので、岸の線だけ使う
            elif code:
                for r in rings:
                    add_poly(code, r)
    return fine, coarse


def poly_items(recs):
    return ','.join('["%s","%s",%s]' % (c, k, ','.join(map(str, a))) for c, k, a in recs)


def env_json(t):
    lines = ','.join('["%s",%s]' % (c, ','.join(map(str, a))) for c, a in t['l'])
    polys = poly_items(t['a'])
    trees = ','.join(map(str, t['t']))
    pois = ','.join('[%d,%d,%d,%s]' % (la, lo, ti, json.dumps(nm, ensure_ascii=False)) for la, lo, ti, nm in t['p'])
    return '{"l":[%s],"a":[%s],"t":[%s],"p":[%s]}' % (lines, polys, trees, pois)


# ---------- 書き出し ----------
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return len(text.encode('utf-8'))


def write_all(out, bbox, roads, fine, coarse):
    total = 0
    man = {'v': 1, 'g': G / 1e6, 'c': CG / 1e6, 'bbox': list(bbox),
           'built': datetime.now(timezone.utc).strftime('%Y-%m-%d'), 'roads': [], 'env': [], 'coarse': []}
    for (iy, ix), t in sorted(roads.items()):
        total += write(os.path.join(out, 'r', str(iy), '%d.json' % ix), road_json(t))
        man['roads'].append('%d/%d' % (iy, ix))
    for (iy, ix), t in sorted(fine.items()):
        total += write(os.path.join(out, 'e', str(iy), '%d.json' % ix), env_json(t))
        man['env'].append('%d/%d' % (iy, ix))
    for (cy, cx), recs in sorted(coarse.items()):
        total += write(os.path.join(out, 'c', str(cy), '%d.json' % cx), '{"a":[%s]}' % poly_items(recs))
        man['coarse'].append('%d/%d' % (cy, cx))
    total += write(os.path.join(out, 'index.json'), json.dumps(man, separators=(',', ':')))
    write(os.path.join(out, '.nojekyll'), '')
    write(os.path.join(out, 'index.html'), PAGE.replace('{built}', man['built']))
    return man, total


PAGE = """<!doctype html><meta charset="utf-8"><title>ぐるり散歩 地図データ</title>
<p>「ぐるり散歩」用の地図データ置き場です（作成日 {built}）。</p>
<p>地図データ © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>
（<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>）。このデータは、同じ条件で利用できます。</p>
"""


def sh(*cmd):
    print('+', ' '.join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def run_osmium(pbf, bbox, work):
    os.makedirs(work, exist_ok=True)
    area = os.path.join(work, 'area.osm.pbf')
    sh('osmium', 'extract', '--overwrite', '--bbox=' + ','.join(str(v) for v in bbox), '-o', area, pbf)
    roads_pbf, roads_geo = os.path.join(work, 'roads.osm.pbf'), os.path.join(work, 'roads.geojsonseq')
    sh('osmium', 'tags-filter', '--overwrite', '-o', roads_pbf, area, 'w/highway')
    sh('osmium', 'export', '--overwrite', '-f', 'geojsonseq', '--geometry-types=point,linestring', '-o', roads_geo, roads_pbf)
    os.remove(roads_pbf)
    env_pbf, env_geo = os.path.join(work, 'env.osm.pbf'), os.path.join(work, 'env.geojsonseq')
    sh('osmium', 'tags-filter', '--overwrite', '-o', env_pbf, area,
       'nwr/leisure=park,garden,nature_reserve',
       'nwr/landuse=forest,grass,meadow,recreation_ground,village_green,farmland,orchard,allotments,paddy,vineyard',
       'nwr/natural=wood,scrub,grassland,wetland,water,coastline,tree_row,tree',
       'nwr/waterway=river,stream,canal,riverbank',
       'nwr/amenity=toilets,vending_machine,bench,cafe,restaurant,fast_food,pharmacy',
       'nwr/shop=convenience,supermarket,chemist,drugstore')
    os.remove(area)
    sh('osmium', 'export', '--overwrite', '-f', 'geojsonseq', '--geometry-types=point,linestring,polygon', '-o', env_geo, env_pbf)
    os.remove(env_pbf)
    return roads_geo, env_geo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--area', default='test', choices=sorted(AREAS))
    ap.add_argument('--pbf', default='japan-latest.osm.pbf')
    ap.add_argument('--out', default='site')
    ap.add_argument('--work', default='work')
    ap.add_argument('--roads-geojson', help='(試験用) osmium を使わず、この GeoJSON から作る')
    ap.add_argument('--env-geojson', help='(試験用)')
    ap.add_argument('--bbox', help='(試験用) 西,南,東,北')
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(',')) if a.bbox else AREAS[a.area]
    t0 = time.time()
    if a.roads_geojson:
        roads_geo, env_geo = a.roads_geojson, a.env_geojson
    else:
        roads_geo, env_geo = run_osmium(a.pbf, bbox, a.work)
    stat = {k: 0 for k in ('road_ways', 'road_pieces', 'env_lines', 'env_polys', 'env_coarse', 'env_skipped_huge', 'trees', 'pois')}
    roads = build_roads(roads_geo, stat)
    print('道路 変換完了 %.0f秒' % (time.time() - t0), flush=True)
    fine, coarse = build_env(env_geo, stat) if env_geo else ({}, {})
    print('公園など 変換完了 %.0f秒' % (time.time() - t0), flush=True)
    if os.path.isdir(a.out):
        shutil.rmtree(a.out)
    man, total = write_all(a.out, bbox, roads, fine, coarse)
    print(json.dumps(stat, ensure_ascii=False))
    print('区画: 道路 %d / 公園など %d / 粗い %d。合計 %.1f MB（圧縮前）。%.0f秒' % (
        len(man['roads']), len(man['env']), len(man['coarse']), total / 1048576, time.time() - t0))
    if total > 900 * 1048576:
        print('警告: 合計が900MBを超えています。GitHub Pages の上限(約1GB)に近づいています', file=sys.stderr)


if __name__ == '__main__':
    main()
