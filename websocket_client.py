import asyncio
import websockets
import json

class KalshiWebsocketClient:
    def __init__(self, uri="wss://api.elections.kalshi.com"):
        self.uri = uri
        self.websocket = None
        self.subscriptions = {}

    async def connect(self):
        self.websocket = await websockets.connect(self.uri)

    async def send(self, message):
        await self.websocket.send(json.dumps(message))

    async def receive(self):
        message = await self.websocket.recv()
        return json.loads(message)

    async def subscribe(self, channels, market_ticker):
        message_id = len(self.subscriptions) + 1
        subscribe_message = {
            "id": message_id,
            "cmd": "subscribe",
            "params": {
                "channels": channels,
                "market_ticker": market_ticker
            }
        }
        await self.send(subscribe_message)
        response = await self.receive()
        if response.get("type") == "subscribed":
            self.subscriptions[message_id] = response["msg"]["sid"]
        return response

    async def unsubscribe(self, sids):
        message_id = len(self.subscriptions) + 1
        unsubscribe_message = {
            "id": message_id,
            "cmd": "unsubscribe",
            "params": {
                "sids": sids
            }
        }
        await self.send(unsubscribe_message)
        return await self.receive()

    async def update_subscription(self, sids, action, market_tickers):
        message_id = len(self.subscriptions) + 1
        update_message = {
            "id": message_id,
            "cmd": "update_subscription",
            "params": {
                "sids": sids,
                "action": action,
                "market_tickers": market_tickers
            }
        }
        await self.send(update_message)
        return await self.receive()

    async def close(self):
        await self.websocket.close()

async def main():
    client = KalshiWebsocketClient()
    await client.connect()

    # Example: Subscribe to a channel
    response = await client.subscribe(["orderbook_delta"], "CPI-22DEC-TN0.1")
    print(f"< {response}")

    # Process incoming messages
    try:
        while True:
            message = await client.receive()
            print(f"< {message}")
    except websockets.exceptions.ConnectionClosed:
        print("Connection closed")
    finally:
        await client.close()

if __name__ == "__main__":
    # To run this example, you would need to have a running asyncio event loop.
    # For example, you can run this file with `python websocket_client.py`
    # and it will connect and subscribe to the specified channel.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
