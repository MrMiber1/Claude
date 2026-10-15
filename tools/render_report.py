#!/usr/bin/env python3
"""Собирает страницу сравнения билетов из выдачи `tools/search_ignav.py --json`.

Страница самодостаточна (HTML, CSS и JS в одном файле): карточки с полосами «перелёт и пересадки»
в общем масштабе, сортировка, фильтры, таблица и сравнение до трёх вариантов. Данные встраиваются
в файл, поэтому страницу можно открыть где угодно или опубликовать как артефакт.

Пример:
  python3 tools/search_ignav.py MUC GOI --depart 2026-12-23..2026-12-25 --return 2027-01-07..2027-01-09 \\
      --adults 2 --children 2 --max-stops 2 --limit 40 --max-requests 9 --json > goi.json
  python3 tools/render_report.py report.html goi.json --title "Билеты в Гоа" --eyebrow "MUC → GOI" \\
      --subtitle "2 взрослых + 2 ребёнка" --passengers 4 [--links links.json]

links.json: {"<ignav_id>": [{"provider": ..., "url": ..., "price": ..., "type": ...}]}; ссылки получают
командой `search_ignav.py --links ID --json` (платный запрос на каждый вариант).
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

TEMPLATE = pathlib.Path(__file__).with_name("report_template.html")


def parse_utc(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def night_minutes(arrive, depart):
    """Сколько минут пересадки приходится на ночь (23:00-06:00 по местному времени)."""
    total, day = 0, arrive.date() - dt.timedelta(days=1)
    while day <= depart.date():
        start = dt.datetime.combine(day, dt.time(23, 0))
        end = start + dt.timedelta(hours=7)
        total += max(0, (min(depart, end) - max(arrive, start)).total_seconds() / 60)
        day += dt.timedelta(days=1)
    return int(total)


def compact_leg(leg):
    segs = leg.get("segments") or []
    out = {"minutes": leg.get("duration_minutes") or 0, "carrier": leg.get("carrier"), "segments": [], "layovers": []}
    for s in segs:
        out["segments"].append({
            "from": s["departure_airport"], "to": s["arrival_airport"],
            "dep": s["departure_time_local"][:16], "arr": s["arrival_time_local"][:16],
            "min": s["duration_minutes"],
            "flight": f"{s.get('marketing_carrier_code') or ''}{s.get('flight_number') or ''}",
            "airline": s.get("operating_carrier_name"),
        })
    for a, b in zip(segs, segs[1:]):
        minutes = int((parse_utc(b["departure_time_utc"]) - parse_utc(a["arrival_time_utc"])).total_seconds() / 60)
        arr = dt.datetime.fromisoformat(a["arrival_time_local"][:16])
        dep = dt.datetime.fromisoformat(b["departure_time_local"][:16])
        out["layovers"].append({"airport": a["arrival_airport"], "min": minutes,
                                "from": a["arrival_time_local"][:16], "to": b["departure_time_local"][:16],
                                "night": night_minutes(arr, dep)})
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Страница сравнения билетов из JSON-выдачи search_ignav.py")
    p.add_argument("out", help="куда записать HTML")
    p.add_argument("inputs", nargs="+", help="один или несколько JSON-файлов из search_ignav.py --json")
    p.add_argument("--title", default="Билеты")
    p.add_argument("--eyebrow", default="")
    p.add_argument("--subtitle", default="")
    p.add_argument("--passengers", type=int, required=True, help="сколько пассажиров в поиске (цена указана за всех)")
    p.add_argument("--links", help="JSON со ссылками на покупку: {ignav_id: [...]}")
    p.add_argument("--note", action="append", default=[], help="примечание внизу страницы (можно несколько раз)")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    links = json.load(open(args.links, encoding="utf-8")) if args.links else {}
    items, seen, observed, currency = [], set(), [], "EUR"
    for path in args.inputs:
        data = json.load(open(path, encoding="utf-8"))
        if data.get("stats", {}).get("oldest_data"):
            observed.append(data["stats"]["oldest_data"])
        for it in data.get("data", []):
            if it["ignav_id"] in seen:
                continue
            seen.add(it["ignav_id"])
            currency = it["price"]["currency"]
            items.append({"id": it["ignav_id"], "price": it["price"]["amount"],
                          "outbound": compact_leg(it["outbound"]),
                          "inbound": compact_leg(it["inbound"]) if it.get("inbound") else None,
                          "self": it.get("requires_self_transfer"), "bags": it.get("bags"),
                          "links": links.get(it["ignav_id"], [])})
    if not items:
        raise SystemExit("В файлах нет вариантов: проверьте входные JSON.")
    items.sort(key=lambda i: i["price"])
    notes = args.note or [
        "Цена указана за всех пассажиров поиска, детские тарифы учтены. Данные о багаже есть не у всех вариантов.",
        "Время пересадок и местное время взяты из расписания. Ссылка на покупку не подтверждает наличие мест и цену.",
    ]
    payload = {"title": args.title, "eyebrow": args.eyebrow, "subtitle": args.subtitle, "passengers": args.passengers,
               "currency": currency, "observed_at": min(observed) if observed else dt.datetime.now(dt.timezone.utc).isoformat(),
               "items": items, "notes": notes}
    blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", blob)
    html = html.replace("<title>Билеты в Гоа</title>", f"<title>{args.title}</title>")
    pathlib.Path(args.out).write_text(html, encoding="utf-8")
    print(f"Страница записана: {args.out} ({len(items)} вариантов, {len(html) // 1024} КБ)")


if __name__ == "__main__":
    main()
