#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""product_highlight 增量改写引擎 -> GMC 补充数据源 TSV.

原则（用户 2026-11-22 定稿）：
  * 允许核心卖点跨字段重复（Title/Desc 已说 RFID/可拆 也没关系）
  * 每条 highlight 必须叠加一个「具体产品信息」或「购买利益点」
  * 自然英文完整短句，50-70 字符，去品类关键词堆砌

做法：以 pdp.features 的 id 作为「卖点锚」，按 4 个维度（安全 / 充电 / 形态 / 体验）
各取一条，并把该产品的事实（卡槽数 / 材质 / 颜色数 / 防摔 / 支架）注入成具体增量。
同一产品内 4 条增量点互不撞车。

输入: out/supplement/pdp_full_snapshot.jsonl
输出: out/supplement/product_highlight_v2.tsv (+ highlight_v2_report.json)

用法:
  python generate_highlight_rewrite.py --dry-run --limit 20   # 只看样稿
  python generate_highlight_rewrite.py --build                # 全量出 TSV
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "supplement"
SNAP = OUT / "pdp_full_snapshot.jsonl"
TSV = OUT / "product_highlight_v2.tsv"
REPORT = OUT / "highlight_v2_report.json"

ID_PREFIX = "shopify_ZZ_"
MAX_COLS = 4

NUM_WORD = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
            7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
            12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen"}


def fids(o):
    return {str(f.get("id")) for f in (o.get("features") or []) if f.get("id")}


def slots_of(o):
    for f in (o.get("facts") or []):
        if str(f.get("id")) == "cards":
            v = str(f.get("value", "")).strip()
            if v.isdigit():
                return int(v)
            return "builtin" if v else None
    return None


def colors_of(o):
    for s in (o.get("specs") or []):
        if str(s.get("label", "")).lower() == "colors":
            v = str(s.get("value", ""))
            n = len([x for x in v.split(",") if x.strip()])
            return n or None
    return None


def short_mat(mat):
    if not mat:
        return None
    m = mat.lower()
    if "vegan calfskin" in m or "vegan leather" in m:
        return "vegan leather"
    if "faux leather" in m:
        return "faux leather"
    if "aluminum" in m:
        return "aluminum"
    if "silicone" in m:
        return "liquid silicone"
    if "tpu" in m:
        return "TPU shell"
    return None


def nword(n):
    return NUM_WORD.get(n, str(n))


def build_highlights(o):
    """返回最多 4 条自然短句。每条 = 卖点锚 + 具体增量。

    同一卖点（如 detachable）可支撑两条不同增量：
      ① MagSafe 充电不用脱钱包（充电维度）
      ② 拆下变薄壳单独用（形态维度）
    这正是"允许卖点跨字段重复、但每条加新信息"的落点。
    """
    F = fids(o)
    slots = slots_of(o)
    mat = short_mat(o.get("material"))
    ncolor = colors_of(o)
    pid = str(o.get("product_id", ""))
    h = sum(ord(c) for c in pid)     # 用于变体轮换，避免全站同一句
    out = []
    used = set()          # 维度锚已用
    taken_bits = set()    # 已占用的「增量点」，防条内撞车

    # 维度一：安全 / 防护（rfid > shock > camera > zipper > heavy）
    if "rfid" in F:
        if isinstance(slots, int) and slots > 0:
            if slots == 1:
                out.append("RFID-blocking lining shields the single card slot from skimming.")
            else:
                out.append("RFID-blocking lining shields all %s card slots from skimming." % nword(slots))
            taken_bits.add("slots")
        elif slots == "builtin":
            out.append("RFID-blocking lining shields built-in card storage from skimming.")
            taken_bits.add("slots")
        else:
            out.append("RFID-blocking lining shields your cards from wireless skimming.")
        used.add("safe")
    elif "shock" in F:
        out.append("Reinforced corners absorb the shock of everyday drops.")
        used.add("safe")
    elif "camera" in F:
        out.append("A raised camera lip keeps the lens clear of flat surfaces.")
        used.add("safe")
    elif "zipper" in F:
        out.append("A zippered pocket keeps coins and cash from spilling out.")
        used.add("safe")
        taken_bits.add("cash")
    elif "heavy" in F:
        out.append("A reinforced frame stands up to rough daily handling.")
        used.add("safe")

    # 维度二：充电 / 连接（magsafe > wireless）
    if "magsafe" in F:
        if "detachable" in F:
            out.append("The detachable shell charges on MagSafe without removing the wallet.")
        else:
            out.append("Built-in magnets lock onto MagSafe chargers and mounts.")
        used.add("power")
    elif "wireless" in F:
        out.append("It charges wirelessly without taking the case off.")
        used.add("power")

    # 维度三：形态 / 设计（detachable > folio > magnetic > slim）
    # 注意：detachable 即使已被 MagSafe 那条用过，这里仍讲"变薄壳"这个不同增量。
    if "detachable" in F:
        out.append("The detachable inner shell works as a slim standalone case.")
        used.add("detach")
    elif "folio" in F:
        out.append("The folding front cover guards the screen from daily scratches.")
        used.add("form")
    elif "magnetic" in F:
        out.append("A magnetic flap keeps everything shut inside your bag.")
        used.add("form")
    elif "slim" in F:
        out.append("The slim profile slides easily into a front pocket.")
        used.add("form")

    # 维度四：携带 / 体验（crossbody 优先——它改变携带方式，是最强差异化卖点）
    if "crossbody" in F:
        out.append("The detachable crossbody strap frees both hands on the move.")
        used.add("carry")
        taken_bits.add("crossbody")
    elif "kickstand" in F:
        if h % 2 == 0:
            out.append("A built-in kickstand props the phone up for hands-free viewing.")
        else:
            out.append("A built-in kickstand holds the screen at a comfortable angle.")
        used.add("carry")
        taken_bits.add("kickstand")
    elif "wrist" in F:
        out.append("A wrist strap keeps the phone secure in one hand.")
        used.add("carry")
        taken_bits.add("wrist")
    elif "lanyard" in F:
        out.append("The lanyard lets you wear the phone around your neck.")
        used.add("carry")
        taken_bits.add("lanyard")
    elif "ring" in F:
        out.append("A rotating ring doubles as a stand and a finger grip.")
        used.add("carry")
    elif "clutch" in F:
        out.append("It doubles as a compact clutch for evenings out.")
        used.add("carry")

    # 补充（优先补未用到的第二卖点，材质/卡槽/颜色最后兜底）
    # 1) 现金袋
    if "cash" in F and "cash" not in taken_bits and len(out) < MAX_COLS:
        out.append("A hidden cash pocket keeps bills apart from your cards.")
        taken_bits.add("cash")

    # 2) 未被维度四选中的第二携带卖点（kickstand / wrist 等）
    if "kickstand" in F and "kickstand" not in taken_bits and len(out) < MAX_COLS:
        out.append("A built-in kickstand holds the screen at a comfortable angle.")
        taken_bits.add("kickstand")
    elif "wrist" in F and "wrist" not in taken_bits and len(out) < MAX_COLS:
        out.append("A wrist strap keeps the phone secure in one hand.")
        taken_bits.add("wrist")

    # 3) 材质
    if mat and len(out) < MAX_COLS and "mat" not in taken_bits:
        if mat == "faux leather":
            variants = [
                "Finished in soft faux leather with a matte hand-feel.",
                "The pebbled faux leather resists scuffs and fingerprints.",
                "A smooth faux leather finish keeps it looking neat over time.",
            ]
            out.append(variants[h % len(variants)])
        elif mat == "vegan leather":
            out.append("Wrapped in vegan leather for a supple, premium feel.")
        elif mat == "aluminum":
            out.append("A forged aluminum body stays light but rigid.")
        elif mat == "liquid silicone":
            out.append("Liquid silicone gives it a grippy, smudge-resistant surface.")
        elif mat == "TPU shell":
            out.append("A flexible TPU shell absorbs impact without adding bulk.")
        taken_bits.add("mat")

    # 4) 卡槽数
    if "slots" not in taken_bits and isinstance(slots, int) and slots > 0 \
            and "card" in F and len(out) < MAX_COLS:
        if slots == 1:
            out.append("One card slot keeps your most-used card within reach.")
        else:
            out.append("%s card slots hold your IDs and daily payment cards." % nword(slots).capitalize())
        taken_bits.add("slots")

    # 5) 颜色数：最后兜底，且句中不大写
    if ncolor and ncolor >= 2 and len(out) < MAX_COLS:
        out.append("Comes in %s colors to match your everyday carry." % nword(ncolor))

    # 去重 + 长度兜底（<=150）
    seen = set()
    clean = []
    for s in out:
        s = " ".join(s.split())
        if s in seen:
            continue
        seen.add(s)
        if len(s) > 150:
            continue
        clean.append(s)
    return clean[:MAX_COLS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印样稿")
    ap.add_argument("--limit", type=int, help="dry-run 取样数量")
    ap.add_argument("--build", action="store_true", help="全量生成 TSV")
    ap.add_argument("--prefix", default=ID_PREFIX)
    a = ap.parse_args()

    if not SNAP.exists():
        raise SystemExit("缺少 %s" % SNAP)

    rows = []
    for line in SNAP.open(encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))

    if a.dry_run:
        import random
        random.seed(7)
        pool = rows if not a.limit else random.sample(rows, min(a.limit, len(rows)))
        for o in pool[:a.limit or 20]:
            hl = build_highlights(o)
            print("=" * 80)
            print("PROD", o["product_id"], "| mat", o.get("material"),
                  "| slots", slots_of(o), "| feats", sorted(fids(o)))
            for s in hl:
                print("   -", s, "(%d)" % len(s))
        return

    if a.build:
        eligible, skipped, lens = [], 0, []
        cols = 0
        seen_ids = set()
        for o in rows:
            hl = build_highlights(o)
            if len(hl) < 2:
                skipped += 1
                continue
            cols = max(cols, len(hl))
            lens.extend(len(x) for x in hl)
            for vid in o.get("variant_ids", []):
                gid = "%s%s_%s" % (a.prefix, o["product_id"], vid)
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                eligible.append((gid, hl))
        eligible.sort(key=lambda r: r[0])
        header = ["id"] + ["product_highlight"] * cols
        with TSV.open("w", encoding="utf-8", newline="") as fh:
            fh.write("\t".join(header) + "\n")
            for gid, hl in eligible:
                cells = [gid] + list(hl) + [""] * (cols - len(hl))
                cells = [str(c).replace("\t", " ").replace("\n", " ") for c in cells]
                fh.write("\t".join(cells) + "\n")
        rep = {
            "rows": len(eligible),
            "products_skipped_lt2": skipped,
            "columns": cols,
            "min_chars": min(lens) if lens else 0,
            "max_chars": max(lens) if lens else 0,
            "avg_chars": round(sum(lens) / len(lens), 1) if lens else 0,
            "over_70": sum(1 for x in lens if x > 70),
            "over_150": sum(1 for x in lens if x > 150),
        }
        REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("DONE build:", json.dumps(rep, ensure_ascii=False), flush=True)
        print("  ->", TSV, flush=True)


if __name__ == "__main__":
    main()
