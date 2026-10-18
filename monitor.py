"""Мониторинг цен: следит за товарами на сайтах и пишет в Telegram, когда цена падает.

Цена ищется по CSS-селектору, который вы указываете при добавлении товара.
История цен хранится в SQLite, поэтому видно, как менялась цена.

Примеры:
    python monitor.py add "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html" \
        --selector "p.price_color" --name "A Light in the Attic" --target 40
    python monitor.py list
    python monitor.py check
    python monitor.py history 1
    python monitor.py run --every 60     # проверять каждые 60 минут
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# .env читаем до os.getenv ниже, иначе PRICE_DB из файла не подхватится
load_dotenv()
DB_PATH = os.getenv("PRICE_DB", "prices.db")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PriceMonitor/1.0)"}
log = logging.getLogger("monitor")


@dataclass
class Item:
    id: int
    url: str
    selector: str
    name: str
    target: float | None
    last_price: float | None


# ---------- разбор цены ----------

def parse_price(text: str) -> float | None:
    """Достаёт число из строки с ценой: «12 990 ₽», «£51.77», «1.299,00 €», «$1,299.99»."""
    cleaned = re.sub(r"[^\d,.\s]", "", text).strip()
    cleaned = re.sub(r"(?<=\d)\s+(?=\d)", "", cleaned)  # пробелы-разделители тысяч
    m = re.search(r"\d[\d.,]*", cleaned)
    if not m:
        return None
    num = m.group().rstrip(".,")
    if "," in num and "." in num:  # кто последний, тот десятичный разделитель
        num = num.replace(".", "").replace(",", ".") if num.rfind(",") > num.rfind(".") else num.replace(",", "")
    elif "," in num:
        whole, _, frac = num.rpartition(",")
        num = f"{whole.replace(',', '')}.{frac}" if len(frac) <= 2 else num.replace(",", "")
    elif num.count(".") > 1 or (num.count(".") == 1 and len(num.rpartition(".")[2]) == 3):
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def extract_price(html: str, selector: str) -> float | None:
    el = BeautifulSoup(html, "html.parser").select_one(selector)
    if el is None:
        return None
    return parse_price(el.get("content") or el.get_text(" ", strip=True))


def fetch_price(url: str, selector: str) -> float | None:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return extract_price(r.text, selector)


# ---------- хранилище ----------

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL,
        selector TEXT NOT NULL, name TEXT NOT NULL, target REAL, last_price REAL, added_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS history (item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        price REAL NOT NULL, checked_at TEXT NOT NULL);
    """)
    return conn


def items(conn) -> list[Item]:
    return [Item(r["id"], r["url"], r["selector"], r["name"], r["target"], r["last_price"])
            for r in conn.execute("SELECT * FROM items ORDER BY id")]


# ---------- уведомления ----------

def notify(text: str) -> None:
    token, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        log.info("Уведомление (Telegram не настроен): %s", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15).raise_for_status()
    except requests.RequestException as exc:
        log.warning("Не удалось отправить в Telegram: %s", exc)


def decide(item: Item, price: float) -> str | None:
    """Решает, о чём сообщить: цена упала или достигла желаемой."""
    if item.target is not None and price <= item.target and (item.last_price is None or item.last_price > item.target):
        return f"🎯 {item.name}: цена {price:g} — достигла вашей цели {item.target:g}\n{item.url}"
    if item.last_price is not None and price < item.last_price:
        drop = (item.last_price - price) / item.last_price * 100
        return f"📉 {item.name}: {item.last_price:g} → {price:g} (−{drop:.1f}%)\n{item.url}"
    return None


# ---------- команды ----------

def cmd_add(args) -> None:
    price = fetch_price(args.url, args.selector)
    if price is None:
        raise SystemExit(f"По селектору «{args.selector}» цена не найдена. Проверьте селектор в инструментах разработчика браузера.")
    with closing(connect()) as conn, conn:
        try:
            cur = conn.execute("INSERT INTO items (url, selector, name, target, last_price, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (args.url, args.selector, args.name or args.url[:60], args.target, price, datetime.now().isoformat(timespec="seconds")))
        except sqlite3.IntegrityError:
            raise SystemExit("Этот адрес уже отслеживается.")
        conn.execute("INSERT INTO history VALUES (?, ?, ?)", (cur.lastrowid, price, datetime.now().isoformat(timespec="seconds")))
    print(f"Добавлено, №{cur.lastrowid}. Текущая цена: {price:g}")


def cmd_list(_) -> None:
    with closing(connect()) as conn:
        rows = items(conn)
    if not rows:
        print("Список пуст. Добавьте товар: python monitor.py add URL --selector CSS")
    for it in rows:
        target = f", цель {it.target:g}" if it.target is not None else ""
        print(f"{it.id:>3}. {it.name[:50]:<50} {it.last_price if it.last_price is not None else '—':>10}{target}")


def check_all() -> int:
    alerts = 0
    with closing(connect()) as conn, conn:
        for it in items(conn):
            try:
                price = fetch_price(it.url, it.selector)
            except requests.RequestException as exc:
                log.warning("%s: сайт недоступен (%s)", it.name, exc)
                continue
            if price is None:
                log.warning("%s: цена не найдена, возможно, изменилась вёрстка", it.name)
                continue
            message = decide(it, price)
            if message:
                notify(message)
                alerts += 1
            conn.execute("UPDATE items SET last_price = ? WHERE id = ?", (price, it.id))
            conn.execute("INSERT INTO history VALUES (?, ?, ?)", (it.id, price, datetime.now().isoformat(timespec="seconds")))
            log.info("%s: %g", it.name, price)
    return alerts


def cmd_history(args) -> None:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT price, checked_at FROM history WHERE item_id = ? ORDER BY checked_at", (args.id,)).fetchall()
    if not rows:
        raise SystemExit("Нет истории для этого номера.")
    lo, hi = min(r["price"] for r in rows), max(r["price"] for r in rows)
    for r in rows:
        bar = "█" * (1 + int((r["price"] - lo) / (hi - lo or 1) * 30))
        print(f"{r['checked_at'][:16]}  {r['price']:>10g}  {bar}")


def cmd_remove(args) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM history WHERE item_id = ?", (args.id,))
        n = conn.execute("DELETE FROM items WHERE id = ?", (args.id,)).rowcount
    print("Удалено." if n else "Такого номера нет.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Мониторинг цен с уведомлениями в Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="добавить товар")
    a.add_argument("url")
    a.add_argument("--selector", required=True, help="CSS-селектор элемента с ценой")
    a.add_argument("--name")
    a.add_argument("--target", type=float, help="желаемая цена: сообщим, когда опустится до неё")
    a.set_defaults(func=cmd_add)
    sub.add_parser("list", help="список товаров").set_defaults(func=cmd_list)
    sub.add_parser("check", help="проверить цены один раз").set_defaults(func=lambda _: print(f"Уведомлений: {check_all()}"))
    h = sub.add_parser("history", help="история цен товара")
    h.add_argument("id", type=int)
    h.set_defaults(func=cmd_history)
    r = sub.add_parser("remove", help="перестать отслеживать")
    r.add_argument("id", type=int)
    r.set_defaults(func=cmd_remove)
    run = sub.add_parser("run", help="проверять по расписанию")
    run.add_argument("--every", type=int, default=60, help="интервал в минутах")
    run.set_defaults(func=lambda a: loop(a.every))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


def loop(minutes: int) -> None:
    log.info("Проверка каждые %d мин. Остановить: Ctrl+C", minutes)
    while True:
        check_all()
        time.sleep(minutes * 60)


if __name__ == "__main__":
    main()
