#!/usr/bin/env python3
"""Живой поиск авиабилетов через Ignav API (https://ignav.com/docs).

Ключ в скрипт не передаётся: окружение (network secret IGNAV_API_KEY, заголовок X-Api-Key)
само добавляет его в запросы к ignav.com. Не записывайте ключ в репозиторий.

Цена в ответе указана за ВСЕХ пассажиров поиска сразу (проверено: 1 взрослый и 2 взрослых + 2 ребёнка
дают разные суммы, детский тариф учтён). Каждый успешный запрос платный: 1000 разовых бесплатных,
затем $2 за 1000. Ошибки не оплачиваются. Поэтому есть предохранитель --max-requests.

Примеры:
  python3 tools/search_ignav.py MUC GOI --depart 2026-12-24 --adults 2 --children 2 --max-stops 2
  python3 tools/search_ignav.py MUC GOI,GOX --depart 2026-12-23..2026-12-25 --return 2027-01-07..2027-01-09 --adults 2 --children 2 --max-requests 18
  python3 tools/search_ignav.py --links IGNAV_ID
"""
import argparse
import datetime as dt
import itertools
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://ignav.com/api"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
RETRY_STATUSES = {424, 502, 503, 504}


def date_arg(value):
    """День или диапазон дней -> список дат (API принимает только конкретные дни)."""
    try:
        m = RANGE_RE.match(value)
        if m:
            start, end = (dt.date.fromisoformat(x) for x in m.groups())
            if end < start or (end - start).days > 14:
                raise argparse.ArgumentTypeError("в диапазоне конец не раньше начала, не больше 15 дней")
            return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        if DAY_RE.match(value):
            dt.date.fromisoformat(value)
            return [value]
    except ValueError:
        raise argparse.ArgumentTypeError(f"некорректная дата: {value}")
    raise argparse.ArgumentTypeError("нужен формат YYYY-MM-DD или диапазон YYYY-MM-DD..YYYY-MM-DD")


def codes_arg(value):
    codes = [c.strip().upper() for c in value.split(",") if c.strip()]
    if not codes:
        raise argparse.ArgumentTypeError("нужен хотя бы один IATA-код")
    return codes


def parse_args(argv):
    p = argparse.ArgumentParser(description="Живой поиск билетов (Ignav API)")
    p.add_argument("origin", nargs="?", type=codes_arg, help="IATA-коды вылета через запятую, например MUC")
    p.add_argument("destination", nargs="?", type=codes_arg, help="IATA-коды прилёта через запятую, например GOI,GOX")
    p.add_argument("--depart", type=date_arg, help="дата вылета: день или диапазон YYYY-MM-DD..YYYY-MM-DD")
    p.add_argument("--return", dest="ret", type=date_arg,
                   help="дата обратного вылета: день или диапазон; без неё поиск в одну сторону")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument("--infants-in-seat", type=int, default=0)
    p.add_argument("--infants-on-lap", type=int, default=0)
    p.add_argument("--max-stops", type=int, help="не больше N пересадок на каждом плече")
    p.add_argument("--cabin", default="economy", choices=["economy", "premium_economy", "business", "first"])
    p.add_argument("--market", default="DE", help="рынок продажи (определяет валюту), по умолчанию DE (евро)")
    p.add_argument("--max-price", type=int, help="максимальная цена за всех пассажиров")
    p.add_argument("--min-checked-bags", type=int, help="минимум мест багажа на человека")
    p.add_argument("--no-self-transfer", action="store_true",
                   help="не показывать варианты из отдельных билетов (фильтр приблизительный)")
    p.add_argument("--limit", type=int, default=8, help="сколько лучших вариантов показать, по умолчанию 8")
    p.add_argument("--max-requests", type=int, default=12,
                   help="предохранитель: максимум платных запросов за запуск, по умолчанию 12")
    p.add_argument("--links", metavar="IGNAV_ID", help="получить ссылки на покупку для выбранного варианта (ignav_id)")
    p.add_argument("--json", action="store_true", help="вывести сырой JSON")
    args = p.parse_args(argv)
    if not args.links and not (args.origin and args.destination and args.depart):
        p.error("нужны origin, destination и --depart (или --links IGNAV_ID)")
    return args


def passengers(args):
    return args.adults + args.children + args.infants_in_seat + args.infants_on_lap


def post(path, body):
    """POST с повторами на временные ошибки. Возвращает (данные, None) или (None, сообщение)."""
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "flight-search-agent/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp), None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:400]
            if e.code in RETRY_STATUSES and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code == 401:
                raise SystemExit("Ошибка 401: Ignav не принял ключ. Проверьте секрет IGNAV_API_KEY "
                                 "(хост ignav.com, заголовок X-Api-Key, пустой префикс) и запустите новую сессию.")
            if e.code == 402:
                raise SystemExit("Ошибка 402: нужна оплата. Бесплатный лимит исчерпан или аккаунт не подтверждён: "
                                 "откройте https://ignav.com/dashboard и добавьте оплату.")
            if e.code == 429:
                raise SystemExit(f"Ошибка 429: достигнут месячный лимит расходов Ignav, повторять не нужно. {text}")
            return None, f"HTTP {e.code}: {text}"
        except urllib.error.URLError as e:
            raise SystemExit(f"Нет связи с ignav.com: {e.reason}. Проверьте доступ к сети в настройках окружения.")
    return None, "не удалось получить ответ"


def parse_utc(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt_minutes(minutes):
    if minutes is None:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}ч {m:02d}м"


def night_minutes(arrive, depart):
    """Сколько минут пересадки приходится на ночь (23:00–06:00 по местному времени)."""
    total = 0
    day = arrive.date() - dt.timedelta(days=1)
    while day <= depart.date():
        start = dt.datetime.combine(day, dt.time(23, 0))
        end = start + dt.timedelta(hours=7)
        overlap = (min(depart, end) - max(arrive, start)).total_seconds() / 60
        total += max(0, overlap)
        day += dt.timedelta(days=1)
    return int(total)


def describe_leg(leg):
    segs = leg.get("segments") or []
    if not segs:
        return "нет данных о сегментах"
    route = " → ".join([segs[0]["departure_airport"]] + [s["arrival_airport"] for s in segs])
    flights = ", ".join(f"{s.get('marketing_carrier_code') or ''}{s.get('flight_number') or ''}" for s in segs)
    layovers = []
    for a, b in zip(segs, segs[1:]):
        try:
            minutes = (parse_utc(b["departure_time_utc"]) - parse_utc(a["arrival_time_utc"])).total_seconds() / 60
            arr = dt.datetime.fromisoformat(a["arrival_time_local"][:16])
            dep = dt.datetime.fromisoformat(b["departure_time_local"][:16])
            night = night_minutes(arr, dep)
            night_text = f"ночью ≈{fmt_minutes(night)}" if night >= 60 else "ночью нет"
            layovers.append(f"{a['arrival_airport']} {fmt_minutes(minutes)} "
                            f"(местное время {arr:%d.%m %H:%M} → {dep:%d.%m %H:%M}, {night_text})")
        except (KeyError, TypeError, ValueError):
            layovers.append(f"{a['arrival_airport']} ?")
    line = (f"{route} | вылет {segs[0]['departure_time_local'][:16].replace('T', ' ')}"
            f" | прилёт {segs[-1]['arrival_time_local'][:16].replace('T', ' ')}"
            f" | в пути {fmt_minutes(leg.get('duration_minutes'))} | рейсы {flights}")
    return line + (f"\n              пересадки: {', '.join(layovers)}" if layovers else "\n              без пересадок")


def fmt_bags(bags):
    if not bags:
        return "багаж: нет данных"
    return f"ручная кладь: {bags.get('carry_on', 'нет данных')}, багаж: {bags.get('checked', 'нет данных')}"


def search(args):
    origins, dests = args.origin, args.destination
    combos = list(itertools.product(origins, dests, args.depart, args.ret or [None]))
    if len(combos) > args.max_requests:
        raise SystemExit(
            f"Слишком много платных запросов: {len(combos)} (лимит {args.max_requests}). "
            "Сузьте диапазоны дат или список аэропортов, либо увеличьте --max-requests."
        )
    items, failures, cache_hits, observed = [], [], 0, []
    for o, d, dep, ret in combos:
        body = {"origin": o, "destination": d, "departure_date": dep, "adults": args.adults,
                "children": args.children, "infants_in_seat": args.infants_in_seat,
                "infants_on_lap": args.infants_on_lap, "cabin_class": args.cabin,
                "market": args.market, "allow_self_transfer": not args.no_self_transfer}
        for key, value in (("max_stops", args.max_stops), ("max_price", args.max_price),
                           ("min_checked_bags", args.min_checked_bags)):
            if value is not None:
                body[key] = value
        if ret:
            body["return_date"] = ret
        data, error = post("/fares/round-trip" if ret else "/fares/one-way", body)
        if error:
            failures.append(f"{o}->{d} {dep}{' / ' + ret if ret else ''}: {error}")
            continue
        cache_hits += bool(data.get("cache_hit"))
        if data.get("observed_at"):
            observed.append(data["observed_at"])
        for it in data.get("itineraries") or []:
            it["_search"] = {"origin": o, "destination": d, "departure_date": dep, "return_date": ret}
            items.append(it)
    items.sort(key=lambda it: it["price"]["amount"])
    stats = {"requests": len(combos), "failed": len(failures), "cache_hits": cache_hits,
             "found": len(items), "oldest_data": min(observed) if observed else None}
    return items, failures, stats


def booking_links(args):
    # По ignav_id API не принимает поля пассажиров и рынка: они уже заложены в найденный вариант.
    data, error = post("/fares/booking-links", {"ignav_id": args.links})
    if error:
        raise SystemExit(f"Ссылки получить не удалось: {error}")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    options = data.get("booking_options") or []
    if not options:
        print("Готовых ссылок для этого варианта нет. Цену и наличие мест проверяйте на сайте авиакомпании.")
    for opt in options:
        print(f"Покрывает: {', '.join(opt.get('legs') or [])}")
        for link in opt.get("links") or []:
            price = link.get("price")
            price_text = f" | {price['amount']} {price['currency']}" if price else ""
            print(f"  - {link['provider_name']} ({link.get('provider_type') or '—'}"
                  f"{', ' + link['fare_name'] if link.get('fare_name') else ''}){price_text}"
                  f" | {link.get('specificity') or 'тип ссылки не указан'}\n    {link['url']}")
    print("\nСсылка не подтверждает наличие мест и цену: проверьте их у продавца перед оплатой.")


def render(items, failures, stats, args):
    n = passengers(args)
    print(f"Запросов к Ignav: {stats['requests']} (ошибок: {stats['failed']}, из кэша: {stats['cache_hits']}); "
          f"найдено вариантов: {stats['found']}. Данные получены: {stats['oldest_data'] or 'время неизвестно'}.")
    for f in failures:
        print(f"  ! {f}")
    if not items:
        print("Ничего не найдено по этим параметрам.")
        return
    for i, it in enumerate(items[:args.limit], 1):
        price = it["price"]
        s = it["_search"]
        dates = s["departure_date"] + (f" → {s['return_date']}" if s["return_date"] else "")
        print(f"\n{i}. {price['amount']:.0f} {price['currency']} за всех ({n} чел., ≈{price['amount'] / n:.0f} на человека)"
              f" | {dates} | цена: {price['status']}")
        print(f"   туда:    {describe_leg(it['outbound'])}")
        if it.get("inbound"):
            print(f"   обратно: {describe_leg(it['inbound'])}")
        transfer = it.get("requires_self_transfer")
        print(f"   {fmt_bags(it.get('bags'))} | отдельные билеты (самостоятельная пересадка): "
              f"{'да' if transfer else 'нет' if transfer is False else 'нет данных'}")
        print(f"   ignav_id: {it['ignav_id']}")
    print("\nЦена указана за всех пассажиров. Ссылка на покупку: "
          "python3 tools/search_ignav.py --links IGNAV_ID (один платный запрос)")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.links:
        booking_links(args)
        return
    items, failures, stats = search(args)
    if stats["failed"] == stats["requests"]:
        raise SystemExit("Все запросы завершились ошибкой:\n  " + "\n  ".join(failures))
    if args.json:
        for it in items:
            it.pop("_search", None)
        print(json.dumps({"stats": stats, "failures": failures, "data": items[:args.limit]}, ensure_ascii=False, indent=2))
        return
    render(items, failures, stats, args)


if __name__ == "__main__":
    main()
