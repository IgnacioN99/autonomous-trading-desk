#!/usr/bin/env python3
"""
exchange_adapter.py - Capa de Abstracción y Adaptadores de Exchanges.
Patrón de diseño Adapter para soporte multi-exchange desacoplado.

Define un contrato unificado e independiente del exchange (ExchangeAdapter) para:
- Consulta de posiciones activas y precios
- Ejecución de órdenes de mercado y límite
- Colocación y verificación de Stop Loss algorítmico
- Cancelación masiva y cierre de emergencia

Permite alternar entre Binance Testnet, Binance Mainnet o futuros exchanges (Hyperliquid/Bybit)
sin tener que modificar ni una sola línea de lógica en los evaluadores o screeners.
"""

import os
import sys
import abc
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute_futures_trade as eft

class BaseExchangeAdapter(abc.ABC):
    """Contrato abstracto para cualquier exchange de futuros perp."""

    @abc.abstractmethod
    def get_ticker_price(self, symbol: str) -> float:
        pass

    @abc.abstractmethod
    def get_symbol_filters(self, symbol: str) -> dict:
        pass

    @abc.abstractmethod
    def get_active_positions(self) -> List[dict]:
        pass

    @abc.abstractmethod
    def place_order(self, symbol: str, side: str, order_type: str, qty: float, price: Optional[float] = None, reduce_only: bool = False) -> dict:
        pass

    @abc.abstractmethod
    def place_algo_stop_loss(self, symbol: str, side: str, trigger_price: float) -> dict:
        pass

    @abc.abstractmethod
    def close_position_market(self, symbol: str) -> dict:
        pass

class BinanceFuturesAdapter(BaseExchangeAdapter):
    """Adaptador concreto para Binance USDⓈ-M Futures (Testnet y Mainnet)."""

    def __init__(self, target_env: str = "testnet"):
        self.target_env = target_env.lower()

    def get_ticker_price(self, symbol: str) -> float:
        res = eft.send_signed_request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, target_env=self.target_env)
        return float(res.get("price", 0.0)) if isinstance(res, dict) else 0.0

    def get_symbol_filters(self, symbol: str) -> dict:
        return eft.get_symbol_filters(symbol, target_env=self.target_env) or {}

    def get_active_positions(self) -> List[dict]:
        res = eft.send_signed_request("GET", "/fapi/v2/positionRisk", target_env=self.target_env)
        return [p for p in res if float(p.get("positionAmt", 0)) != 0] if isinstance(res, list) else []

    def place_order(self, symbol: str, side: str, order_type: str, qty: float, price: Optional[float] = None, reduce_only: bool = False) -> dict:
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": qty
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if price is not None and order_type.upper() == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"
        return eft.send_signed_request("POST", "/fapi/v1/order", params, target_env=self.target_env)

    def place_algo_stop_loss(self, symbol: str, side: str, trigger_price: float) -> dict:
        return eft.place_algo_stop_loss(symbol, side.upper(), trigger_price, target_env=self.target_env)

    def close_position_market(self, symbol: str) -> dict:
        return eft.close_position_market(symbol, target_env=self.target_env)

def get_exchange_adapter(exchange_name: str = "binance", target_env: str = "testnet") -> BaseExchangeAdapter:
    """Fábrica de adaptadores."""
    if exchange_name.lower() == "binance":
        return BinanceFuturesAdapter(target_env=target_env)
    else:
        raise ValueError(f"Exchange no soportado: {exchange_name}")

if __name__ == "__main__":
    adapter = get_exchange_adapter("binance", "testnet")
    btc_p = adapter.get_ticker_price("BTCUSDT")
    positions = adapter.get_active_positions()
    print(f"✅ Adapter conectado a Binance Futures ({adapter.target_env.upper()}):")
    print(f"   BTC Price: ${btc_p:,.2f}")
    print(f"   Posiciones Vivas: {len(positions)}")
