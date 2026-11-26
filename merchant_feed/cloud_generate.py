#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端版：从 Shopify 拉 pdp 全字段，生成 GMC 补充数据源 XML。

专为 GitHub Actions / 任意无状态容器设计：
  * 凭证全部从环境变量读，不依赖本地 stores.json
  * 只用 Python 标准库，无需 pip install
  * 直连 Shopify Admin API（GitHub runner 在海外，无需代理）
  * 输出 public/supplement_feed.xml，由 workflow 部署到 GitHub Pages

环境变量（必填其一）：
  SHOPIFY_STORE_DOMAIN   <store>.myshopify.com
  SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET   （client_credentials 换 token）
  或 SHOPIFY_ACCESS_TOKEN （静态 token，二选一）

可选：
  OUTPUT_XML             默认 public/supplement_feed.xml
  SHOPIFY_API_VERSION    默认 2024-10
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_highlight_rewrite import build_highlights  # noqa: E402

API_VER = os.environ.get("SHOPIFY_API_VERSION", "2024-10").strip()
DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip().rstrip("/")
PAGE = 50
VPAGE = 250
ID_PREFIX = "shopify_ZZ_"
OUT = Path(os.environ.get("OUTPUT_XML", "public/supplement_feed.xml"))
SHOP_URL = "https://snapfolda.com"

Q = '''
query($after: String){
  products(first: %d, after: $after){
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      variants(first: %d){ nodes { id title selectedOptions { name value } } }
      pdp: metafield(namespace:"snapfolda", key:"pdp"){ value }
    }
  }
}''' % (PAGE, VPAGE)


def _http(req, timeout=60):
    for i in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < 5:
                time.sleep(min(2 ** i, 30))
                continue
            body = ""
            try:
                body = e.read().decode(errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError("HTTP %d: %s" % (e.code, body)) from e
        except (urllib.error.URLError, OSError, TimeoutError):
            if i == 5:
                raise
            time.sleep(min(2 ** i, 30))


def get_token():
    tok = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    cid = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    sec = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    if not (DOMAIN and cid and sec):
        raise SystemExit("缺少凭证：需要 SHOPIFY_STORE_DOMAIN + SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET，或 SHOPIFY_ACCESS_TOKEN")
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid, "client_secret": sec,
    }).encode()
    req = urllib.request.Request(
        "https://%s/admin/oauth/access_token" % DOMAIN, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    out = _http(req, timeout=30)
    if not out.get("access_token"):
        raise SystemExit("token 端点未返回 access_token: %s" % str(out)[:300])
    return out["access_token"]


def gql(token, query, variables):
    delay = 2.0
    for _ in range(10):
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            "https://%s/admin/api/%s/graphql.json" % (DOMAIN, API_VER), data=body, method="POST",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"})
        out = _http(req)
        errs = out.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
            print("  throttled %.1fs" % delay, flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if errs:
            raise RuntimeError("GraphQL errors: " + json.dumps(errs, ensure_ascii=False)[:600])
        return out["data"]
    raise RuntimeError("THROTTLED retries exhausted")


def gid_num(g):
    return str(g or "").rsplit("/", 1)[-1]


def _norm_material(v):
    """Feed-layer display normalization (does not touch Shopify source data)."""
    if not v:
        return v
    low = v.lower()
    if "calfskin" in low:
        return "Vegan leather" if "vegan" in low else "Leather"
    return v


def material_of(pdp):
    for sec in ("specs", "facts"):
        for it in (pdp.get(sec) or []):
            if str(it.get("label", "")).strip().lower() in ("material", "materials"):
                v = " ".join(str(it.get("value", "")).split())
                if v:
                    return _norm_material(v)
    return None


def variant_color(v):
    """GMC 标题后缀用色：优先 Color 选项值，退回变体标题。"""
    for o in (v.get("selectedOptions") or []):
        if str(o.get("name", "")).strip().lower() == "color":
            return " ".join(str(o.get("value", "")).split())
    return " ".join(str(v.get("title", "")).split())


def fetch_all(token):
    products = []
    after = None
    page = 0
    while True:
        data = gql(token, Q, {"after": after})
        conn = data["products"]
        for n in conn.get("nodes") or []:
            pid = gid_num(n.get("id"))
            vnodes = [v for v in (n.get("variants") or {}).get("nodes", []) if v.get("id")]
            vids = [gid_num(v.get("id")) for v in vnodes]
            colors = {gid_num(v.get("id")): variant_color(v) for v in vnodes}
            mf = n.get("pdp") or {}
            pdp = {}
            if mf.get("value"):
                try:
                    pdp = json.loads(mf["value"])
                except Exception:
                    pdp = {}
            products.append({
                "product_id": pid,
                "variant_ids": vids,
                "variant_colors": colors,
                "facts": pdp.get("facts") or [],
                "features": pdp.get("features") or [],
                "specs": pdp.get("specs") or [],
                "material": material_of(pdp),
                "highlights": pdp.get("highlights") or [],
            })
        page += 1
        pi = conn.get("pageInfo") or {}
        after = pi.get("endCursor")
        print("  page %d total %d" % (page, len(products)), flush=True)
        if not pi.get("hasNextPage"):
            break
    return products


SKIP_FILE = Path("merchant_feed/data/gmc_skip_offers.txt")
TITLE_FILE = Path(__file__).resolve().parent / "data" / "gmc_title_overrides.json"


def load_title_overrides():
    """产品级 title 覆盖表（pid -> title）。文件缺失时该字段整体不输出。"""
    try:
        if TITLE_FILE.exists():
            raw = json.loads(TITLE_FILE.read_text(encoding="utf-8"))
            t = raw.get("titles") if isinstance(raw, dict) and "titles" in raw else raw
            return {str(k): " ".join(str(v).split()) for k, v in (t or {}).items() if v}
    except Exception as e:
        print("  title overrides skipped: %s" % e, flush=True)
    return {}


# 覆盖表产品的机型 -> custom_label_3 值（仅这两组写入，其余机型不输出该字段）
MODEL_LABELS = {
    "iPhone 17": "ip17",
    "iPhone 17 Pro Max": "ip17promax",
}


def label_for_title(title):
    """从覆盖表标题前缀反推机型标签；未登记机型返回空串。"""
    i = title.find(" Wallet Phone Case")
    if i <= 0:
        return ""
    return MODEL_LABELS.get(title[:i].strip(), "")


def load_skip_offers():
    """仅向 GMC 中已存在的 offer 提交补充数据；清单缺失时退化为全量。"""
    try:
        if SKIP_FILE.exists():
            return {ln.strip() for ln in SKIP_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except Exception:
        pass
    return set()


def build_xml(products):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
             "  <channel>",
             "    <title>Supplemental Product Feed</title>",
             "    <link>%s</link>" % escape(SHOP_URL),
             "    <description>Supplemental material and product_highlight data</description>"]
    seen = set()
    skip = load_skip_offers()
    titles = load_title_overrides()
    items = 0
    t_items = 0
    l_items = 0
    for o in products:
        mat = o.get("material")
        hl = build_highlights(o)
        ttl = titles.get(o.get("product_id"))
        if not mat and not hl and not ttl:
            continue
        for vid in o.get("variant_ids", []):
            gid = "%s%s_%s" % (ID_PREFIX, o["product_id"], vid)
            if gid in seen or gid in skip:
                continue
            seen.add(gid)
            lines.append("    <item>")
            lines.append("      <g:id>%s</g:id>" % escape(gid))
            if ttl:
                col = (o.get("variant_colors") or {}).get(vid, "")
                val = (ttl + " " + col).strip() if col else ttl
                lines.append("      <g:title>%s</g:title>" % escape(val))
                t_items += 1
                lbl = label_for_title(ttl)
                if lbl:
                    lines.append("      <g:custom_label_3>%s</g:custom_label_3>" % escape(lbl))
                    l_items += 1
            if mat:
                lines.append("      <g:material>%s</g:material>" % escape(mat))
            for s in hl:
                lines.append("      <g:product_highlight>%s</g:product_highlight>" % escape(s))
            lines.append("    </item>")
            items += 1
    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines), items, t_items, l_items


def main():
    if not DOMAIN and not os.environ.get("SHOPIFY_ACCESS_TOKEN"):
        raise SystemExit("缺少 SHOPIFY_STORE_DOMAIN")
    token = get_token()
    print("token ok, fetching products...", flush=True)
    products = fetch_all(token)
    print("fetched %d products, building xml..." % len(products), flush=True)
    xml, items, t_items, l_items = build_xml(products)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)
    size = OUT.stat().st_size
    print("DONE items=%d titles=%d labels=%d products=%d size=%.2fMB -> %s"
          % (items, t_items, l_items, len(products), size / 1024 / 1024, OUT), flush=True)


if __name__ == "__main__":
    main()
