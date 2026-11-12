"""pytest tests/test_coupons.py — coupon rules (no Firebase)."""

from datetime import datetime, timedelta, timezone

import pytest

from services.coupon_service import (
    CODE_RE, CouponError, CouponService, decide_redeem, generate_code, normalize,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
ACTIVE = {"active": True, "duration_days": 30, "max_redemptions": 5, "redeemed_count": 0,
          "expires_at": NOW + timedelta(days=10)}


def code_of(fn, *a):
    with pytest.raises(CouponError) as e:
        fn(*a)
    return e.value.code


def test_redeem_from_now():
    assert decide_redeem(ACTIVE, False, None, NOW) == NOW + timedelta(days=30)


def test_redeem_stacks_on_existing_coupon():
    current = NOW + timedelta(days=5)
    assert decide_redeem(ACTIVE, False, current, NOW) == current + timedelta(days=30)


def test_redeem_ignores_expired_coupon_until():
    assert decide_redeem(ACTIVE, False, NOW - timedelta(days=5), NOW) == NOW + timedelta(days=30)


@pytest.mark.parametrize("coupon,already,expected", [
    (None, False, "COUPON_INVALID"),
    ({**ACTIVE, "active": False}, False, "COUPON_INVALID"),
    ({**ACTIVE, "expires_at": NOW - timedelta(seconds=1)}, False, "COUPON_EXPIRED"),
    (ACTIVE, True, "COUPON_ALREADY_REDEEMED"),
    ({**ACTIVE, "redeemed_count": 5}, False, "COUPON_USED_UP"),
])
def test_redeem_rejections(coupon, already, expected):
    assert code_of(decide_redeem, coupon, already, None, NOW) == expected


def test_unlimited_and_no_expiry():
    c = {**ACTIVE, "max_redemptions": None, "redeemed_count": 999, "expires_at": None}
    assert decide_redeem(c, False, None, NOW) == NOW + timedelta(days=30)


def test_codes():
    for _ in range(20):
        c = generate_code()
        assert CODE_RE.match(c) and c.startswith("SBC-") and not set("01OI") & set(c[4:])
    assert normalize(" sbc-abcd efgh ") == "SBC-ABCDEFGH"


def test_rate_limit():
    t = [0.0]
    svc = CouponService(access_svc=None, db=object(), clock=lambda: t[0])
    for _ in range(5):
        svc._check_rate("u")
    assert code_of(svc._check_rate, "u") == "COUPON_RATE_LIMITED"
    t[0] = 3601
    svc._check_rate("u")


def test_no_db():
    svc = CouponService(access_svc=None)
    assert code_of(svc.redeem, "u", "SBC-AAAA-BBBB") == "COUPONS_UNAVAILABLE"


def test_create_validation():
    svc = CouponService(access_svc=None, db=object())
    assert code_of(svc.create, "a", 0) == "INVALID_DURATION"
    assert code_of(svc.create, "a", 30, 0) == "INVALID_MAX"
    assert code_of(svc.create, "a", 30, 1, 0) == "INVALID_EXPIRY"
