"""One ALGO2 simulation round using the local RIT REST API."""

import json
from time import sleep

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

URL = "http://localhost:9999/v1"
API_KEY = "YOUR API KEY HERE"  # Set this in your local copy.
TICKER = "ALGO"
MIN_TRADED = 15_000
MAX_ORDER = 5_000
MAX_POSITION = 25_000
CASE_SECONDS = 300
FLATTEN_SECONDS = 15
POLL_SECONDS = 0.25
STALE_CENTS = 2
STATUS_INTERVAL = 10


def api(session, method, path, **params):
    url = URL + path
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"X-API-Key": session}, method=method)
    while True:
        try:
            with urlopen(request, timeout=3) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code != 429:
                raise
            sleep(max(POLL_SECONDS, float(exc.headers.get("Retry-After", 0.5))))


def submit(session, tracked, action, size, price_cents=None):
    params = dict(ticker=TICKER, action=action, quantity=size, type="MARKET")
    if price_cents is not None:
        params.update(type="LIMIT", price=f"{price_cents / 100:.2f}")
    order = api(session, "POST", "/orders", **params)
    tracked.add(order["order_id"])


def read_fills(session, tracked, closed_volume):
    # Each order reports cumulative fills, including fills before cancellation.
    open_orders = []
    open_volume = 0
    for order_id in list(tracked):
        order = api(session, "GET", f"/orders/{order_id}")
        filled = order["quantity_filled"]
        if order["status"] == "OPEN":
            open_orders.append(order)
            open_volume += filled
        else:
            closed_volume += filled
            tracked.remove(order_id)
    return open_orders, closed_volume, closed_volume + open_volume


def cancel(session, orders):
    for order in orders:
        api(session, "DELETE", f"/orders/{order['order_id']}")
    # The next poll rechecks status and counts any fills during cancellation.


def book_top(session):
    book = api(session, "GET", "/securities/book", ticker=TICKER)
    if not book["bids"] or not book["asks"]:
        return None
    bid, ask = (
        round(float(book[side][0]["price"]) * 100) for side in ("bids", "asks")
    )
    return (bid, ask) if 0 < bid < ask else None


def quote(top, last, position):
    best_bid, best_ask = top
    half_spread = (best_ask - best_bid + 1) // 2
    # Shift both quotes one cent toward reducing the current position.
    skew = (position > 0) - (position < 0)
    bid = max(1, min(best_ask - 1, last - half_spread - skew))
    ask = max(bid + 1, best_bid + 1, last + half_spread - skew)

    # Check each side as if that order fills on its own.
    buy_size = max(0, min(MAX_ORDER, MAX_POSITION - position))
    sell_size = max(0, min(MAX_ORDER, MAX_POSITION + position))
    return bid, ask, buy_size, sell_size


def quotes_are_stale(orders, top):
    best_bid, best_ask = top
    for order in orders:
        price = round(float(order["price"]) * 100)
        if order["action"] == "BUY" and best_bid - price >= STALE_CENTS:
            return True
        if order["action"] == "SELL" and price - best_ask >= STALE_CENTS:
            return True
    return False


def security(session):
    return api(session, "GET", "/securities", ticker=TICKER)[0]


def run(session):
    tracked = set()
    closed_volume = traded = position = 0
    last_report = -1
    last_status_tick = -STATUS_INTERVAL
    expected_count = 2
    started = False
    flattening = False
    try:
        while True:
            case = api(session, "GET", "/case")
            if case["status"] != "ACTIVE":
                if started:
                    _, closed_volume, traded = read_fills(session, tracked, closed_volume)
                    position = security(session)["position"]
                break

            if not started:
                position = security(session)["position"]
                existing = api(session, "GET", "/orders", status="OPEN")
                if position or any(order["ticker"] == TICKER for order in existing):
                    raise RuntimeError("Start with a flat ALGO position and no working ALGO orders.")
                started = True

            remaining = CASE_SECONDS - case["tick"]
            orders, closed_volume, traded = read_fills(session, tracked, closed_volume)
            stock = security(session)
            position = stock["position"]
            if case["tick"] - last_status_tick >= STATUS_INTERVAL:
                print(
                    f"Tick {case['tick']} | Working: {len(orders)}/{expected_count} "
                    f"| Gross: {traded:,} | Position: {position:+,}",
                    flush=True,
                )
                last_status_tick = case["tick"]
            if traded != last_report:
                print(f"Gross traded: {traded:,}/{MIN_TRADED:,} | Position: {position:+,}")
                last_report = traded

            if flattening or remaining <= FLATTEN_SECONDS:
                flattening = True
                quotes = [order for order in orders if order["type"] == "LIMIT"]
                if quotes:
                    cancel(session, quotes)
                elif not orders:
                    if not position:
                        break
                    action = "SELL" if position > 0 else "BUY"
                    submit(session, tracked, action, min(abs(position), MAX_ORDER))
            elif orders:
                if len(orders) != expected_count:
                    cancel(session, orders)
                elif (top := book_top(session)) and quotes_are_stale(orders, top):
                    print(f"Tick {case['tick']} | Requote: working quote is stale.", flush=True)
                    cancel(session, orders)
            else:
                top = book_top(session)
                if top:
                    last_price = stock.get("last") or sum(top) / 200
                    last = round(float(last_price) * 100)
                    bid, ask, buy_size, sell_size = quote(top, last, position)
                    expected_count = 2 if buy_size and sell_size else 1
                    if buy_size:
                        submit(session, tracked, "BUY", buy_size, bid)
                    if sell_size:
                        fresh = book_top(session) if buy_size else top
                        if fresh and ask > fresh[0]:
                            submit(session, tracked, "SELL", sell_size, ask)
            sleep(POLL_SECONDS)
    finally:
        if tracked:
            try:
                orders, closed_volume, traded = read_fills(session, tracked, closed_volume)
                cancel(session, [order for order in orders if order["type"] == "LIMIT"])
                _, closed_volume, traded = read_fills(session, tracked, closed_volume)
                position = security(session)["position"]
            except URLError as exc:
                print(f"Could not verify final orders and fills: {exc}")
        if started:
            print(f"Final observed gross: {traded:,} | Position: {position:+,}")
            if traded >= MIN_TRADED:
                print("Minimum gross volume met.")
            else:
                print(f"Volume shortfall: {MIN_TRADED - traded:,} shares.")
            if position:
                print("Position was not confirmed flat.")
    return started and flattening and traded >= MIN_TRADED and position == 0


def main():
    if not API_KEY.strip() or API_KEY == "YOUR API KEY HERE":
        print("Set API_KEY at the top of algo2.py before running.")
        return 1
    try:
        return 0 if run(API_KEY.strip()) else 1
    except (OSError, RuntimeError, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
