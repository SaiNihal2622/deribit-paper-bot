"""Crypto Options Paper-Trading Bot.

A focused, real-time crypto options paper-trading system using Deribit public
market data. Mirrors proven patterns from the Kotak Neo bot, but adapted for
crypto: Deribit symbol format (BTC-26DEC25-100000-C), daily expiries, no
lot size (1 contract = 1 unit), and Deribit-published mark_iv for Greeks.
"""
from __future__ import annotations

__version__ = "0.1.0"
