#!/usr/bin/env python3
"""Поиск дешёвых авиабилетов через Aviasales Data API (Travelpayouts).

Токен в скрипт не передаётся: окружение (network secret) само добавляет его
в запросы к api.travelpayouts.com. Не записывайте токен в репозиторий.

Примеры:
  python3 tools/search_flights.py MOW LED --depart 2026-11
  python3 tools/search_flights.py MOW IST --depart 2026-12-20 --return 2026-12-27 --direct
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def date_arg(value):
    """Принимает YYYY-MM-DD (день) или YYYY-MM (месяц)."""
    if not DATE_RE.match(value):
        raise argparse.ArgumentTypeError("нужен формат YYYY-MM-DD или YYYY-MM")
    return value


def parse_args(argv):
    p = argparse.ArgumentParser(description="Поиск дешёвых авиабилетов (Aviasales Data API)")
    p.add_argument("origin", help="IATA-код города или аэропорта вылета, например MOW")
    p.add_argument("destination", help="IATA-код города или аэропорта прилёта, например LED")
    p.add_argument("--depart", type=date_arg, help="дата вылета: YYYY-MM-DD или месяц YYYY-MM")
    p.add_argument("--return", dest="ret", type=date_arg,
                   help="дата обратного вылета (YYYY-MM-DD или YYYY-MM); без неё ищем в одну сторону")
    p.add_argument("--currency", default="rub", help="валюта цен, по умолчанию rub")
    p.add_argument("--direct", action="store_true", help="только рейсы без пересадок")
    p.add_argument("--limit", type=int, default=10, help="сколько вариантов показать, по умолчанию 10")
    p.add_argument("--sorting", choices=["price", "route"], default="price",
                   help="сортировка: price (по цене) или route (по популярности маршрута)")
    p.add_argument("--site", default="https://www.aviasales.ru",
                   help="сайт, к которому добавляется относительная ссылка из ответа")
    p.add_argument("--json", action="store_true", help="вывести сырой JSON вместо таблицы")
    return p.parse_args(argv)


def build_params(args):
    params = {
        "origin": args.origin.upper(),
        "destination": args.destination.upper(),
        "currency": args.currency.lower(),
        "limit": args.limit,
        "sorting": args.sorting,
        "one_way": "false" if args.ret else "true",
    }
    if args.depart:
        params["departure_at"] = args.depart
    if args.ret:
        params["return_at"] = args.ret
    if args.direct:
        params["direct"] = "true"
    return params


def fetch(params):
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "flight-search-agent/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
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


def fmt_duration(minutes):
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


def render(data, site, currency):
    items = data.get("data") or []
    if not items:
        print("Ничего не найдено: в кэше Aviasales нет подходящих цен по этим параметрам.")
        return
    cur = (data.get("currency") or currency).upper()
    for i, t in enumerate(items, 1):
        src = t.get("origin_airport") or t.get("origin")
        dst = t.get("destination_airport") or t.get("destination")
        flight = f"{t.get('airline', '')} {t.get('flight_number', '')}".strip() or "—"
        print(f"{i}. {t.get('price')} {cur} | вылет {fmt_time(t.get('departure_at'))} | {src} → {dst}"
              f" | рейс {flight} | пересадок: {t.get('transfers', '—')}"
              f" | в пути {fmt_duration(t.get('duration_to') or t.get('duration'))}")
        if t.get("return_at"):
            print(f"   обратно {fmt_time(t['return_at'])} | пересадок: {t.get('return_transfers', '—')}"
                  f" | в пути {fmt_duration(t.get('duration_back'))}")
        link = t.get("link") or ""
        if link:
            print(f"   ссылка: {link if link.startswith('http') else site + link}")
        if t.get("gate"):
            print(f"   источник цены: {t['gate']}")
    print("\nЦены из кэша Aviasales (поиски пользователей за последние дни): "
          "перед покупкой проверьте актуальную цену по ссылке.")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    data = fetch(build_params(args))
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif not data.get("success", True):
        raise SystemExit(f"API вернул ошибку: {data}")
    else:
        render(data, args.site, args.currency)


if __name__ == "__main__":
    main()
