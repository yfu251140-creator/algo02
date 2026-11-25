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
FLATTEN_TICK = CASE_TICKS - 10
POLL_SECONDS = 0.5
MAX_ORDERS_PER_SECOND = 10
PROBE_SECONDS = 2.0
POLL_RETRY_SECONDS = 1.0


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
                delay = float(exc.headers.get("Retry-After", str(POLL_RETRY_SECONDS)))
            except ValueError:
                delay = POLL_RETRY_SECONDS
            raise RateLimited(max(0.0, delay)) from exc
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


class OrderPace:
    def __init__(self):
        self.rate = 1
        self.locked = False
        self.next_at = 0.0
        self.last_quote_at = None
        self.probe_started_at = None
        self.validated_rate = 0

    def accepted(self, sent_at, probing):
        interval = 1 / self.rate
        if probing and not self.locked:
            if self.last_quote_at is None or sent_at - self.last_quote_at > interval * 1.25:
                self.probe_started_at = sent_at
            self.last_quote_at = sent_at
            if sent_at - self.probe_started_at >= PROBE_SECONDS:
                self.validated_rate = self.rate
                if self.rate == MAX_ORDERS_PER_SECOND:
                    self.locked = True
                    print("Held 10 orders/sec without order HTTP 429.", flush=True)
                else:
                    self.rate += 1
                    self.probe_started_at = None
                    self.last_quote_at = None
                    print(f"Testing {self.rate} orders/sec.", flush=True)
        self.next_at = sent_at + 1 / self.rate

    def limited(self, sent_at, retry_after):
        self.rate = max(1, self.rate - 1)
        self.locked = True
        self.next_at = max(sent_at + 1 / self.rate, monotonic() + retry_after)
        print(f"Order HTTP 429. Pace locked at {self.rate} orders/sec.", flush=True)


def submit(fills, pace, action, quantity, price_cents=None):
    params = dict(ticker=TICKER, action=action, quantity=quantity, type="MARKET")
    if price_cents is not None:
        params.update(type="LIMIT", price=f"{price_cents / 100:.2f}")
    sent_at = monotonic()
    try:
        order = api("POST", "/orders", **params)
    except RateLimited as exc:
        pace.limited(sent_at, exc.args[0])
        return
    fills[order["order_id"]] = int(order["quantity_filled"])
    pace.accepted(sent_at, probing=price_cents is not None)


def cents(price):
    return round(float(price) * 100)


def quote(fills, pace, working, inventory):
    book = api("GET", "/securities/book", ticker=TICKER)
    if not book["bids"] or not book["asks"]:
        return

    bid = cents(book["bids"][0]["price"])
    ask = cents(book["asks"][0]["price"])
    stale = [
        order for order in working
        if (order["action"] == "BUY" and cents(order["price"]) < bid)
        or (order["action"] == "SELL" and cents(order["price"]) > ask)
    ]
    if stale:
        cancel(stale)
        return

    if ask - bid < MIN_SPREAD_CENTS:
        return

    sides = [
        ("BUY", bid, MAX_POSITION - inventory),
        ("SELL", ask, MAX_POSITION + inventory),
    ]
    if inventory > 0:
        sides.reverse()
    working_sides = {order["action"] for order in working}
    for action, price, capacity in sides:
        if action not in working_sides and capacity > 0:
            submit(fills, pace, action, min(MAX_ORDER, capacity), price)
            return


def run():
    if api("GET", "/case")["status"] != "ACTIVE":
        raise RuntimeError("Start the ALGO2 simulation in RIT first.")
    if position() or open_orders():
        raise RuntimeError("Start with a flat ALGO position and no working ALGO orders.")

    fills = {}
    last_report = None
    pace = OrderPace()
    print("Testing 1 order/sec.", flush=True)
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
                        f"| Position: {inventory:+,} | Pace: {pace.rate}/s", flush=True
                    )
                    last_report = report

                if case["tick"] >= FLATTEN_TICK:
                    quotes = [order for order in working if order["type"] == "LIMIT"]
                    if quotes:
                        cancel(quotes)
                    elif not working and not pending:
                        if not inventory:
                            break
                        if monotonic() >= pace.next_at:
                            action = "SELL" if inventory > 0 else "BUY"
                            submit(fills, pace, action, min(MAX_ORDER, abs(inventory)))
                elif not pending and monotonic() >= pace.next_at:
                    quote(fills, pace, working, inventory)
            except RateLimited as exc:
                delay = max(POLL_RETRY_SECONDS, exc.args[0])
                print(f"RIT request limited. Waiting {delay:g} seconds.", flush=True)
                sleep(delay)
                continue

            until_slot = pace.next_at - monotonic()
            sleep(min(POLL_SECONDS, until_slot) if until_slot > 0 else POLL_SECONDS)
    finally:
        print(
            f"Final order pace: {pace.rate}/s | "
            f"Highest sustained probe without order HTTP 429: {pace.validated_rate or 'none'}"
        )
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
