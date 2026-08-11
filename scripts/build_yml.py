"""Сборка YML-фида каталога для uniSiter.

Разработчик сайта ответил 2026-07-31: потоварного приоритета у платформы нет,
переключение на YML возможно только целиком. Отсюда главное свойство этого скрипта:
**фид обязан содержать весь каталог, а не только позиции с нашими материалами**.
Позиция, отсутствующая в фиде, после переключения исчезает с сайта.

Поэтому источников два, и они складываются, а не выбираются:

  1. `data/catalog/products/*.json` — то, что сейчас стоит на сайте. База фида.
     Для позиции без наших материалов её содержимое проходит НАСКВОЗЬ без изменений.
  2. `data/site_cards/{slug}.md`   — наш материал. Там, где он есть, перекрывает
     описание, характеристики и заголовок.

Скрипт не генерирует контент. Он сшивает и проверяет.

    .venv/bin/python -m scripts.build_yml --out data/feed/darwinshop.yml
    .venv/bin/python -m scripts.build_yml --picture-base https://cdn.example.ru/darwin
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS = ROOT / "catalog"
SITE_CARDS = ROOT / "cards"
PHOTO_FINAL = ROOT / "images"
LABELS = ROOT / "images"

SHOP = {
    "name": "Сажень",
    "company": "Питомник «Сажень»",
    "url": "https://darwinshop.ru",
}

# Предел длины описания. 3000 символов — правило Яндекс.Маркета для фидов, которые уходят
# В Яндекс; наш фид уходит в импортёр uniSiter, и на него это правило не распространяется.
# Поэтому по умолчанию предел не применяется: медиана наших описаний — 5,7 тыс. знаков,
# и жёсткая отсечка выбросила бы 18 карточек из 21, то есть весь смысл затеи.
# Ключ --description-limit включает отсечку, если разработчик подтвердит ограничение.
DESCRIPTION_LIMIT: int | None = None
NO_PICTURES = False   # режим «только описание»: отсутствие картинки не считается браком
PASS_THROUGH = False  # снимок сайта: пустое описание воспроизводится, а не отбраковывается
ZERO_PRICE = False    # подставить 0 там, где цены нет, чтобы товар не выпал из фида

# Список артикулов, по которым наш материал разрешён к публикации (см. data/feed/approved.txt).
# None — режим без ограничений: публикуется всё, что у нас есть.
# Пустое множество — фид становится дословной копией сайта: ровно то состояние,
# с которого владелец хочет начать переключение, чтобы на витрине ничего не дрогнуло.
APPROVED: set[str] | None = None
YANDEX_DESCRIPTION_LIMIT = 3000

# Адреса, которые на сайте действительно существуют. Ссылка на что-либо иное
# превращается в обычный текст: карточки ссылаются на хаб-страницы («Подготовка участка»,
# «Опыление в холодном томском июне» и ещё семь), которых на сайте пока нет, — по корпусу
# таких ссылок больше четырёхсот. Ссылка в никуда на витрине хуже её отсутствия.
# Когда хабы появятся, сюда добавляется "/hub/", и текст оживает сам.
LIVE_LINK_PREFIXES = ("/shop/goods/", "/shop/category/", "https://darwinshop.ru", "http://darwinshop.ru")

# Характеристики, которые НИКОГДА не отдаются в фид, даже если объявлены в карточке.
#
# «Фасовка» — единственная законная ось переключения вариантов. Характеристика
# с несколькими значениями работает на сайте выбором товара: 160 мл / Р9 / С3 у Авроры,
# клик вызывает ChangeGoods → /shop/ajax_goods.php и подставляет другой товар со своей
# ценой. Наш обход видит только вариант по умолчанию и знает одно значение; отдай мы его —
# пара схлопнется, кнопки исчезнут, а вместе с ними возможность купить остальные фасовки.
#
# Остальное здесь — склад и коммерция: меняется ежедневно и живёт в учёте, а не в тексте.
#
# «Производитель» отсюда убран 2026-08-08: питомник сообщил, что переключатель по этому
# полю (виденный у Бакчарского Великана) — их внутренняя ошибка, осью он быть не должен.
BELONGS_TO_1C = {
    "фасовка",                          # единственная ось вариантов
    "доступность", "скидка", "цена",    # склад и коммерция
    "пометкаудаления",                  # служебный флаг 1С: снят с продажи. На витрине ему не место
}
VARIANT_AXES = BELONGS_TO_1C  # прежнее имя: на него ссылаются скрипты проверки

# Поля, которые ПЕРЕДАЁМ, но не ВЫВОДИМ. Их сообщает оператор питомника; исследованием
# они не устанавливаются в принципе.
#
# Досье описывает сорт: высота взрослого дерева, масса плода, зимостойкость. Высота саженца
# в конкретной партии — свойство поставки, оно меняется от завоза к завозу, и вывести его
# неоткуда. Агент, написавший «Высота саженца: 0,5–0,8 м» по аналогии с соседней карточкой,
# совершает ровно ту подмену, против которой выстроен весь проект: правдоподобное вместо
# известного. Поэтому поле либо приходит от оператора, либо отсутствует. Пустых и
# приблизительных значений здесь быть не может.
OPERATOR_SUPPLIED = {
    "высота саженца", "размер саженца", "тип саженца", "возраст саженца",
    "объём контейнера", "производитель",
}

# Блоки витринной карточки, которые НЕ идут на сайт: служебные пометки редактору.
EDITOR_ONLY = re.compile(r"^##\s*(Примечания для редактора|Служебн)", re.I | re.M)


@dataclass
class Offer:
    offer_id: str
    slug: str
    url: str
    name: str
    price: float | None
    available: bool
    category_path: list[str]
    description: str
    params: list[tuple[str, str]] = field(default_factory=list)
    pictures: list[str] = field(default_factory=list)
    source: str = "1С"  # «1С» — проход насквозь, «наш» — перекрыт нашим материалом
    fetched_at: str = ""  # когда скачана карточка товара; решает спор одинаковых артикулов
    problems: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- разбор

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """YAML-шапка карточки → (dict, тело).

    Разбираем сами, без PyYAML: нам нужны только скаляры и плоские списки,
    а тянуть зависимость ради этого не стоит. Вложенные структуры (pollinators,
    planting_material) в фид не идут, и их достаточно пропустить.
    """
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    head, body = m.group(1), m.group(2)
    data: dict = {}
    key = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\s+-\s", line):  # элемент списка
            if key:
                cur = data.get(key)
                if not isinstance(cur, list):
                    cur = data[key] = []
                cur.append(_scalar(line.split("-", 1)[1].strip()))
            continue
        # Вложенная пара под родительским ключом — так записан yml_params, где имена
        # характеристик русские и с пробелами («Масса плода: "84–140 г"»).
        if (nested := re.match(r"^\s+(\S.*?):\s*(.*)$", line)) and key:
            cur = data.get(key)
            if not isinstance(cur, dict):
                cur = data[key] = {}
            cur[nested.group(1).strip().strip('"')] = _scalar(nested.group(2))
            continue
        m2 = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
        if not m2:
            continue
        key, raw = m2.group(1), m2.group(2)
        raw = re.sub(r"\s+#.*$", "", raw).strip()  # хвостовой комментарий
        data[key] = [] if raw == "" else _scalar(raw)
    return data, body


def _scalar(raw: str):
    raw = raw.strip().strip('"').strip("'")
    if raw in ("null", "~", ""):
        return None
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip().strip('"').strip("'") for x in raw[1:-1].split(",") if x.strip()]
    return raw


def card_description(body: str) -> str:
    """Тело витринной карточки → HTML описания.

    Два формата сосуществуют, и оба надо принять:
      • новый — блоки помечены «## БЛОК about1 → …»;
      • старый (20 карточек) — тематические подзаголовки без пометок.
    В обоих случаях служебный хвост для редактора отрезается.
    """
    cut = EDITOR_ONLY.search(body)
    if cut:
        body = body[: cut.start()]

    blocks = re.split(r"^##\s*БЛОК\s+about\d+[^\n]*\n", body, flags=re.M)
    if len(blocks) > 1:
        body = "\n\n".join(b.strip() for b in blocks[1:] if b.strip())
    else:
        body = re.sub(r"^#\s+[^\n]*\n", "", body, count=1, flags=re.M)

    return md_to_html(body.strip())


def md_to_html(md: str) -> str:
    """Минимальный Markdown → HTML. Витрина принимает только простую разметку."""
    out = []
    for chunk in re.split(r"\n{2,}", md):
        chunk = chunk.strip()
        if not chunk:
            continue
        chunk = re.sub(r"\[\[ref:[^\]]+\]\]", "", chunk)
        chunk = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", chunk)
        chunk = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", chunk)
        chunk = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: (f'<a href="{m.group(2)}">{m.group(1)}</a>'
                       if m.group(2).startswith(LIVE_LINK_PREFIXES) else m.group(1)),
            chunk,
        )
        if chunk.startswith("|"):
            continue  # таблицы витрина рисует сама из характеристик
        if m := re.match(r"^#{1,6}\s+(.*)$", chunk):
            out.append(f"<h3>{m.group(1)}</h3>")
        elif re.match(r"^[-*]\s", chunk):
            items = "".join(
                f"<li>{re.sub(r'^[-*]\s+', '', ln).strip()}</li>"
                for ln in chunk.splitlines() if ln.strip()
            )
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{' '.join(chunk.split())}</p>")
    return "\n".join(out)


def offer_id(product: dict, front: dict) -> str:
    """Ключ склейки с номенклатурой.

    Для Excel-импорта uniSiter документирует ключевым атрибутом Артикул. Берём его же:
    article_1c из витринной карточки, иначе числовой хвост слага (`...-1551` → `1551`),
    который на сайте и есть идентификатор товара.
    """
    if front.get("article_1c"):
        return str(front["article_1c"])
    m = re.search(r"-(\d+)$", product.get("slug", ""))
    return m.group(1) if m else product.get("slug", "")


# --------------------------------------------------------------------------- сборка

def collect(picture_base: str | None, *, pass_through: bool = False,
            only_ours: bool = False, no_pictures: bool = False) -> list[Offer]:
    """pass_through=True — снимок сайта «как есть», наши материалы игнорируются.

    Нужен как страховка: если переключение на YML пойдёт не так, этим файлом
    восстанавливается ровно то наполнение, которое было до вмешательства.
    Поэтому здесь не должно быть ни одного нашего слова — ни описания, ни заголовка,
    ни характеристики.
    """
    offers: list[Offer] = []
    for path in sorted(PRODUCTS.glob("*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        slug = p["slug"]

        card = SITE_CARDS / f"{slug}.md"
        art = offer_id(p, {})
        if APPROVED is not None and art not in APPROVED:
            card = Path("/nonexistent")  # не одобрен — идёт копией сайта
        if only_ours and not card.exists():
            continue  # режим обогащения: в фид идут только позиции с нашим материалом

        front, body = {}, ""
        if card.exists() and not pass_through:
            front, body = parse_frontmatter(card.read_text(encoding="utf-8"))

        description = card_description(body) if body else (p.get("description_html") or "")
        avail = str(p.get("availability") or "")

        o = Offer(
            offer_id=offer_id(p, front),
            slug=slug,
            url=p.get("url") or f"{SHOP['url']}/shop/goods/{slug}",
            name=front.get("title") or p.get("name") or slug,
            price=p.get("price_rub") if p.get("price_rub") is not None else front.get("price_rub"),
            available=not re.search(r"снят|нет в наличии|под заказ", avail, re.I),
            category_path=p.get("category_path") or ["Каталог"],
            description=description,
            source="наш" if body else "1С",
        )

        # Характеристики: наши, если карточка их объявила, иначе те, что стоят на сайте.
        # Оси вариантов отфильтровываются в обоих случаях — см. VARIANT_AXES.
        for name, value in (front.get("yml_params") or {}).items() if isinstance(front.get("yml_params"), dict) else []:
            o.params.append((name, str(value)))
        if not o.params:
            for name, value in (p.get("attributes") or {}).items():
                if str(value).strip():
                    o.params.append((str(name).strip(), str(value).strip()))
        o.params = [(n, v) for n, v in o.params if n.strip().rstrip(":").lower() not in VARIANT_AXES]

        # Картинки. Те, что уже на сайте, — готовые публичные ссылки, берём как есть.
        # images в карточке товара — список {url, alt}; в YML идёт только адрес.
        o.pictures = [
            im["url"] if isinstance(im, dict) else str(im)
            for im in (p.get("images") or [])
            if (im.get("url") if isinstance(im, dict) else im)
        ]
        if picture_base and not pass_through:
            base = picture_base.rstrip("/")
            for local, suffix in (
                (PHOTO_FINAL / f"{slug}__main.jpg", f"{slug}__main.jpg"),
                (PHOTO_FINAL / f"{slug}__detail.jpg", f"{slug}__detail.jpg"),
                (LABELS / f"{slug}.png", f"{slug}__label.png"),
            ):
                if local.exists():
                    o.pictures.insert(0 if "main" in suffix else len(o.pictures), f"{base}/{suffix}")
        if no_pictures:
            # Картинки на сайте, вероятно, приходят из 1С. Пока у нас нет своего хостинга,
            # безопаснее не претендовать на это поле вовсе: очистка картинок в 1С ради
            # обогащения снесёт ровно те файлы, на которые мы же и ссылаемся.
            o.pictures = []
        o.pictures = list(dict.fromkeys(o.pictures))[:10]  # YML: не более 10 на предложение

        o.fetched_at = str(p.get("fetched_at") or "")
        validate(o)
        offers.append(o)
    return dedupe_by_id(offers)


def dedupe_by_id(offers: list[Offer]) -> list[Offer]:
    """Один артикул — одно предложение.

    Сайт время от времени переименовывает позицию, и слаг меняется при том же
    артикуле: `abrikos_vostochno_sibirskiy_-1959` → `abrikos_minusinskiy_rumyanyiy_zks-1959`,
    название и код 1С прежние. Оба файла остаются на диске, и в фид уходят два
    `<offer id="1959">` — при импорте это либо ошибка разбора, либо один товар
    молча затирает другой.

    Оставляем свежескачанный, выбывший помечаем, чтобы он попал в отчёт, а не исчез тихо.
    """
    best: dict[str, Offer] = {}
    dropped: list[Offer] = []
    for o in offers:
        cur = best.get(o.offer_id)
        if cur is None:
            best[o.offer_id] = o
        elif o.fetched_at > cur.fetched_at:
            best[o.offer_id] = o
            dropped.append(cur)
        else:
            dropped.append(o)
    for o in dropped:
        o.problems.append(f"дубль артикула {o.offer_id} — устаревший слаг, товар переименован")
    return [*best.values(), *dropped]


def validate(o: Offer) -> None:
    if o.price is None:
        if ZERO_PRICE:
            o.price = 0  # цена всё равно приходит из 1С; ноль — заглушка ради синтаксиса YML
        else:
            o.problems.append("нет цены — YML требует <price>")
    if not o.description.strip() and o.source == "наш":
        o.problems.append("пустое описание")
    # В режиме снимка пустое описание — не брак, а правда: на сайте оно тоже пустое.
    # Выбросить такой товар из фида значит лишить его картинок и характеристик
    # при переключении источника, то есть изменить сайт там, где мы обещали не менять.
    if DESCRIPTION_LIMIT and len(o.description) > DESCRIPTION_LIMIT:
        o.problems.append(f"описание {len(o.description)} зн. > лимита {DESCRIPTION_LIMIT}")
    if not o.pictures and not NO_PICTURES and o.source == "наш":
        o.problems.append("нет ни одной картинки")
    # В снимке товар без картинок воспроизводится как есть: на сайте у него их тоже нет.
    if not o.offer_id:
        o.problems.append("не удалось определить артикул")


def build_categories(offers: list[Offer]) -> dict[tuple[str, ...], int]:
    """Пути категорий → плоское дерево с числовыми id и parentId."""
    ids: dict[tuple[str, ...], int] = {}
    for o in offers:
        for depth in range(1, len(o.category_path) + 1):
            key = tuple(o.category_path[:depth])
            if key not in ids:
                ids[key] = len(ids) + 1
    return ids


def to_xml(offers: list[Offer], cats: dict[tuple[str, ...], int], date: str) -> str:
    root = ET.Element("yml_catalog", date=date)
    shop = ET.SubElement(root, "shop")
    for tag in ("name", "company", "url"):
        ET.SubElement(shop, tag).text = SHOP[tag]
    cur = ET.SubElement(shop, "currencies")
    ET.SubElement(cur, "currency", id="RUR", rate="1")

    cel = ET.SubElement(shop, "categories")
    for path, cid in cats.items():
        attrs = {"id": str(cid)}
        if len(path) > 1:
            attrs["parentId"] = str(cats[path[:-1]])
        ET.SubElement(cel, "category", **attrs).text = path[-1]

    oel = ET.SubElement(shop, "offers")
    for o in offers:
        if o.problems:
            continue  # в фид уходит только то, что прошло проверку; остальное — в отчёт
        el = ET.SubElement(oel, "offer", id=o.offer_id, available="true" if o.available else "false")
        ET.SubElement(el, "url").text = o.url
        ET.SubElement(el, "price").text = f"{o.price:g}"
        ET.SubElement(el, "currencyId").text = "RUR"
        ET.SubElement(el, "categoryId").text = str(cats[tuple(o.category_path)])
        for pic in o.pictures:
            ET.SubElement(el, "picture").text = pic
        ET.SubElement(el, "vendor").text = SHOP["name"]
        ET.SubElement(el, "name").text = o.name
        ET.SubElement(el, "description").text = o.description
        for name, value in o.params[:20]:
            ET.SubElement(el, "param", name=name).text = value

    raw = ET.tostring(root, encoding="unicode")
    body = minidom.parseString(raw).toprettyxml(indent="  ")
    body = body.split("\n", 1)[1]  # свой пролог вместо минидомовского без кодировки
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE yml_catalog SYSTEM "shops.dtd">\n'
        + body
    )


# --------------------------------------------------------------------------- отчёт

def report(offers: list[Offer]) -> None:
    ok = [o for o in offers if not o.problems]
    ours = [o for o in offers if o.source == "наш"]
    print(f"позиций разобрано:      {len(offers)}")
    print(f"  из них с нашим текстом {len(ours)}")
    print(f"  проходят насквозь      {len(offers) - len(ours)}")
    print(f"уходит в фид:           {len(ok)}")
    print(f"отсеяно проверкой:      {len(offers) - len(ok)}\n")

    buckets: dict[str, list[str]] = {}
    for o in offers:
        for pr in o.problems:
            buckets.setdefault(re.sub(r"\d+", "N", pr), []).append(o.slug)
    over = [o for o in offers if len(o.description) > YANDEX_DESCRIPTION_LIMIT]
    if over:
        print(f"  справочно: {len(over)} описаний длиннее {YANDEX_DESCRIPTION_LIMIT} зн. "
              f"(предел Яндекс.Маркета, для uniSiter не подтверждён); "
              f"самое длинное — {max(len(o.description) for o in over)} зн.\n")

    for pr, slugs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(slugs):>4}  {pr}")
        for s in slugs[:3]:
            print(f"          {s}")
        if len(slugs) > 3:
            print(f"          … ещё {len(slugs) - 3}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "feed" / "darwinshop.yml")
    ap.add_argument("--approved", type=Path, default=None,
                    help="Файл со списком артикулов, которым разрешена публикация нашего "
                         "материала. Остальные идут копией сайта")
    ap.add_argument("--zero-price-for-missing", action="store_true",
                    help="Товарам без цены проставить 0, чтобы они не выпадали из фида "
                         "(цена всё равно берётся из 1С)")
    ap.add_argument("--only-ours", action="store_true",
                    help="Только позиции с нашим материалом (режим обогащения)")
    ap.add_argument("--no-pictures", action="store_true",
                    help="Не отдавать <picture>: поле остаётся за 1С")
    ap.add_argument("--pass-through", action="store_true",
                    help="Снимок сайта «как есть»: наши витринные карточки не подмешиваются")
    ap.add_argument("--picture-base", default=None,
                    help="Публичный URL, под которым выложены наши фото и этикетки")
    ap.add_argument("--date", default=None,
                    help="Дата в атрибуте yml_catalog (по умолчанию — момент сборки)")
    ap.add_argument("--description-limit", type=int, default=None,
                    help=f"Отсекать описания длиннее N знаков "
                         f"(правило Яндекс.Маркета — {YANDEX_DESCRIPTION_LIMIT}; "
                         f"для импорта в uniSiter не подтверждено)")
    args = ap.parse_args()
    args.out = args.out.resolve()
    if not args.date:
        from datetime import datetime
        args.date = datetime.now().strftime("%Y-%m-%d %H:%M")  # путь может прийти относительным — приводим к абсолютному

    global DESCRIPTION_LIMIT
    DESCRIPTION_LIMIT = args.description_limit

    global NO_PICTURES, PASS_THROUGH, ZERO_PRICE, APPROVED
    if args.approved:
        # В строке может стоять не только артикул, но и название с ценой —
        # список читают люди. Значим только первый столбец.
        APPROVED = {
            ln.split("#")[0].split()[0]
            for ln in args.approved.read_text(encoding="utf-8").splitlines()
            if ln.split("#")[0].split()
        }
        print(f"разрешено к публикации артикулов: {len(APPROVED)}\n")
    NO_PICTURES = args.no_pictures
    PASS_THROUGH = args.pass_through
    ZERO_PRICE = args.zero_price_for_missing
    offers = collect(args.picture_base, pass_through=args.pass_through,
                     only_ours=args.only_ours, no_pictures=args.no_pictures)
    if not offers:
        print("В data/catalog/products/ нет ни одного товара — нечего собирать.", file=sys.stderr)
        return 1

    cats = build_categories(offers)
    xml = to_xml(offers, cats, args.date)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(xml, encoding="utf-8")

    if args.pass_through:
        print("режим: СНИМОК САЙТА, наши материалы не подмешивались\n")
    report(offers)
    where = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    print(f"\nфид: {where}  ({args.out.stat().st_size / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
