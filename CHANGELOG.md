# Changelog

All notable changes to this project will be documented in this file.

The format loosely follows Keep a Changelog, and this project uses calendar dates
for unreleased work snapshots.

## [Unreleased]

### Added

- Added BitMart Futures affiliate rebate support via the KEYED futures API.
- Added `BITMART_API_KEY` configuration; `BITMART_API_SECRET` is optional for the
  current BitMart futures affiliate endpoints.
- Added tests for BitMart multi-currency rebate parsing and KEYED request headers.

### Changed

- Result rendering now preserves all returned assets instead of showing only
  USDT, so BTC/ETH rebate lines are not silently dropped.
- Updated user-facing help text and project documentation to describe
  multi-currency results.

### Notes

- BitMart `rebate-user` returns rebate data without a currency field, so the bot
  uses the futures `rebate-list` endpoint to keep the BTC/USDT/ETH split.
