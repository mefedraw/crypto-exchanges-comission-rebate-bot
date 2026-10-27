"""verify_referral tests for every adapter + the fail-open helper (no live calls).

Membership is amount-independent. Hard-reject adapters (Gate/KuCoin/Bitget/BitMart/
WEEX/Bybit/MEXC) return NOT_REFERRAL on an authoritative negative; OKX stays soft
(its keyword filter is ignored server-side, so a miss -> UNVERIFIABLE), so a genuine
referral is never wrongly blocked there.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from bot.config import SPEC_BY_CODE, ExchangeCredentials
from bot.exchanges.base import ExchangeApiError, ReferralCheck
from bot.exchanges.bitget import BitgetAdapter
from bot.exchanges.bitmart import BitmartAdapter
from bot.exchanges.bybit import BybitAdapter
from bot.exchanges.gate import GateAdapter
from bot.exchanges.kucoin import KucoinAdapter
from bot.exchanges.mexc import MexcAdapter
from bot.exchanges.okx import OkxAdapter
from bot.exchanges.weex import WeexAdapter
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _creds(code: str, *, passphrase: bool = False) -> ExchangeCredentials:
    return ExchangeCredentials(
        SPEC_BY_CODE[code],
        SecretStr("api-key"),
        SecretStr("api-secret"),
        SecretStr("pass") if passphrase else None,
    )


# ---------------------------------------------------------------- Gate (hard)
async def test_gate_confirms_when_in_referral_graph(httpx_mock):
    httpx_mock.add_response(json={"list": [{"uid": 123, "belong": "partnerX", "type": 3}]})
    adapter = GateAdapter(_creds("gate"), make_settings())
    assert await adapter.verify_referral("123") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_gate_rejects_when_not_in_network(httpx_mock):
    # Present in Gate but belongs to no partner (type 0) -> authoritative non-referral.
    httpx_mock.add_response(json={"list": [{"uid": 123, "belong": "", "type": 0}]})
    adapter = GateAdapter(_creds("gate"), make_settings())
    assert await adapter.verify_referral("123") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


async def test_gate_rejects_when_absent(httpx_mock):
    httpx_mock.add_response(json={"list": []})
    adapter = GateAdapter(_creds("gate"), make_settings())
    assert await adapter.verify_referral("123") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


# -------------------------------------------------------------- KuCoin (hard)
async def test_kucoin_confirms_invitee(httpx_mock):
    httpx_mock.add_response(json={"code": "200000", "data": {"items": [{"uid": "555"}]}})
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    assert await adapter.verify_referral("555") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_kucoin_rejects_when_absent(httpx_mock):
    httpx_mock.add_response(json={"code": "200000", "data": {"items": []}})
    adapter = KucoinAdapter(_creds("kucoin", passphrase=True), make_settings())
    assert await adapter.verify_referral("555") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


# -------------------------------------------------------------- Bitget (hard)
async def test_bitget_confirms_customer_and_signs_body(httpx_mock):
    httpx_mock.add_response(json={"code": "00000", "data": [{"uid": "777", "registerTime": "1"}]})
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    assert await adapter.verify_referral("777") is ReferralCheck.CONFIRMED
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert request.read() == b'{"uid":"777"}'  # exact body that was signed
    assert request.headers["ACCESS-SIGN"]
    await adapter.aclose()


async def test_bitget_rejects_when_absent(httpx_mock):
    httpx_mock.add_response(json={"code": "00000", "data": []})
    adapter = BitgetAdapter(_creds("bitget", passphrase=True), make_settings())
    assert await adapter.verify_referral("777") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


# ------------------------------------------------------------- BitMart (hard)
async def test_bitmart_confirms_via_boolean(httpx_mock):
    httpx_mock.add_response(json={"code": "1000", "message": "OK", "data": {"isInviteUser": True}})
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    assert await adapter.verify_referral("42") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_bitmart_rejects_via_boolean(httpx_mock):
    httpx_mock.add_response(json={"code": "1000", "message": "OK", "data": {"isInviteUser": False}})
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    assert await adapter.verify_referral("42") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


async def test_bitmart_rejects_unknown_user_40025(httpx_mock):
    # VERIFIED (live): an unknown cid -> code 40025 "User does not exist" (HTTP 200) -> reject.
    httpx_mock.add_response(
        json={
            "code": 40025,
            "errno": "invalidUserId",
            "msg": "User does not exist",
            "success": False,
        }
    )
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    assert await adapter.verify_referral("3066862172") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


async def test_bitmart_unverifiable_on_other_business_code(httpx_mock):
    # An auth/other business code fails open (never a false reject).
    httpx_mock.add_response(json={"code": "30001", "message": "signature error", "success": False})
    adapter = BitmartAdapter(_creds("bitmart"), make_settings())
    assert await adapter.verify_referral("42") is ReferralCheck.UNVERIFIABLE
    await adapter.aclose()


# ---------------------------------------------------------------- Bybit (hard)
async def test_bybit_confirms_via_customer_info(httpx_mock):
    # aff-customer-info retCode 0 => the uid is the affiliate's referral.
    httpx_mock.add_response(json={"retCode": 0, "retMsg": "", "result": {"uid": "570426167"}})
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    assert await adapter.verify_referral("570426167") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_bybit_rejects_no_relation_141024(httpx_mock):
    # VERIFIED (live): a stranger uid -> retCode 141024 "no relation with affiliate".
    httpx_mock.add_response(
        json={"retCode": 141024, "retMsg": "no relation with affiliate", "result": {}}
    )
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    assert await adapter.verify_referral("100000000000") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


async def test_bybit_unverifiable_on_other_code(httpx_mock):
    httpx_mock.add_response(json={"retCode": 10001, "retMsg": "params error", "result": {}})
    adapter = BybitAdapter(_creds("bybit"), make_settings())
    assert await adapter.verify_referral("570426167") is ReferralCheck.UNVERIFIABLE
    await adapter.aclose()


# ------------------------------------------------------------------ OKX (soft)
async def test_okx_confirms_when_in_invitee_list(httpx_mock):
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "888"}]})
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    assert await adapter.verify_referral("888") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_okx_unverifiable_when_absent(httpx_mock):
    httpx_mock.add_response(json={"code": "0", "data": [{"uid": "999"}]})
    adapter = OkxAdapter(_creds("okx", passphrase=True), make_settings())
    assert await adapter.verify_referral("888") is ReferralCheck.UNVERIFIABLE
    await adapter.aclose()


# ----------------------------------------------------------------- MEXC (hard)
async def test_mexc_confirms_on_success(httpx_mock):
    httpx_mock.add_response(
        json={
            "success": True,
            "code": 0,
            "data": {"totalPage": 1, "resultList": [{"uid": "43305891"}]},
        }
    )
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    assert await adapter.verify_referral("43305891") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_mexc_rejects_code_1035(httpx_mock):
    # VERIFIED (live): a non-referral uid -> code 1035 (success:false, data:null).
    httpx_mock.add_response(json={"success": False, "code": 1035, "message": None, "data": None})
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    assert await adapter.verify_referral("100000000000") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


async def test_mexc_unverifiable_on_other_code(httpx_mock):
    httpx_mock.add_response(json={"success": False, "code": 700007, "message": "sig", "data": None})
    adapter = MexcAdapter(_creds("mexc"), make_settings())
    assert await adapter.verify_referral("43305891") is ReferralCheck.UNVERIFIABLE
    await adapter.aclose()


# ----------------------------------------------------------------- WEEX (hard)
async def test_weex_confirms_on_http_200(httpx_mock):
    # A uid-filtered 200 means WEEX accepted the uid as a partner referral.
    httpx_mock.add_response(
        json={
            "pages": 1,
            "total": 1,
            "channelUserInfoItemList": [{"uid": "8357587701", "registerTime": 1783940963000}],
        }
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    assert await adapter.verify_referral("8357587701") is ReferralCheck.CONFIRMED
    await adapter.aclose()


async def test_weex_rejects_non_partner_code_1054(httpx_mock):
    # VERIFIED (live): a non-referral uid returns code -1054 as HTTP 500 -> hard reject.
    httpx_mock.add_response(
        status_code=500, json={"code": -1054, "msg": "The UID is not part of the partner system"}
    )
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    assert await adapter.verify_referral("3066862172") is ReferralCheck.NOT_REFERRAL
    await adapter.aclose()


async def test_weex_unverifiable_on_other_error(httpx_mock):
    # A genuine server error (no -1054) fails open, never a false reject.
    httpx_mock.add_response(status_code=503, json={"code": -9999, "msg": "service unavailable"})
    adapter = WeexAdapter(_creds("weex", passphrase=True), make_settings())
    assert await adapter.verify_referral("3066862172") is ReferralCheck.UNVERIFIABLE
    await adapter.aclose()


# ----------------------------------------------------- fail-open helper (botui)
async def test_helper_unverifiable_when_no_keys():
    # No credentials configured -> build_adapter returns None -> UNVERIFIABLE.
    from bot.botui import referral

    assert (
        await referral.verify_referral(make_settings(), "gate", "1") is ReferralCheck.UNVERIFIABLE
    )


async def test_helper_fails_open_on_api_error(monkeypatch):
    from bot.botui import referral

    closed = {"n": 0}

    class _Boom:
        async def verify_referral(self, uid: str) -> ReferralCheck:
            raise ExchangeApiError("boom")

        async def aclose(self) -> None:
            closed["n"] += 1

    monkeypatch.setattr(referral, "build_adapter", lambda settings, code: _Boom())
    result = await referral.verify_referral(make_settings(), "gate", "1")
    assert result is ReferralCheck.UNVERIFIABLE
    assert closed["n"] == 1  # adapter always closed
