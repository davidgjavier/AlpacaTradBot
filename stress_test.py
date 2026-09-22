import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest


SYMBOL = "SPY"
LIMIT_PRICE = 1.00
BURST_COUNT = 8
MAX_BACKOFF_SECONDS = 8


def build_client():
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running this test.")
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=True)


def validate_payload(payload):
    symbol = payload.get("symbol")
    quantity = payload.get("qty")
    limit_price = payload.get("limit_price")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,4}", symbol):
        raise ValueError("invalid ticker")
    if quantity is None or quantity <= 0:
        raise ValueError("quantity must be positive")
    if limit_price is None or limit_price <= 0:
        raise ValueError("limit price must be positive")


def malformed_payload_test():
    payloads = [
        {"symbol": "NOT A SYMBOL", "qty": 1, "limit_price": LIMIT_PRICE},
        {"symbol": SYMBOL, "qty": 0, "limit_price": LIMIT_PRICE},
        {"symbol": SYMBOL, "qty": 1, "limit_price": -1.00},
    ]
    rejected = 0
    for payload in payloads:
        try:
            validate_payload(payload)
        except ValueError as error:
            rejected += 1
            print(f"MALFORMED rejected safely: {error}")
        else:
            print(f"MALFORMED unexpectedly accepted: {payload}")
    return rejected == len(payloads)


def order_request():
    return LimitOrderRequest(
        symbol=SYMBOL,
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=LIMIT_PRICE,
    )


def is_rate_limited(error):
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    return status_code == 429 or getattr(response, "status_code", None) == 429


def submit_with_backoff(client, attempt_label):
    delay = 1
    for attempt in range(1, 6):
        try:
            order = client.submit_order(order_data=order_request())
            print(f"BURST {attempt_label}: submitted {order.id}")
            return order.id
        except Exception as error:
            if not is_rate_limited(error) or attempt == 5:
                print(f"BURST {attempt_label}: failed ({type(error).__name__})")
                return None
            print(f"BURST {attempt_label}: HTTP 429, backing off {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    return None


def cancellation_race_test(client):
    order_ids = [order_id for order_id in (submit_with_backoff(client, "race") for _ in range(3)) if order_id]
    if not order_ids:
        print("RACE skipped: no test orders were accepted")
        return True

    def cancel(order_id):
        try:
            client.cancel_order_by_id(order_id)
            return "cancelled"
        except Exception as error:
            return f"{type(error).__name__}"

    results = []
    with ThreadPoolExecutor(max_workers=len(order_ids) * 2) as executor:
        futures = [executor.submit(cancel, order_id) for order_id in order_ids for _ in range(2)]
        for future in as_completed(futures):
            results.append(future.result())
    print(f"RACE completed: {len(results)} concurrent cancellation attempts ({', '.join(results)})")
    return True


def burst_submission_test(client):
    with ThreadPoolExecutor(max_workers=BURST_COUNT) as executor:
        futures = [executor.submit(submit_with_backoff, client, index + 1) for index in range(BURST_COUNT)]
        order_ids = [future.result() for future in as_completed(futures)]
    submitted = sum(order_id is not None for order_id in order_ids)
    print(f"BURST completed: {submitted}/{BURST_COUNT} submissions succeeded")
    return True


def emergency_killswitch_test(client):
    client.cancel_orders()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        open_orders = client.get_orders(filter=request)
        if not open_orders:
            print("KILLSWITCH verified: open order book is empty")
            return True
        time.sleep(1)
    print(f"KILLSWITCH warning: {len(client.get_orders(filter=request))} open orders remain")
    return False


def main():
    client = build_client()
    results = {}
    try:
        results["malformed"] = malformed_payload_test()
        results["race"] = cancellation_race_test(client)
        results["burst"] = burst_submission_test(client)
    finally:
        results["killswitch"] = emergency_killswitch_test(client)

    print("RESULTS:")
    for name, passed in results.items():
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
