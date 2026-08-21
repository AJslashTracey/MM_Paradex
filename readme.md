# Regime dependent Market Making 

My plan for this algo right here is to profit from quoting around an external fair price, with a regime dependent inventory managment system. 


Data needed:
Hyperliquid: Data:BBO; timestamp, bid/ask size; purpose: realtime extermal fair value as basis 

Fairprice v1: F=(HLbid+HLask)/2 

Paradex: 

Data: BBO: bid/ask + size, => determine available spread and quote placement 

Market metadata  tick size, lot size minimum order, Generate orders 

Trades: price, size, side timestamp => measure activity and possible fill conditions 

Book updates: sequence number and timestamp => detect gaps or stale books

Funding: current rate => inventory cost

For now I think L1 data is enough 


Account data
Open order: Id, side, price, size, remaining size =>  order reconcilliation 



