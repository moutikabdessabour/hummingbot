import asyncio
import json
import unittest
from collections import Awaitable
from decimal import Decimal
from unittest.mock import patch

import aiohttp
from aioresponses import aioresponses

from hummingbot.client.config.global_config_map import global_config_map
from hummingbot.connector.gateway_EVM_AMM import GatewayEVMAMM
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.event.event_logger import EventLogger

from hummingbot.core.event.events import MarketEvent, TokenApprovalEvent


class GatewayEVMAMMTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = "COINALHPA"
        self.quote = "HBOT"
        self.trading_pair = f"{self.base}-{self.quote}"
        self.wallet_key = "someWalletKey"
        self.gateway_host = "gtw_host"
        self.gateway_port = 123
        global_config_map["gateway_api_host"].value = self.gateway_host
        global_config_map["gateway_api_port"].value = self.gateway_port
        self.connector = GatewayEVMAMM(
            connector_name="uniswap",
            chain="ethereum",
            network="mainnet",
            wallet_address="0xABCD....1234",
            trading_pairs=[self.trading_pair],
        )
        self.ev_loop = asyncio.get_event_loop()
        self._initialize_event_loggers()

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def _initialize_event_loggers(self):
        self.buy_order_completed_logger = EventLogger()
        self.order_filled_logger = EventLogger()
        self.order_failure_logger = EventLogger()
        self.approval_successful_logger = EventLogger()
        self.approval_failed_logger = EventLogger()

        events_and_loggers = [
            (MarketEvent.BuyOrderCompleted, self.buy_order_completed_logger),
            (MarketEvent.OrderFilled, self.order_filled_logger),
            (MarketEvent.OrderFailure, self.order_failure_logger),
            (TokenApprovalEvent.ApprovalSuccessful, self.approval_successful_logger),
            (TokenApprovalEvent.ApprovalFailed, self.approval_failed_logger),
        ]

        for event, logger in events_and_loggers:
            self.connector.add_listener(event, logger)

    @aioresponses()
    @patch("hummingbot.core.gateway.gateway_http_client.GatewayHttpClient._http_client")
    def test_get_quote_price_updates_fee_overrides_config_map(self, mocked_api, _http_client_mock):
        _http_client_mock.return_value = aiohttp.ClientSession()
        url = f"https://{self.gateway_host}:{self.gateway_port}/amm/price"
        mock_response = {
            "price": 10,
            "gasLimit": 30000,
            "gasPrice": 1,
            "gasCost": 2,
            "swaps": [],
        }
        mocked_api.post(url, body=json.dumps(mock_response))

        self.connector._account_balances = {"ETH": Decimal("10000")}
        self.connector._allowances = {self.quote: Decimal("10000")}

        res = self.async_run_with_timeout(
            self.connector.get_quote_price(self.trading_pair, is_buy=True, amount=Decimal("2"))
        )

        self.assertEqual(Decimal("10"), res)

    @aioresponses()
    @patch("hummingbot.core.gateway.gateway_http_client.GatewayHttpClient._http_client")
    def test_update_token_approval_status(self, mocked_api, mocked_http_client):
        mocked_http_client.return_value = aiohttp.ClientSession()

        url = f"https://{self.gateway_host}:{self.gateway_port}/network/poll"
        approval_id = self.connector.create_approval_order_id(self.trading_pair)

        self.connector.start_tracking_order(approval_id, trading_pair=self.trading_pair)
        self.connector._in_flight_orders[approval_id].update_exchange_order_id("approval0")
        api_resp = {"txHash": "someTxHash", "txStatus": 1, "txReceipt": {"status": 1}}
        mocked_api.post(url, body=json.dumps(api_resp))

        self.async_run_with_timeout(
            self.connector._update_token_approval_status(self.connector.approval_orders), 10000
        )

        self.assertEqual(1, len(self.approval_successful_logger.event_log))
        self.assertEqual(0, len(self.approval_failed_logger.event_log))
        self.assertEqual(0, len(self.connector.approval_orders))

        self.connector.start_tracking_order(approval_id, trading_pair=self.trading_pair)
        self.connector._in_flight_orders[approval_id].update_exchange_order_id("approval1")
        api_resp = {"txHash": "someTxHash", "txStatus": 1, "txReceipt": {"status": -1}}
        mocked_api.post(url, body=json.dumps(api_resp))

        self.async_run_with_timeout(
            self.connector._update_token_approval_status(self.connector.approval_orders), 10000
        )

        self.assertEqual(1, len(self.approval_successful_logger.event_log))
        self.assertEqual(1, len(self.approval_failed_logger.event_log))
        self.assertEqual(0, len(self.connector.approval_orders))

        self.connector.start_tracking_order(approval_id, trading_pair=self.trading_pair)
        self.connector._in_flight_orders[approval_id].update_exchange_order_id("approval1")
        api_resp = {"txHash": "someTxHash", "txStatus": -1}
        mocked_api.post(url, body=json.dumps(api_resp))

        self.async_run_with_timeout(
            self.connector._update_token_approval_status(self.connector.approval_orders), 10000
        )

        self.assertEqual(1, len(self.approval_successful_logger.event_log))
        self.assertEqual(2, len(self.approval_failed_logger.event_log))
        self.assertEqual(0, len(self.connector.approval_orders))

    @aioresponses()
    @patch("hummingbot.core.gateway.gateway_http_client.GatewayHttpClient._http_client")
    def test_update_order_status(self, mocked_api, mocked_http_client):
        mocked_http_client.return_value = aiohttp.ClientSession()

        url = f"https://{self.gateway_host}:{self.gateway_port}/network/poll"
        trade_type = TradeType.BUY

        order_id = self.connector.create_market_order_id(trade_type, self.trading_pair)
        self.connector.start_tracking_order(
            order_id, trading_pair=self.trading_pair, trade_type=trade_type, price=Decimal("1"), amount=Decimal("2")
        )
        self.connector._in_flight_orders[order_id].update_exchange_order_id("anotherExchangeId0")
        api_resp = {"txHash": "someTxHash", "txStatus": 1, "txReceipt": {"status": 1, "gasUsed": 2}}
        mocked_api.post(url, body=json.dumps(api_resp))

        order_id = self.connector.create_market_order_id(trade_type, self.trading_pair)
        self.connector.start_tracking_order(
            order_id, trading_pair=self.trading_pair, trade_type=trade_type, price=Decimal("1"), amount=Decimal("2")
        )
        self.connector._in_flight_orders[order_id].update_exchange_order_id("anotherExchangeId1")
        api_resp = {"txHash": "someTxHash", "txStatus": 1, "txReceipt": {"status": -1}}
        mocked_api.post(url, body=json.dumps(api_resp))

        order_id = self.connector.create_market_order_id(trade_type, self.trading_pair)
        self.connector.start_tracking_order(
            order_id, trading_pair=self.trading_pair, trade_type=trade_type, price=Decimal("1"), amount=Decimal("2")
        )
        self.connector._in_flight_orders[order_id].update_exchange_order_id("anotherExchangeId2")
        api_resp = {"txHash": "someTxHash", "txStatus": -1}
        mocked_api.post(url, body=json.dumps(api_resp))

        self.async_run_with_timeout(self.connector.update_order_status(self.connector.amm_orders))

        self.assertEqual(1, len(self.buy_order_completed_logger.event_log))
        self.assertEqual(1, len(self.order_filled_logger.event_log))
        self.assertEqual(2, len(self.order_failure_logger.event_log))
