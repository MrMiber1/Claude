#!/usr/bin/env python3
"""Поиск дешёвых авиабилетов через Aviasales Data API (Travelpayouts).

Токен в скрипт не передаётся: окружение (network secret) само добавляет его
в запросы к api.travelpayouts.com. Не записывайте токен в репозиторий.

Даты: день (2026-12-24), месяц (2026-12) или диапазон (2026-12-23..2026-12-25).
Аэропорты и города можно перечислять через запятую (GOI,GOX).
Одна команда перебирает все комбинации и показывает лучшие варианты по цене.

Примеры:
  python3 tools/search_flights.py MUC GOI,GOX --depart 2026-12-23..2026-12-25 --return 2027-01-07..2027-01-09 --currency eur
  python3 tools/search_flights.py MUC DXB,SHJ --depart 2026-12-23..2026-12-25 --direct
  python3 tools/search_flights.py MUC GOI --depart 2026-12-23..2026-12-25 --return 2027-01-07..2027-01-09 --legs
"""
import argparse
import datetime as dt
import itertools
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
PAUSE_SECONDS = 0.15


def date_arg(value):
    """День, месяц или диапазон дней -> список значений для API."""
    try:
        m = RANGE_RE.match(value)
        if m:
            start, end = (dt.date.fromisoformat(x) for x in m.groups())
            if end < start or (end - start).days > 30:
                raise argparse.ArgumentTypeError("в диапазоне конец не раньше начала, не больше 31 дня")
            return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        if DAY_RE.match(value):
            dt.date.fromisoformat(value)
            return [value]
        if MONTH_RE.match(value):
            dt.date.fromisoformat(value + "-01")
            return [value]
    except ValueError:
        raise argparse.ArgumentTypeError(f"некорректная дата: {value}")
    raise argparse.ArgumentTypeError("нужен формат YYYY-MM-DD, YYYY-MM или диапазон YYYY-MM-DD..YYYY-MM-DD")


def codes_arg(value):
    codes = [c.strip().upper() for c in value.split(",") if c.strip()]
    if not codes:
        raise argparse.ArgumentTypeError("нужен хотя бы один IATA-код")
    return codes


def parse_args(argv):
    p = argparse.ArgumentParser(description="Поиск дешёвых авиабилетов (Aviasales Data API)")
    p.add_argument("origin", type=codes_arg, help="IATA-коды вылета через запятую, например MUC или MOW,LED")
    p.add_argument("destination", type=codes_arg, help="IATA-коды прилёта через запятую, например GOI,GOX")
    p.add_argument("--depart", type=date_arg, help="дата вылета: день, месяц или диапазон")
    p.add_argument("--return", dest="ret", type=date_arg,
                   help="дата обратного вылета: день, месяц или диапазон; без неё ищем в одну сторону")
    p.add_argument("--legs", action="store_true",
                   help="искать плечи отдельно: вылет туда и вылет обратно как два билета в одну сторону")
    p.add_argument("--currency", default="rub", help="валюта цен, по умолчанию rub")
    p.add_argument("--direct", action="store_true", help="только рейсы без пересадок")
    p.add_argument("--max-transfers", type=int, help="не больше N пересадок на каждом плече")
    p.add_argument("--limit", type=int, default=10, help="сколько лучших вариантов показать, по умолчанию 10")
    p.add_argument("--max-requests", type=int, default=40,
                   help="предохранитель: максимум запросов к API за один запуск, по умолчанию 40")
    p.add_argument("--site", default="https://www.aviasales.ru",
                   help="сайт, к которому добавляется относительная ссылка из ответа")
    p.add_argument("--json", action="store_true", help="вывести сырой JSON вместо текста")
    args = p.parse_args(argv)
    if args.legs and not (args.depart and args.ret):
        p.error("--legs требует и --depart, и --return")
    return args


def build_params(origin, destination, depart, ret, args):
    params = {
        "origin": origin,
        "destination": destination,
        "currency": args.currency.lower(),
        "limit": 30,
        "sorting": "price",
        "one_way": "false" if ret else "true",
    }
    if depart:
        params["departure_at"] = depart
    if ret:
        params["return_at"] = ret
    if args.direct:
        params["direct"] = "true"
    return params


def fetch(params):
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "flight-search-agent/1.0"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 401:
                raise SystemExit(
                    "Ошибка 401: API не принял токен. Проверьте секрет TRAVELPAYOUTS_TOKEN в "
                    "настройках облачного окружения (заголовок X-Access-Token, пустой префикс) "
                    "и запустите новую сессию."
                )
            raise SystemExit(f"Ошибка API {e.code}: {body}")
        except urllib.error.URLError as e:
            raise SystemExit(
                f"Нет связи с api.travelpayouts.com: {e.reason}. "
                "Проверьте доступ к сети в настройках окружения."
            )


def search(origins, destinations, departs, returns, args):
    """Перебирает все комбинации и возвращает (варианты по цене, статистика)."""
    combos = list(itertools.product(origins, destinations, departs or [None], returns or [None]))
    if len(combos) > args.max_requests:
        raise SystemExit(
            f"Слишком много запросов: {len(combos)} (лимит {args.max_requests}). "
            "Сузьте диапазоны дат или список аэропортов, либо увеличьте --max-requests."
        )
    items, seen, hits, currency = [], set(), 0, args.currency
    for i, (o, d, dep, ret) in enumerate(combos):
        if i:
            time.sleep(PAUSE_SECONDS)
        data = fetch(build_params(o, d, dep, ret, args))
        found = data.get("data") or []
        currency = data.get("currency") or currency
        hits += bool(found)
        for t in found:
            if args.max_transfers is not None and (
                (t.get("transfers") or 0) > args.max_transfers
                or (t.get("return_transfers") or 0) > args.max_transfers
            ):
                continue
            key = t.get("link") or json.dumps(t, sort_keys=True)
            if key not in seen:
                seen.add(key)
                items.append(t)
    items.sort(key=lambda t: t.get("price") or 0)
    return items, {"requests": len(combos), "with_results": hits, "found": len(items), "currency": currency}


def fmt_minutes(minutes):
    if not minutes:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}ч {m:02d}м"


def fmt_time(value):
    """2026-11-01T17:10:00+03:00 -> 2026-11-01 17:10 (+03:00)."""
    if not value:
        return "—"
    zone = value[19:] if len(value) > 19 else ""
    return value[:16].replace("T", " ") + (f" ({zone})" if zone else "")


def utc_span(t):
    """UTC-время вылета и прилёта для прямого рейса в одну сторону (нужно для расчёта стыковок)."""
    if t.get("return_at") or t.get("transfers") or not t.get("duration_to") or not t.get("departure_at"):
        return None
    try:
        dep = dt.datetime.fromisoformat(t["departure_at"]).astimezone(dt.timezone.utc)
    except ValueError:
        return None
    arr = dep + dt.timedelta(minutes=int(t["duration_to"]))
    return f"UTC: вылет {dep:%Y-%m-%d %H:%M} → прилёт {arr:%Y-%m-%d %H:%M}"


def time_summary(t):
    """Время в воздухе, общее время в пути и оценка времени на пересадки.

    По наблюдениям: duration_to / duration_back это время в воздухе без пересадок,
    а duration это всё время в пути с пересадками (для билета туда-обратно сразу по обоим направлениям).
    """
    total, to, back = t.get("duration"), t.get("duration_to"), t.get("duration_back")
    if t.get("return_at"):
        air = (to or 0) + (back or 0)
        text = f"в воздухе {fmt_minutes(to)} туда / {fmt_minutes(back)} обратно"
    else:
        air = to or total or 0
        text = f"в воздухе {fmt_minutes(air)}"
    has_stops = (t.get("transfers") or 0) or (t.get("return_transfers") or 0)
    if has_stops and total and total > air:
        text += f"; всего в пути с пересадками {fmt_minutes(total)}; на пересадки ≈ {fmt_minutes(total - air)}"
        if t.get("return_at"):
            text += " (сумма по обоим направлениям)"
    return text


def render_item(i, t, site, cur):
    src = t.get("origin_airport") or t.get("origin")
    dst = t.get("destination_airport") or t.get("destination")
    flight = f"{t.get('airline', '')} {t.get('flight_number', '')}".strip() or "—"
    if t.get("return_at"):
        stops = f"туда {t.get('transfers', '—')} / обратно {t.get('return_transfers', '—')}"
    else:
        stops = str(t.get("transfers", "—"))
    print(f"{i}. {t.get('price')} {cur} | вылет {fmt_time(t.get('departure_at'))} | {src} → {dst}"
          f" | рейс {flight} | пересадок: {stops}")
    if t.get("return_at"):
        print(f"   обратно {fmt_time(t['return_at'])}")
    print(f"   {time_summary(t)}")
    span = utc_span(t)
    if span:
        print(f"   {span}")
    link = t.get("link") or ""
    if link:
        print(f"   ссылка: {link if link.startswith('http') else site + link}")
    if t.get("gate"):
        print(f"   источник цены: {t['gate']}")


def render(items, stats, args, title=None):
    if title:
        print(f"=== {title} ===")
    print(f"Проверено запросов: {stats['requests']}, с результатами: {stats['with_results']}, "
          f"найдено вариантов: {stats['found']}.")
    if not items:
        print("Ничего не найдено: в кэше Aviasales нет подходящих цен по этим параметрам.\n")
        return
    cur = (stats["currency"] or args.currency).upper()
    for i, t in enumerate(items[:args.limit], 1):
        render_item(i, t, args.site, cur)
    print()


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.legs:
        out_items, out_stats = search(args.origin, args.destination, args.depart, [None], args)
        back_items, back_stats = search(args.destination, args.origin, args.ret, [None], args)
        if args.json:
            print(json.dumps({"outbound": {"stats": out_stats, "data": out_items[:args.limit]},
                              "inbound": {"stats": back_stats, "data": back_items[:args.limit]}},
                             ensure_ascii=False, indent=2))
            return
        render(out_items, out_stats, args, "Туда")
        render(back_items, back_stats, args, "Обратно")
        if out_items and back_items:
            total = out_items[0]["price"] + back_items[0]["price"]
            print(f"Самая дешёвая пара плеч (две отдельные брони): {total} {out_stats['currency'].upper()}")
    else:
        items, stats = search(args.origin, args.destination, args.depart, args.ret, args)
        if args.json:
            print(json.dumps({"stats": stats, "data": items[:args.limit]}, ensure_ascii=False, indent=2))
            return
        render(items, stats, args)
    print("Цены из кэша Aviasales (поиски пользователей за последние дни): "
          "перед покупкой проверьте актуальную цену по ссылке.")


if __name__ == "__main__":
    main()
