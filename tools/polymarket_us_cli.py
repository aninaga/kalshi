#!/usr/bin/env python3
"""Polymarket US account and guarded trading CLI.

This uses the Polymarket US API key style from polymarket.us/developer, not the
legacy polymarket.com CLOB wallet flow.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv


API_BASE = "https://api.polymarket.us"
GATEWAY_BASE = "https://gateway.polymarket.us"


class PolymarketUSClient:
    def __init__(self, key_id: str | None = None, secret_key: str | None = None):
        load_dotenv()
        self.key_id = key_id or os.getenv("POLYMARKET_US_KEY_ID")
        self.secret_key = secret_key or os.getenv("POLYMARKET_US_SECRET_KEY")
        self.session = requests.Session()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.key_id or not self.secret_key:
            raise RuntimeError(
                "Set POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY in .env"
            )

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        secret = "".join(self.secret_key.split())
        key_bytes = base64.b64decode(secret)
        if len(key_bytes) == 64:
            key_bytes = key_bytes[:32]
        signature = Ed25519PrivateKey.from_private_bytes(key_bytes).sign(message)

        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": base64.b64encode(signature).decode(),
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        base = API_BASE if authenticated else GATEWAY_BASE
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers.update(self._auth_headers(method, path))

        response = self.session.request(
            method,
            f"{base}{path}",
            params=params,
            json=body,
            headers=headers,
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(
                f"{method} {path} failed: {response.status_code} {response.text}"
            )
        if response.content:
            return response.json()
        return None

    def balances(self) -> dict[str, Any]:
        return self.request("GET", "/v1/account/balances")

    def positions(self) -> dict[str, Any]:
        return self.request("GET", "/v1/portfolio/positions")

    def activities(self, limit: int = 100, cursor: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/v1/portfolio/activities", params=params)

    def open_orders(self) -> dict[str, Any]:
        return self.request("GET", "/v1/orders/open")

    def preview_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/order/preview", body={"request": payload})

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/orders", body=payload)

    def cancel_order(self, order_id: str, market_slug: str) -> None:
        self.request(
            "POST",
            f"/v1/order/{order_id}/cancel",
            body={"marketSlug": market_slug},
        )

    def cancel_all(self, slugs: list[str] | None = None) -> dict[str, Any]:
        body = {"slugs": slugs} if slugs else {}
        return self.request("POST", "/v1/orders/open/cancel", body=body)

    def market_bbo(self, market_slug: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/v1/markets/{market_slug}/bbo", authenticated=False
        )

    def market_book(self, market_slug: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/v1/markets/{market_slug}/book", authenticated=False
        )


def amount(value: str | float | Decimal) -> dict[str, str]:
    return {"value": str(Decimal(str(value))), "currency": "USD"}


def build_order_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "marketSlug": args.market_slug,
        "intent": args.intent,
        "type": args.order_type,
        "tif": args.tif,
        "manualOrderIndicator": args.manual_order_indicator,
        "synchronousExecution": True,
        "maxBlockTime": args.max_block_time,
    }
    if args.price is not None:
        payload["price"] = amount(args.price)
    if args.quantity is not None:
        payload["quantity"] = args.quantity
    if args.cash_usd is not None:
        payload["cashOrderQty"] = amount(args.cash_usd)
    if args.slippage_bips is not None:
        payload["slippageTolerance"] = {"bips": args.slippage_bips}
        if args.current_price is not None:
            payload["slippageTolerance"]["currentPrice"] = amount(args.current_price)
    return payload


def estimated_cash_exposure(payload: dict[str, Any]) -> Decimal:
    if "cashOrderQty" in payload:
        return Decimal(payload["cashOrderQty"]["value"])
    quantity = Decimal(str(payload.get("quantity", 0)))
    price_value = payload.get("price", {}).get("value")
    if not quantity or price_value is None:
        return Decimal("0")
    price = Decimal(str(price_value))
    if "SHORT" in payload.get("intent", ""):
        return quantity * (Decimal("1") - price)
    return quantity * price


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def account_command(client: PolymarketUSClient, _args: argparse.Namespace) -> None:
    balances = client.balances()
    positions = client.positions()
    orders = client.open_orders()
    print_json(
        {
            "balances": balances,
            "positions_count": len(positions.get("positions", {})),
            "available_positions_count": len(positions.get("availablePositions", [])),
            "open_orders_count": len(orders.get("orders", [])),
            "open_orders": orders.get("orders", []),
        }
    )


def activities_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    all_activities = []
    cursor = ""
    eof = False
    while not eof and len(all_activities) < args.max:
        page = client.activities(limit=min(100, args.max - len(all_activities)), cursor=cursor)
        all_activities.extend(page.get("activities", []))
        eof = bool(page.get("eof"))
        cursor = page.get("nextCursor", "")
        if not cursor:
            break
    output = {"count": len(all_activities), "activities": all_activities}
    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2))
    print_json({"count": len(all_activities), "eof": eof, "output": args.output})


def preview_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    payload = build_order_payload(args)
    print_json(client.preview_order(payload))


def place_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    if not args.confirm_live_order:
        raise SystemExit("Refusing live order: pass --confirm-live-order to place it.")

    payload = build_order_payload(args)
    exposure = estimated_cash_exposure(payload)
    max_cash = Decimal(str(args.max_cash_usd))
    if exposure > max_cash:
        raise SystemExit(
            f"Refusing live order: estimated exposure {exposure} > max {max_cash}."
        )

    balances = client.balances().get("balances", [])
    buying_power = Decimal(str(balances[0].get("buyingPower", 0))) if balances else Decimal("0")
    if exposure > buying_power:
        raise SystemExit(
            f"Refusing live order: exposure {exposure} > buying power {buying_power}."
        )

    preview = client.preview_order(payload)
    order = client.create_order(payload)
    print_json({"preview": preview, "order": order})


def cancel_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    client.cancel_order(args.order_id, args.market_slug)
    print_json({"canceled": args.order_id, "marketSlug": args.market_slug})


def cancel_all_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    print_json(client.cancel_all(args.market_slug or None))


def bbo_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    print_json(client.market_bbo(args.market_slug))


def book_command(client: PolymarketUSClient, args: argparse.Namespace) -> None:
    print_json(client.market_book(args.market_slug))


def add_order_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--market-slug", required=True)
    parser.add_argument(
        "--intent",
        required=True,
        choices=[
            "ORDER_INTENT_BUY_LONG",
            "ORDER_INTENT_SELL_LONG",
            "ORDER_INTENT_BUY_SHORT",
            "ORDER_INTENT_SELL_SHORT",
        ],
    )
    parser.add_argument(
        "--order-type",
        default="ORDER_TYPE_LIMIT",
        choices=["ORDER_TYPE_LIMIT", "ORDER_TYPE_MARKET"],
    )
    parser.add_argument("--price")
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--cash-usd")
    parser.add_argument(
        "--tif",
        default="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        choices=[
            "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "TIME_IN_FORCE_GOOD_TILL_DATE",
            "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "TIME_IN_FORCE_FILL_OR_KILL",
        ],
    )
    parser.add_argument(
        "--manual-order-indicator",
        default="MANUAL_ORDER_INDICATOR_AUTOMATIC",
        choices=[
            "MANUAL_ORDER_INDICATOR_MANUAL",
            "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        ],
    )
    parser.add_argument("--max-block-time", default="3s")
    parser.add_argument("--slippage-bips", type=int)
    parser.add_argument("--current-price")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("account")
    p.set_defaults(func=account_command)

    p = sub.add_parser("activities")
    p.add_argument("--max", type=int, default=100)
    p.add_argument("--output")
    p.set_defaults(func=activities_command)

    p = sub.add_parser("preview")
    add_order_args(p)
    p.set_defaults(func=preview_command)

    p = sub.add_parser("place")
    add_order_args(p)
    p.add_argument("--max-cash-usd", default="100")
    p.add_argument("--confirm-live-order", action="store_true")
    p.set_defaults(func=place_command)

    p = sub.add_parser("cancel")
    p.add_argument("--order-id", required=True)
    p.add_argument("--market-slug", required=True)
    p.set_defaults(func=cancel_command)

    p = sub.add_parser("cancel-all")
    p.add_argument("--market-slug", action="append")
    p.set_defaults(func=cancel_all_command)

    p = sub.add_parser("bbo")
    p.add_argument("--market-slug", required=True)
    p.set_defaults(func=bbo_command)

    p = sub.add_parser("book")
    p.add_argument("--market-slug", required=True)
    p.set_defaults(func=book_command)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    client = PolymarketUSClient()
    args.func(client, args)


if __name__ == "__main__":
    main()
