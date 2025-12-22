
==============
Major Files:-
==============

backtest_trading_cp.py - File which will be used to execute orders with current market price and will book profits at given amount, and it is having notification once profit booked with alarm tune

sample command:- Buy 14 NIFTY24JUL2526450CE at CP and Book at 1500
----------------------------------------------------------------------
live_option_bot_cp.py - File which will be used to execute orders with current market price and will book profits at given amount, and it is not having notification once profit booked with alarm tune

sample command:- Buy 14 NIFTY24JUL2526450CE at CP and Book at 1500
----------------------------------------------------------------------

live_option_bot.py - File which will be used to execute orders with specific Buying and Exit/Target price

sample command:- Buy 2 NIFTY24JUL2526450CE at 5.5 and Sell at 6.5
----------------------------------------------------------------------

=================
Execution Flow:-
=================
1. After opening pycharm, we need to first initialize main class like(backtest_trading_cp.py,live_option_bot_cp.py or live_option_bot.py, based on requirement) using command:-
• python backtest_trading_cp.py

2. After this we can directly push the command like:-
• Buy 14 NIFTY24JUL2526450CE at CP and Book at 1500

3. This will place order and will Book profit after given amount

----------------------------------------------------------------------------

======================
Option Chain Viewer:-
======================

To Get Exact Option Symbol like "NIFTY24JUL2526450CE" for any option, follow this below process:-
1. python generate_option_chain.py
2. cd frontend
3. python -m http.server 8000
4. Open Browser with "http://localhost:8000/"
5. now you will get option chain viewer




