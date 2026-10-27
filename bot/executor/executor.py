# bot/executor/executor.py
"""State-machine driver for the payout executor.

`process_next` is the single-flight submit: it claims at most one `reserved`
payout, persists the nonce (mark_submitting + commit) BEFORE broadcasting, and
never blindly re-sends on broadcast uncertainty (§4.3) — that is left to a
later `reconcile()` pass. The decrypted recipient address and the master key
are never logged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, localcontext

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.admin import repository as admin_repo
from bot.botui import repository as botui_repo
from bot.executor import repository as repo
from bot.executor.chain import (
    DEFAULT_GAS_LIMIT,
    NATIVE_TRANSFER_GAS,
    ChainClient,
    ChainError,
    ChainReader,
    FundsShortfall,
    NativeChainClient,
    exact_18_to_wei,
    native_to_wei,
    usdt_to_wei,
)
from bot.logging import get_logger
from bot.notifications import repository as notif_repo
from bot.security.secrets import SecretCipher

logger = get_logger(__name__)


def _mask_addr(addr: str) -> str:
    """Recipient address for logs — head+tail only, never the full string in one place."""
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) >= 10 else "…"


class PayoutExecutor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        chain: ChainClient,
        cipher: SecretCipher,
        *,
        now: Callable[[], datetime],
        wallet_address: str,
        auto_limit: Decimal,
        min_payout: Decimal,
        confirmations: int,
        developer_id: int | None = None,
        mantle_chain: NativeChainClient | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._chain = chain
        self._mantle_chain = mantle_chain
        self._cipher = cipher
        self._now = now
        self._wallet = wallet_address
        self._auto_limit = auto_limit
        self._min_payout = min_payout
        self._confirmations = confirmations
        self._developer_id = developer_id
        # Payout ids we've already alerted the developer about for the CURRENT stuck
        # episode (insufficient funds, or an uncertain broadcast). A held/failing payout
        # is re-examined every 30s tick and auto-retried; dedup so it alerts ONCE, not
        # every tick. Cleared on real progress (mark_sent) so a fresh episode re-alerts.
        # In-memory only: a process restart re-alerts once, which is acceptable.
        self._alerted_pids: set[int] = set()

    def _client_for(self, asset: str) -> ChainReader | None:
        """The chain client that owns ``asset``'s ledger — Mantle for MNT (native,
        separate chain/nonce-space), BSC for everything else (USDT). ``None`` for MNT
        means the Mantle client is not configured/enabled.
        """
        return self._mantle_chain if asset == "MNT" else self._chain

    async def _dev_alert(self, session: AsyncSession, title: str, detail: str) -> None:
        """Queue a developer alert (no-op if no developer chat is configured)."""
        if self._developer_id is None:
            return
        await notif_repo.enqueue(
            session,
            recipient_chat_id=self._developer_id,
            kind=notif_repo.DEV_ALERT,
            payload={"title": title, "detail": detail},
        )

    async def _hold_insufficient(
        self, session: AsyncSession, pid: int, asset: str, shortfall: FundsShortfall
    ) -> None:
        """Insufficient hot-wallet balance: leave the payout ``reserved`` (do NOT
        broadcast) so it auto-retries once the owner tops up, and alert the developer
        ONCE per hold episode. Never fails the payout — the money stays reserved for the
        user, and nothing is sent that would revert (USDT) or stick submitting (MNT)."""
        # `shortfall.kind == "gas"` only arises for a USDT payout (native gas = BNB); the
        # amount coin is USDT for token payouts and MNT for native ones.
        coin = "BNB" if shortfall.kind == "gas" else ("MNT" if asset == "MNT" else "USDT")
        detail = (
            f"Выплата #{pid} ({asset}): не хватает {coin}. "
            f"Нужно {shortfall.need:.6f}, на кошельке {shortfall.have:.6f}. "
            "Пополни кошелёк — выплата уйдёт автоматически."
        )
        logger.warning(
            "payout insufficient funds -> held reserved",
            pid=pid,
            asset=asset,
            kind=shortfall.kind,
            need=str(shortfall.need),
            have=str(shortfall.have),
        )
        if pid not in self._alerted_pids:
            self._alerted_pids.add(pid)
            await self._dev_alert(session, "Недостаточно средств для выплаты", detail)

    async def process_next(self) -> None:
        # single-flight: never start a second payout while one is submitting/sent
        async with self._sm() as session:
            # Kill-switch (§Task 6): a paused flag stops ALL sends/reserves — checked
            # first, before even the single-flight/claim, so a paused payout is left
            # exactly as-is (a reserved payout stays reserved; nothing is claimed).
            if await admin_repo.is_payouts_paused(session):
                return
            if await repo.payouts_in_flight(session):
                return
            payout = await repo.claim_next_reserved(session)
            if payout is None:
                return
            pid, net, addr_enc = payout.id, payout.net_usdt, payout.address_encrypted
            asset, net_native = payout.asset, payout.net_native
            to_base_units: Callable[[Decimal], int]
            if payout.purpose == "partner":
                to_base_units = exact_18_to_wei
            elif asset == "MNT":
                to_base_units = native_to_wei
            else:
                to_base_units = usdt_to_wei
            uid = payout.user_id
            approved = payout.approved_at is not None
            logger.info("claimed reserved payout", pid=pid, asset=asset, net_usdt=str(net))
            # Per-user block gate (defense-in-depth for confirm_withdrawal): a payout
            # reserved BEFORE an admin blocked the user must not be sent. FAIL it (not
            # skip) — skipping leaves it as the oldest 'reserved' row, which
            # claim_next_reserved would hand back every tick and starve everyone else's
            # payouts. finalize_failed returns the accruals to 'available', so the money
            # is preserved for after an unblock.
            if (await botui_repo.get_user_status(session, uid)) == "blocked":
                logger.warning("payout owner blocked -> failed", pid=pid, uid=uid)
                await repo.finalize_failed(session, pid, "user_blocked")
                await botui_repo.write_audit(
                    session,
                    actor="executor",
                    action="payout_failed",
                    entity_type="payout",
                    entity_id=str(pid),
                    new_value={"reason": "user_blocked"},
                )
                await self._dev_alert(
                    session, "Payout failed (user blocked)", f"payout #{pid}, user {uid}"
                )
                await session.commit()
                return
            # FINDING 1: only gate a NOT-yet-approved payout. An admin-released payout
            # (approved_at set by admin_repo.release_payout, status already flipped back
            # to 'reserved') must proceed to send — re-checking the raw amount here on
            # every tick would otherwise bounce it straight back to pending_admin
            # forever, since release_payout has no other memory of the approval.
            if not approved and net > self._auto_limit:
                logger.info(
                    "payout exceeds auto-limit -> pending_admin",
                    pid=pid,
                    net_usdt=str(net),
                    auto_limit=str(self._auto_limit),
                )
                await repo.mark_pending_admin(session, pid)
                await botui_repo.write_audit(
                    session,
                    actor="executor",
                    action="payout_pending_admin",
                    entity_type="payout",
                    entity_id=str(pid),
                )
                await session.commit()
                return
            if net < self._min_payout or net <= 0:
                logger.warning("payout below min -> failed", pid=pid, net_usdt=str(net))
                await repo.finalize_failed(session, pid, "below_min")
                await botui_repo.write_audit(
                    session,
                    actor="executor",
                    action="payout_failed",
                    entity_type="payout",
                    entity_id=str(pid),
                )
                await notif_repo.enqueue(
                    session,
                    recipient_chat_id=uid,
                    kind=notif_repo.PAYOUT_FAILED,
                    payload={
                        "net_usdt": str(net),
                        "asset": asset,
                        "net_native": str(net_native),
                        "reason": "below_min",
                    },
                )
                await self._dev_alert(
                    session, "Payout failed (below_min)", f"payout #{pid}, net {net}"
                )
                await session.commit()
                return
            if asset == "MNT":
                # MNT is admin-gated regardless of amount (see confirm_withdrawal) and
                # only ever reaches 'reserved' via an explicit admin release, so no
                # auto-limit re-check here — that branch above is USDT's, untouched.
                if self._mantle_chain is None:
                    # Defensive: MNT withdraw should already be gated off when disabled.
                    logger.error(
                        "MNT payout claimed but Mantle client is unavailable -> failed",
                        pid=pid,
                    )
                    await repo.finalize_failed(session, pid, "mnt_disabled")
                    await botui_repo.write_audit(
                        session,
                        actor="executor",
                        action="payout_failed",
                        entity_type="payout",
                        entity_id=str(pid),
                        new_value={"reason": "mnt_disabled"},
                    )
                    await self._dev_alert(
                        session, "MNT payout failed (mnt_disabled)", f"payout #{pid}"
                    )
                    await session.commit()
                    return
                # Native gas on Mantle is paid by the wallet owner — never quoted or
                # deducted from the payout (unlike USDT/BSC below).
                gas_usdt = Decimal(0)
                send_amount = net_native
                # Pre-flight: hold (don't broadcast) if the wallet can't cover MNT + gas,
                # so an underfunded send never sticks in 'submitting'. Auto-retries once
                # funded. Any RPC error here propagates -> we do NOT broadcast blind.
                shortfall = await self._mantle_chain.funds_shortfall(
                    amount_wei=to_base_units(send_amount),
                    token=False,
                    gas_limit=NATIVE_TRANSFER_GAS,
                )
                if shortfall is not None:
                    await self._hold_insufficient(session, pid, asset, shortfall)
                    await session.commit()
                    return
                onchain = await self._mantle_chain.get_nonce(self._wallet)
                last = await repo.last_nonce(session, "MNT")
                nonce = max(onchain, (last + 1) if last is not None else 0)
                await repo.mark_submitting(session, pid, nonce)
            else:
                # Gas is borne by the user: quote the transfer cost in USDT and deduct
                # it from the on-chain amount (#2). No quote (no feed/testnet) => none.
                quote = await self._chain.quote_gas_usdt(DEFAULT_GAS_LIMIT)
                gas_usdt = quote.gas_usdt if quote else Decimal(0)
                with localcontext() as ctx:
                    ctx.prec = 80
                    send_amount = net - gas_usdt
                if send_amount <= 0:
                    logger.warning(
                        "gas exceeds payout -> failed",
                        pid=pid,
                        net_usdt=str(net),
                        gas_usdt=str(gas_usdt),
                    )
                    await repo.finalize_failed(session, pid, "gas_exceeds_net")
                    await botui_repo.write_audit(
                        session,
                        actor="executor",
                        action="payout_failed",
                        entity_type="payout",
                        entity_id=str(pid),
                    )
                    await notif_repo.enqueue(
                        session,
                        recipient_chat_id=uid,
                        kind=notif_repo.PAYOUT_FAILED,
                        payload={
                            "net_usdt": str(net),
                            "asset": asset,
                            "net_native": str(net_native),
                            "reason": "gas_exceeds_net",
                        },
                    )
                    await self._dev_alert(
                        session,
                        "Payout failed (gas exceeds net)",
                        f"payout #{pid}, gas {gas_usdt}",
                    )
                    await session.commit()
                    return

                # Pre-flight: hold (don't broadcast) if the wallet lacks USDT for the
                # transfer or BNB for gas, so an underfunded send never reverts on-chain.
                # Auto-retries once funded. RPC error propagates -> no blind broadcast.
                shortfall = await self._chain.funds_shortfall(
                    amount_wei=to_base_units(send_amount), token=True, gas_limit=DEFAULT_GAS_LIMIT
                )
                if shortfall is not None:
                    await self._hold_insufficient(session, pid, asset, shortfall)
                    await session.commit()
                    return
                onchain = await self._chain.get_nonce(self._wallet)
                last = await repo.last_nonce(session, "USDT")
                nonce = max(onchain, (last + 1) if last is not None else 0)
                await repo.mark_submitting(
                    session,
                    pid,
                    nonce,
                    gas_bnb=quote.gas_bnb if quote else None,
                    gas_usdt=gas_usdt if quote else None,
                    bnb_usdt_rate=quote.bnb_usdt_rate if quote else None,
                )
            await session.commit()  # persist nonce+submitting BEFORE broadcast (§4.3.1)

        to = self._cipher.decrypt(addr_enc.decode())

        async def _persist_hash(txhash: str) -> None:
            # Durably record the hash while still 'submitting', BEFORE broadcast (#3),
            # so a crash in the broadcast window is resolvable from the DB alone.
            async with self._sm() as s:
                await repo.set_txhash(s, pid, txhash)
                await s.commit()

        mantle_chain = self._mantle_chain
        if asset == "MNT":
            if mantle_chain is None:
                # Unreachable: gated above (mnt_disabled returns before this point).
                raise RuntimeError(f"MNT payout #{pid} reached broadcast with no Mantle client")
            amount_wei = to_base_units(send_amount)
            logger.info(
                "broadcasting MNT payout",
                pid=pid,
                nonce=nonce,
                net_native=str(send_amount),
                amount_wei=amount_wei,
                to=_mask_addr(to),
            )
            send_call = mantle_chain.send_native(
                to=to, amount_wei=amount_wei, nonce=nonce, on_signed=_persist_hash
            )
        else:
            amount_wei = to_base_units(send_amount)
            logger.info(
                "broadcasting payout",
                pid=pid,
                nonce=nonce,
                net_usdt=str(net),
                gas_usdt=str(gas_usdt),
                amount_wei=amount_wei,
                to=_mask_addr(to),
            )
            send_call = self._chain.send_transfer(
                to=to, amount_wei=amount_wei, nonce=nonce, on_signed=_persist_hash
            )

        try:
            txhash = await send_call
        except ChainError as exc:
            # broadcast outcome is uncertain: do NOT re-send. Leave in `submitting` —
            # a later reconcile() resolves it by checking the chain.
            logger.error(
                "payout broadcast uncertain — left submitting for reconcile",
                pid=pid,
                nonce=nonce,
                error=str(exc),
            )
            async with self._sm() as s:
                await botui_repo.write_audit(
                    s,
                    actor="executor",
                    action="broadcast_uncertain",
                    entity_type="payout",
                    entity_id=str(pid),
                )
                # Dedup with the funds-hold alert: reconcile now re-queues a never-landed
                # 'submitting' every tick, so without this a persistent broadcast failure
                # would alert every 30s. One alert per stuck episode; cleared on mark_sent.
                if pid not in self._alerted_pids:
                    self._alerted_pids.add(pid)
                    await self._dev_alert(
                        s,
                        "Payout broadcast uncertain",
                        f"payout #{pid}, nonce {nonce} — left submitting for reconcile",
                    )
                await s.commit()
            return

        logger.info("payout broadcast sent", pid=pid, nonce=nonce, txhash=txhash)
        self._alerted_pids.discard(pid)  # real progress -> a future stuck episode re-alerts
        async with self._sm() as s:
            await repo.mark_sent(s, pid, txhash, self._now())
            await botui_repo.write_audit(
                s,
                actor="executor",
                action="payout_sent",
                entity_type="payout",
                entity_id=str(pid),
            )
            await s.commit()

    async def poll_confirmations(self) -> None:
        now = self._now()
        async with self._sm() as session:
            for payout in await repo.payouts_in_flight(session):
                if payout.status != "sent" or payout.txhash is None:
                    continue  # 'submitting' is handled by reconcile(), not here
                client = self._client_for(payout.asset)
                if client is None:
                    # Defensive: e.g. MNT disabled mid-flight. Leave 'sent' as-is;
                    # a later poll (once re-enabled) resolves it.
                    logger.error(
                        "no chain client for asset -> deferring confirmation poll",
                        pid=payout.id,
                        asset=payout.asset,
                    )
                    continue
                status = await client.get_tx_status(payout.txhash)
                if status == "failed":
                    logger.error(
                        "payout tx reverted on-chain -> failed",
                        pid=payout.id,
                        txhash=payout.txhash,
                    )
                    await repo.finalize_failed(session, payout.id, "reverted")
                    await botui_repo.write_audit(
                        session,
                        actor="executor",
                        action="payout_failed",
                        entity_type="payout",
                        entity_id=str(payout.id),
                    )
                    await notif_repo.enqueue(
                        session,
                        recipient_chat_id=payout.user_id,
                        kind=notif_repo.PAYOUT_FAILED,
                        payload={
                            "net_usdt": str(payout.net_usdt),
                            "asset": payout.asset,
                            "net_native": str(payout.net_native),
                            "reason": "reverted",
                        },
                    )
                    await self._dev_alert(
                        session,
                        "Payout tx reverted on-chain",
                        f"payout #{payout.id}, tx {payout.txhash}",
                    )
                elif status == "success":
                    confs = await client.get_confirmations(payout.txhash)
                    if confs >= self._confirmations:
                        logger.info(
                            "payout confirmed & paid",
                            pid=payout.id,
                            txhash=payout.txhash,
                            confirmations=confs,
                        )
                        await repo.finalize_paid(session, payout.id, now)
                        await botui_repo.write_audit(
                            session,
                            actor="executor",
                            action="payout_paid",
                            entity_type="payout",
                            entity_id=str(payout.id),
                        )
                        gas = payout.gas_usdt or Decimal(0)
                        # amount actually sent = (net - gas) floored to cents (as usdt_to_wei
                        # does) for USDT; MNT never deducts gas, so its sent amount is simply
                        # net_native (the exact figure send_native broadcast, see #2/process_next).
                        sent_usdt = (payout.net_usdt - gas).quantize(
                            Decimal("0.01"), rounding=ROUND_DOWN
                        )
                        await notif_repo.enqueue(
                            session,
                            recipient_chat_id=payout.user_id,
                            kind=notif_repo.PAYOUT_PAID,
                            payload={
                                "net_usdt": str(sent_usdt),
                                "asset": payout.asset,
                                "net_native": str(payout.net_native),
                                "gas_usdt": str(gas),
                                "txhash": payout.txhash,
                            },
                        )
                    else:
                        logger.info(
                            "payout awaiting confirmations",
                            pid=payout.id,
                            confirmations=confs,
                            required=self._confirmations,
                        )
                # pending / not-enough-confs => leave as 'sent'
            await session.commit()

    async def reconcile(self) -> None:
        """Startup reconcile (§6.1): resolve every in-flight payout against the chain
        WITHOUT any blind re-broadcast. A 'submitting' payout whose tx actually landed
        (found by nonce) is promoted to 'sent'; one that never landed is left
        'submitting' (safe to resubmit on the same nonce later — never a new payout).
        Then finalize any 'sent' via poll_confirmations.
        """
        now = self._now()
        async with self._sm() as session:
            in_flight = await repo.payouts_in_flight(session)
        for payout in in_flight:
            if payout.status != "submitting" or payout.nonce is None:
                continue  # nonce is always set by mark_submitting; None is unreachable
            client = self._client_for(payout.asset)
            if client is None:
                # Defensive: e.g. MNT disabled mid-flight. Leave 'submitting' as-is;
                # a later reconcile (once re-enabled) resolves it.
                async with self._sm() as s:
                    await botui_repo.write_audit(
                        s,
                        actor="executor",
                        action="reconcile_no_chain_client",
                        entity_type="payout",
                        entity_id=str(payout.id),
                    )
                    await self._dev_alert(
                        s,
                        "Payout reconcile: no chain client for asset",
                        f"payout #{payout.id}, asset {payout.asset}",
                    )
                    await s.commit()
                continue
            # Did our tx for this nonce actually land on-chain? This check is independent
            # of process memory, so it is correct across a restart. Only when it landed do
            # we trust a persisted/known hash — a hash persisted before a broadcast that
            # never landed (fail-no-land) must NOT promote the payout to 'sent'.
            landed = await client.is_nonce_used(self._wallet, payout.nonce)
            txh = (
                payout.txhash or await client.find_tx_by_nonce(self._wallet, payout.nonce)
                if landed
                else None
            )
            async with self._sm() as s:
                if landed and txh is not None:
                    await repo.mark_sent(s, payout.id, txh, now)
                    await botui_repo.write_audit(
                        s,
                        actor="executor",
                        action="reconcile_found_sent",
                        entity_type="payout",
                        entity_id=str(payout.id),
                    )
                elif landed:
                    # Landed but we have no hash (should not happen now that the hash is
                    # persisted before broadcast) — surface it rather than guess.
                    await botui_repo.write_audit(
                        s,
                        actor="executor",
                        action="reconcile_landed_hash_unknown",
                        entity_type="payout",
                        entity_id=str(payout.id),
                    )
                    await self._dev_alert(
                        s,
                        "Payout landed but txhash unknown",
                        f"payout #{payout.id}, nonce {payout.nonce}",
                    )
                else:
                    # Provably never landed (nonce unused) -> put it back to 'reserved'
                    # for a clean re-send instead of leaving it stuck 'submitting' (which
                    # would block the single-flight queue forever). Safe: is_nonce_used
                    # ruled out a double-spend. process_next re-claims it and, if the
                    # wallet is still underfunded, holds it reserved with a clear alert.
                    await repo.requeue_submitting(s, payout.id)
                    await botui_repo.write_audit(
                        s,
                        actor="executor",
                        action="reconcile_requeued",
                        entity_type="payout",
                        entity_id=str(payout.id),
                    )
                await s.commit()
        await self.poll_confirmations()  # drive any now-'sent' to paid/failed
