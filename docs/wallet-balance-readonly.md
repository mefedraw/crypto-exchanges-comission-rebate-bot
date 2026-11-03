# Read-only payout-wallet balance

The admin balance view must not depend on `PAYOUT_PRIVATE_KEY`. The bot process
has no signing key after role-isolated deployment; only the payout executor does.
Using executor client factories in the bot incorrectly displayed "not configured"
for both networks even though the executor was configured and RPC calls worked.

`bot.admin.wallet_balance` reads the public payout address through Web3 RPC:

- BSC: native BNB and BEP20 USDT, both with 18 decimal places.
- Mantle: native MNT with 18 decimal places.
- Expected chain IDs are checked before reading balances.
- RPC errors are shown per network as exception types, without endpoint URLs.
- Each reader disconnects its provider after use; callback acknowledgement comes
  before RPC requests so the Telegram callback does not expire during slow reads.

Required public configuration: `PAYOUT_WALLET_ADDRESS`, `BSC_RPC_URLS`,
`USDT_CONTRACT`, `BSC_CHAIN_ID`, `MANTLE_RPC_URL`, and `MANTLE_CHAIN_ID`.
The reader neither signs transactions nor derives an address from a private key.
Missing reader configuration is not presented as proof that payouts are disabled.

Deploy only the bot image and the two admin source files. No migration, worker
restart, environment-secret change, or financial recalculation is required.
Keep the prior bot image and Compose configuration for rollback. When updating a
source-only production directory, preserve the independent pinned accrual and
executor images and the credential updater's Compose configuration.
