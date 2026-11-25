"""Basic market making for one Rotman RIT ALGO2 simulation round."""

import json
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

URL = "http://localhost:9999/v1"
API_KEY = "YOUR API KEY HERE"
TICKER = "ALGO"
MAX_ORDER = 5_000
MAX_POSITION = 25_000
TARGET_VOLUME = 15_000
MIN_SPREAD_CENTS = 5
CASE_TICKS = 300
FLATTEN_TICK = CASE_TICKS - 15
POLL_SECONDS = 0.5
ORDER_INTERVAL = 1.0


class RateLimited(Exception):
    pass


def api(method, path, **params):
    query = "?" + urlencode(params) if params else ""
    request = Request(
        URL + path + query, method=method, headers={"X-API-Key": API_KEY.strip()}
    )
    try:
        with urlopen(request, timeout=3) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 429:
            try:
                delay = float(exc.headers.get("Retry-After", "1"))
            except ValueError:
                delay = 1
            raise RateLimited(max(ORDER_INTERVAL, delay)) from exc
        body = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}{query}: HTTP {exc.code} {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path}{query}: {exc.reason}") from exc


def open_orders():
    return [
        order for order in api("GET", "/orders", status="OPEN")
        if order["ticker"] == TICKER
    ]


def position():
    return int(api("GET", "/securities", ticker=TICKER)[0]["position"])


def update_fills(fills, working):
    closed = set()
    if fills:
        for status in ("TRANSACTED", "CANCELLED"):
            for order in api("GET", "/orders", status=status):
                order_id = order["order_id"]
                if order_id in fills:
                    fills[order_id] = max(fills[order_id], int(order["quantity_filled"]))
                    closed.add(order_id)
    for order in working:
        order_id = order["order_id"]
        if order_id in fills:
            fills[order_id] = max(fills[order_id], int(order["quantity_filled"]))
    # A missing order can be transitioning between list responses.
    return fills.keys() - closed - {order["order_id"] for order in working}


def cancel(orders):
    ids = ",".join(str(order["order_id"]) for order in orders)
    api("POST", "/commands/cancel", ids=ids)


def submit(fills, action, quantity, price_cents=None):
    params = dict(ticker=TICKER, action=action, quantity=quantity, type="MARKET")
    if price_cents is not None:
        params.update(type="LIMIT", price=f"{price_cents / 100:.2f}")
    order = api("POST", "/orders", **params)
    fills[order["order_id"]] = int(order["quantity_filled"])


def cents(price):
    return round(float(price) * 100)


def quote(fills, working, inventory):
    book = api("GET", "/securities/book", ticker=TICKER)
    if not book["bids"] or not book["asks"]:
        return False

    bid = cents(book["bids"][0]["price"])
    ask = cents(book["asks"][0]["price"])
    stale = [
        order for order in working
        if (order["action"] == "BUY" and cents(order["price"]) < bid)
        or (order["action"] == "SELL" and cents(order["price"]) > ask)
    ]
    if stale:
        cancel(stale)
        return False

    if ask - bid < MIN_SPREAD_CENTS:
        return False

    sides = [
        ("BUY", bid, MAX_POSITION - inventory),
        ("SELL", ask, MAX_POSITION + inventory),
    ]
    if inventory > 0:
        sides.reverse()
    working_sides = {order["action"] for order in working}
    for action, price, capacity in sides:
        if action not in working_sides and capacity > 0:
            submit(fills, action, min(MAX_ORDER, capacity), price)
            return True
    return False


def run():
    if api("GET", "/case")["status"] != "ACTIVE":
        raise RuntimeError("Start the ALGO2 simulation in RIT first.")
    if position() or open_orders():
        raise RuntimeError("Start with a flat ALGO position and no working ALGO orders.")

    fills = {}
    last_report = None
    next_order_at = 0.0
    try:
        while True:
            try:
                case = api("GET", "/case")
                if case["status"] != "ACTIVE" or case["tick"] >= CASE_TICKS:
                    break

                working = open_orders()
                pending = update_fills(fills, working)
                inventory = position()
                report = (sum(fills.values()), inventory)
                if report != last_report:
                    print(
                        f"Tick {case['tick']} | Gross: {report[0]:,}/{TARGET_VOLUME:,} "
                        f"| Position: {inventory:+,}", flush=True
                    )
                    last_report = report

                if case["tick"] >= FLATTEN_TICK:
                    quotes = [order for order in working if order["type"] == "LIMIT"]
                    if quotes:
                        cancel(quotes)
                    elif not working and not pending:
                        if not inventory:
                            break
                        if monotonic() >= next_order_at:
                            action = "SELL" if inventory > 0 else "BUY"
                            submit(fills, action, min(MAX_ORDER, abs(inventory)))
                            next_order_at = monotonic() + ORDER_INTERVAL
                elif not pending and monotonic() >= next_order_at:
                    if quote(fills, working, inventory):
                        next_order_at = monotonic() + ORDER_INTERVAL
            except RateLimited as exc:
                print(f"RIT rate limit. Waiting {exc.args[0]:g} seconds.", flush=True)
                sleep(exc.args[0])
                continue
            sleep(POLL_SECONDS)
    finally:
        try:
            working = open_orders()
            our_orders = [order for order in working if order["order_id"] in fills]
            if our_orders:
                cancel(our_orders)
                working = open_orders()
            pending = update_fills(fills, working)
            inventory = position()
            gross = sum(fills.values())
            print(
                f"Final observed gross: {gross:,}/{TARGET_VOLUME:,} "
                f"| Position: {inventory:+,} | Working ALGO orders: {len(working)}"
            )
            if pending:
                print(f"Orders awaiting final status: {sorted(pending)}")
            if inventory or working:
                print("Check the remaining ALGO position and orders in RIT.")
        except (RuntimeError, RateLimited) as exc:
            print(f"Could not verify final orders, fills and position: {exc}")


def main():
    if not API_KEY.strip() or API_KEY == "YOUR API KEY HERE":
        print("Set API_KEY in algo2.py before running.")
        return 1
    try:
        run()
    except (RuntimeError, RateLimited, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
