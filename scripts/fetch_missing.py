"""Добор позиций каталога, которых нет в фиде.

Самостоятельный сборщик: не зависит от `darwin_rag.*`, потому что часть модулей проекта
периодически оказывается недоступна, а добирать позиции нужно срочно — до ночного импорта.
Формат выходного JSON повторяет тот, что пишет штатный обход, и сверяется с ним ключ в ключ.

    python3 scripts/fetch_missing.py --check          # только показать, кого не хватает
    python3 scripts/fetch_missing.py                  # добрать
"""
from __future__ import annotations

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS = ROOT / "catalog"
FEED = ROOT / "darwinshop.xml"
SITEMAP = "https://darwinshop.ru/sitemap.xml"
UA = {"User-Agent": "Mozilla/5.0 (darwin_rag catalog sync)"}


def site_products() -> dict[str, str]:
    xml = requests.get(SITEMAP, timeout=60, headers=UA).text
    out: dict[str, str] = {}
    for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml):
        if "/shop/goods/" not in u:
            continue
        slug = u.rstrip("/").split("/")[-1]
        m = re.search(r"-(\d+)$", slug)
        if m and not slug.startswith(("svg", "sajen")):
            out[m.group(1)] = u
    return out


def parse(html: str, url: str) -> dict:
    s = BeautifulSoup(html, "html.parser")
    slug = url.rstrip("/").split("/")[-1]

    h1 = s.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else slug

    crumbs = [a.get_text(strip=True) for a in s.select("a") if "/shop/category/" in (a.get("href") or "")]
    path = ["Каталог", *dict.fromkeys(crumbs)] if crumbs else ["Каталог"]

    price_el = s.find(attrs={"itemprop": "price"})
    price_raw = (price_el.get("content") if price_el else None) or ""
    price = float(price_raw) if re.fullmatch(r"[\d.]+", price_raw or "") else None

    # Характеристики: имя и значения идут парами внутри additionalProperty.
    # У товара с вариантами значений несколько — берём выбранное, иначе первое:
    # карточка отдаёт вариант по умолчанию, и именно он соответствует цене выше.
    attrs: dict[str, str] = {}
    for item in s.select('[itemprop="additionalProperty"]'):
        n = item.find(attrs={"itemprop": "name"})
        if not n:
            continue
        key = n.get_text(strip=True).rstrip(":").strip()
        vals = item.find_all(attrs={"itemprop": "value"})
        if not vals:
            continue
        sel = [v for v in vals if "selected" in (v.get("class") or [])]
        attrs[key] = (sel[0] if sel else vals[0]).get_text(" ", strip=True)

    # Описание: три вкладки — «Описание», «Устойчивость», «Условия выращивания».
    # Штатный обход склеивает их с заголовками <h3>, повторяем это же.
    tabs = s.select('[class*="content"][class*="tab-"]')
    titles = [li.get_text(strip=True) for li in s.select('li[class*="nav-"]')]
    parts = []
    for i, tab in enumerate(tabs):
        body = tab.decode_contents().strip()
        if not body:
            continue
        head = titles[i] if i < len(titles) else ""
        parts.append((f"<h3>{head}</h3>\n" if head else "") + body)
    desc_html = "\n\n".join(parts)
    desc_text = BeautifulSoup(desc_html, "html.parser").get_text("\n", strip=True)

    imgs = []
    for im in s.select('img[src*="/uploaded/images/shop/goods/"]'):
        src = im.get("src") or ""
        if src.startswith("/"):
            src = "https://darwinshop.ru" + src
        if src not in [x["url"] for x in imgs]:
            imgs.append({"url": src, "alt": im.get("alt") or "preview"})

    avail = ""
    for k in ("Доступность", "Доступность "):
        if k in attrs:
            avail = attrs[k]
            break

    return {
        "slug": slug,
        "url": url,
        "name": name,
        "culture": path[-1] if len(path) > 1 else "",
        "category_path": path,
        "price_rub": price,
        "price_text": f"{price:g} ₽" if price else None,
        "availability": avail,
        "attributes": attrs,
        "description_html": desc_html,
        "description_text": desc_text,
        "images": imgs,
        "raw_html_path": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "parser_version": "fetch_missing-1",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="Только показать, кого не хватает")
    args = ap.parse_args()

    site = site_products()
    feed = {o.get("id") for o in ET.parse(FEED).getroot().iter("offer")}
    missing = sorted(set(site) - feed)
    print(f"на сайте {len(site)} товаров, в фиде {len(feed)} предложений, не хватает {len(missing)}")
    if args.check or not missing:
        for a in missing:
            print(f"   {a:>6}  {site[a].split('/')[-1][:60]}")
        return 0

    ok = fail = 0
    for i, art in enumerate(missing, 1):
        try:
            r = requests.get(site[art], timeout=30, headers=UA)
            r.raise_for_status()
            d = parse(r.text, site[art])
            (PRODUCTS / f"{d['slug']}.json").write_text(
                json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            ok += 1
            print(f"  {i:>3}/{len(missing)}  {art:>5}  {d['name'][:40]:<42} "
                  f"опис {len(d['description_html']):>5} зн., характеристик {len(d['attributes'])}")
        except Exception as e:
            fail += 1
            print(f"  {i:>3}/{len(missing)}  {art:>5}  ОШИБКА: {str(e)[:70]}")
        time.sleep(0.7)
    print(f"\nдобрано: {ok}, ошибок: {fail}")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
