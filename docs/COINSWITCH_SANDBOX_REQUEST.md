# CoinSwitch non-production environment request

Send this from the email address registered to the CoinSwitch account. Do not
include API keys, private keys, OTPs, balances, or account screenshots.

**To:** `api@coinswitch.co`  
**Subject:** Request for CoinSwitch Spot API sandbox/test environment details

Hello CoinSwitch API Support,

We are validating a safety-first Spot trading integration. It currently uses
only the public market-data stream and an in-process paper broker; it does not
send authenticated requests or real orders.

Before implementing authenticated transport, please confirm in writing:

1. Whether CoinSwitch provides a non-production Spot sandbox or testnet.
2. Its official REST and WebSocket base URLs.
3. How sandbox API credentials are provisioned and revoked.
4. Whether authentication matches the documented Ed25519 production flow.
5. Supported exchanges and symbols, including `coinswitchx`, `c2c1`, and `c2c2`.
6. Sandbox behaviour for client order IDs, duplicate requests, partial fills,
   cancellation races, expiration, rejections, timeouts, and `5xx` responses.
7. Sandbox rate limits, balances, fees, TDS/tax simulation, and reset procedure.
8. Whether private order and balance WebSocket streams are available.
9. Any restrictions on automated testing or repeated order-lifecycle drills.

If no sandbox exists, please confirm that explicitly and advise the approved
method for testing with zero or strictly limited financial exposure.

Thank you.

