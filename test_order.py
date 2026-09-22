import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running this test.")

client = TradingClient(api_key=API_KEY, secret_key=SECRET_KEY, paper=True)

# Low-ball limit order to verify connectivity without accidental fills
test_order_data = LimitOrderRequest(
    symbol="SPY",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
    limit_price=100.00
)

order = client.submit_order(order_data=test_order_data)
print(f"Order submitted successfully. ID: {order.id}, Status: {order.status}")

