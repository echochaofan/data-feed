#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端版：从 Shopify 拉 pdp 全字段，生成 GMC 补充数据源 XML。

专为 GitHub Actions / 任意无状态容器设计：
  * 凭证全部从环境变量读，不依赖本地 stores.json
  * 只用 Python 标准库，无需 pip install
  * 直连 Shopify Admin API（GitHub runner 在海外，无需代理）
  * 输出 public/supplement_feed.xml，由 workflow 部署到 GitHub Pages

环境变量（必填其一）：
  SHOPIFY_STORE_DOMAIN   c8ntcr-ij.myshopify.com
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
      variants(first: %d){ nodes { id } }
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


def material_of(pdp):
    for sec in ("specs", "facts"):
        for it in (pdp.get(sec) or []):
            if str(it.get("label", "")).strip().lower() in ("material", "materials"):
                v = " ".join(str(it.get("value", "")).split())
                if v:
                    return v
    return None


def fetch_all(token):
    products = []
    after = None
    page = 0
    while True:
        data = gql(token, Q, {"after": after})
        conn = data["products"]
        for n in conn.get("nodes") or []:
            pid = gid_num(n.get("id"))
            vids = [gid_num(v.get("id")) for v in (n.get("variants") or {}).get("nodes", []) if v.get("id")]
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


def build_xml(products):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
             "  <channel>",
             "    <title>SnapFolda Supplemental Feed</title>",
             "    <link>%s</link>" % escape(SHOP_URL),
             "    <description>Supplemental material and product_highlight for SnapFolda</description>"]
    seen = set()
    items = 0
    for o in products:
        mat = o.get("material")
        hl = build_highlights(o)
        if not mat and not hl:
            continue
        for vid in o.get("variant_ids", []):
            gid = "%s%s_%s" % (ID_PREFIX, o["product_id"], vid)
            if gid in seen:
                continue
            seen.add(gid)
            lines.append("    <item>")
            lines.append("      <g:id>%s</g:id>" % escape(gid))
            if mat:
                lines.append("      <g:material>%s</g:material>" % escape(mat))
            for s in hl:
                lines.append("      <g:product_highlight>%s</g:product_highlight>" % escape(s))
            lines.append("    </item>")
            items += 1
    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines), items


def main():
    if not DOMAIN and not os.environ.get("SHOPIFY_ACCESS_TOKEN"):
        raise SystemExit("缺少 SHOPIFY_STORE_DOMAIN")
    token = get_token()
    print("token ok, fetching products...", flush=True)
    products = fetch_all(token)
    print("fetched %d products, building xml..." % len(products), flush=True)
    xml, items = build_xml(products)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(xml, encoding="utf-8")
    size = OUT.stat().st_size
    print("DONE items=%d products=%d size=%.2fMB -> %s" % (items, len(products), size / 1024 / 1024, OUT), flush=True)


if __name__ == "__main__":
    main()
