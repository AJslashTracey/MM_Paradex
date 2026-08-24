import ccxt as cc
from eth_account import Account
import os

Account.enable_unaudited_hdwallet_features()
wallet = Account.create()
print("Address:", wallet.address)
print("Private Key:", wallet.key.hex())

