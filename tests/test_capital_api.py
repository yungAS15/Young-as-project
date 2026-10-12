"""Client Capital.com testé avec une session HTTP simulée (aucun appel réseau)."""
import json

import pytest
import requests

from broker.capital_api import CapitalAPIError, CapitalClient, InstrumentNotAllowed

MARKET = {
    "instrument": {"epic": "GOLD", "name": "Gold", "lotSize": 1, "type": "COMMODITIES", "currency": "USD",
                   "marginFactor": 5, "marginFactorUnit": "PERCENTAGE",
                   "openingHours": {"mon": ["00:00 - 21:59", "23:05 - 00:00"], "zone": "UTC"}},
    "dealingRules": {"minStepDistance": {"unit": "POINTS", "value": 0.01},
                     "minDealSize": {"unit": "POINTS", "value": 0.01},
                     "maxDealSize": {"unit": "POINTS", "value": 1000},
                     "minSizeIncrement": {"unit": "POINTS", "value": 0.01},
                     "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.01}},
    "snapshot": {"marketStatus": "TRADEABLE", "bid": 4000.0, "offer": 4000.4, "updateTimeUTC": "2026-10-12T10:00:00.123",
                 "scalingFactor": 1, "decimalPlacesFactor": 2},
}


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode() if body is not None else b""
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeHTTP:
    """Rejoue une file de réponses par (méthode, chemin)."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, method, path, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def _next(self, method, url, **kw):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, kw))
        q = self.routes.get((method, path))
        if not q:
            raise AssertionError(f"Appel inattendu {method} {path}")
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def request(self, method, url, **kw):
        return self._next(method, url, **kw)


LOGIN_OK = Resp(200, {"currencyIsoCode": "CHF", "currentAccountId": "ACC123456"},
                {"CST": "cst-secret-token", "X-SECURITY-TOKEN": "xsec-secret-token"})


def client(http, **kw):
    return CapitalClient("https://demo-api-capital.backend-capital.com", "APIKEY-SECRET", "me@mail.com",
                         "PASSWORD-SECRET", http=http, sleep=lambda s: None, **kw)


def test_login_sets_tokens_and_account_currency():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    c = client(http)
    info = c.login()
    assert info["account_currency"] == "CHF"
    assert c.connected
    _, _, kw = http.calls[0]
    assert kw["headers"]["X-CAP-API-KEY"] == "APIKEY-SECRET"


def test_login_failure_does_not_leak_secrets():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", Resp(401, {"errorCode": "error.invalid.details PASSWORD-SECRET APIKEY-SECRET"}))
    c = client(http)
    with pytest.raises(CapitalAPIError) as ei:
        c.login()
    msg = str(ei.value)
    assert "PASSWORD-SECRET" not in msg and "APIKEY-SECRET" not in msg
    assert c.consecutive_errors == 1


def test_missing_credentials():
    c = CapitalClient("https://x.com", "", "", "", http=FakeHTTP(), sleep=lambda s: None)
    with pytest.raises(CapitalAPIError):
        c.login()


def test_instrument_parsing_reads_dealing_rules():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("GET", "/api/v1/markets/GOLD", Resp(200, MARKET))
    inst = client(http).get_instrument("GOLD")
    assert inst.min_deal_size == 0.01 and inst.size_increment == 0.01 and inst.max_deal_size == 1000
    assert inst.currency == "USD" and inst.lot_size == 1
    assert inst.min_stop_distance == pytest.approx(4000.4 * 0.01 / 100)
    assert inst.tradeable and inst.ambiguities() == []


def test_incomplete_instrument_spec_raises():
    bad = json.loads(json.dumps(MARKET))
    del bad["dealingRules"]["minDealSize"]
    with pytest.raises(CapitalAPIError):
        CapitalClient.parse_instrument(bad)


def test_allowlist_blocks_other_epics():
    c = client(FakeHTTP())
    with pytest.raises(InstrumentNotAllowed):
        c.get_instrument("SILVER")
    with pytest.raises(InstrumentNotAllowed):
        c.open_position("BTCUSD", "BUY", 1, 1, 2)


def test_session_expiry_relogin_on_401():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("GET", "/api/v1/positions", Resp(401, {"errorCode": "error.invalid.session.token"}),
             Resp(200, {"positions": []}))
    c = client(http)
    assert c.get_positions() == []
    assert [x[1] for x in http.calls].count("/api/v1/session") == 2


def test_retry_on_network_error_for_reads():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("GET", "/api/v1/positions", requests.ConnectionError("boom"), Resp(200, {"positions": []}))
    assert client(http).get_positions() == []


def test_no_retry_on_order_network_error():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("POST", "/api/v1/positions", requests.Timeout("timeout"), Resp(200, {"dealReference": "o_1"}))
    c = client(http)
    with pytest.raises(CapitalAPIError):
        c.open_position("GOLD", "BUY", 0.5, 3990.0, 4020.0)
    assert [x[1] for x in http.calls].count("/api/v1/positions") == 1   # un seul envoi


def test_consecutive_errors_counted_and_reset():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("GET", "/api/v1/markets/GOLD", Resp(400, {"errorCode": "error.bad"}), Resp(400, {"errorCode": "x"}),
             Resp(200, MARKET))
    c = client(http)
    for _ in range(2):
        with pytest.raises(CapitalAPIError):
            c.get_instrument("GOLD")
    assert c.consecutive_errors == 2
    c.get_instrument("GOLD")
    assert c.consecutive_errors == 0


def test_order_includes_sl_and_tp_and_confirm():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("POST", "/api/v1/positions", Resp(200, {"dealReference": "o_abc"}))
    http.add("GET", "/api/v1/confirms/o_abc", Resp(200, {"dealStatus": "ACCEPTED", "status": "OPEN", "dealId": "D1",
                                                         "affectedDeals": [{"dealId": "D1", "status": "OPENED"}],
                                                         "level": 4000.4, "size": 0.5, "direction": "BUY",
                                                         "epic": "GOLD"}))
    c = client(http)
    ref = c.open_position("GOLD", "BUY", 0.5, 3990.0, 4020.0)
    body = [kw for m, p, kw in http.calls if p == "/api/v1/positions"][0]["json"]
    assert body["stopLevel"] == 3990.0 and body["profitLevel"] == 4020.0 and body["direction"] == "BUY"
    conf = c.confirm(ref)
    assert conf.accepted and conf.affected_deals[0]["dealId"] == "D1"


def test_rejected_confirmation():
    http = FakeHTTP()
    http.add("POST", "/api/v1/session", LOGIN_OK)
    http.add("GET", "/api/v1/confirms/o_x", Resp(200, {"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS"}))
    conf = client(http).confirm("o_x")
    assert not conf.accepted and conf.reason == "INSUFFICIENT_FUNDS"


def test_parse_prices_bid_and_spread():
    data = {"prices": [{"snapshotTimeUTC": "2026-10-12T10:00:00", "openPrice": {"bid": 1, "ask": 1.4},
                        "highPrice": {"bid": 2, "ask": 2.4}, "lowPrice": {"bid": 0.5, "ask": 0.9},
                        "closePrice": {"bid": 1.5, "ask": 1.9}, "lastTradedVolume": 42}]}
    cs = CapitalClient.parse_prices(data)
    assert cs[0].close == 1.5 and cs[0].spread == pytest.approx(0.4) and cs[0].volume == 42
    assert cs[0].time.tzinfo is not None
