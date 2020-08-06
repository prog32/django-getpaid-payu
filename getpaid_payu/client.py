import json
import logging
from copy import deepcopy
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, List, Optional, Union
from urllib.parse import urljoin

from django.core.serializers.json import DjangoJSONEncoder

import pendulum
import requests
from getpaid.exceptions import (
    ChargeFailure,
    CommunicationError,
    CredentialsError,
    GetPaidException,
    LockFailure,
    RefundFailure,
)
from getpaid.types import ItemInfo

from .types import (
    BuyerData,
    CancellationResponse,
    ChargeResponse,
    Currency,
    OrderStatus,
    PaymentResponse,
    ProductData,
    RefundResponse,
    RetrieveOrderInfoResponse,
)

logger = logging.getLogger(__name__)


def ensure_auth(func: Callable) -> Callable:
    @wraps(func)
    def _f(self, *args, **kwargs):
        if self.token_expiration < pendulum.now().add(seconds=-5):
            self._authorize()
        return func(self, *args, **kwargs)

    return _f


class Client:
    last_response = None
    _convertables = {"amount", "total", "available", "unitPrice", "totalAmount"}

    def __init__(
        self,
        api_url: str,
        pos_id: int,
        second_key: str,
        oauth_id: int,
        oauth_secret: str,
    ):
        self.api_url = api_url
        self.pos_id = pos_id
        self.second_key = second_key
        self.oauth_id = oauth_id
        self.oauth_secret = oauth_secret
        self._authorize()

    def _authorize(self):
        url = urljoin(self.api_url, "/pl/standard/user/oauth/authorize")
        self.last_response = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.oauth_id,
                "client_secret": self.oauth_secret,
            },
        )
        if self.last_response.status_code == 200:
            data = self.last_response.json()
            self.token = f"{data['token_type'].capitalize()} {data['access_token']}"
            self.token_expiration = pendulum.now().add(seconds=int(data["expires_in"]))
        else:
            raise CredentialsError(
                "Cannot authenticate.", context={"raw_response": self.last_response}
            )

    def _headers(self, **kwargs):
        data = {"Authorization": self.token, "Content-Type": "application/json"}
        data.update(kwargs)
        return data

    @classmethod
    def _centify(cls, data: Union[ItemInfo, dict, list, Decimal, int, float, str]):
        """
        Traverse through given object and convert all values of 'amount'
        fields and all keys to PayU format.
        :param data: Converted data
        """
        data = deepcopy(data)
        if hasattr(data, "items"):
            return {
                k: str(int(v * 100)) if k in cls._convertables else cls._centify(v)
                for k, v in data.items()
            }
        elif isinstance(data, list):
            return [cls._centify(v) for v in data]
        return data

    @classmethod
    def _normalize(cls, data: Union[ItemInfo, dict, list, Decimal, int, float, str]):
        """
        Traverse through given object and convert all values of 'amount'
        fields to normal and all PayU-specific keys to standard ones.
        :param data: Converted data
        """
        data = deepcopy(data)
        if hasattr(data, "items"):
            return {
                k: Decimal(v) / 100 if k in cls._convertables else cls._normalize(v)
                for k, v in data.items()
            }
        elif isinstance(data, list):
            return [cls._normalize(v) for v in data]
        return data

    @ensure_auth
    def new_order(
        self,
        amount: Union[Decimal, float],
        currency: Currency,
        order_id: Union[str, int],
        description: Optional[str] = None,
        customer_ip: Optional[str] = None,
        buyer: Optional[BuyerData] = None,
        products: Optional[List[ProductData]] = None,
        notify_url: Optional[str] = None,
        continue_url: Optional[str] = None,
        **kwargs,
    ) -> PaymentResponse:
        """
        Register new Order within API.

        :param amount: Payment amount
        :param currency: ISO 4217 currency code
        :param description: Short description of the whole order
        :param customer_ip: IP address of the customer, default: "127.0.0.1"
        :param buyer: Buyer data (see :class:`Buyer`)
        :param products: List of products being bought (see :class:`Product`), defaults to amount + description
        :param notify_url: Callback url
        :param kwargs: Additional params that will first be consumed by headers, with leftovers passed on to order request
        :return: JSON response from API
        """
        url = urljoin(self.api_url, "/api/v2_1/orders")
        data = self._centify(
            {
                "extOrderId": order_id,
                "customerIp": customer_ip if customer_ip else "127.0.0.1",
                "merchantPosId": str(self.pos_id),
                "description": description if description else "Payment order",
                "currencyCode": currency.upper(),
                "totalAmount": amount,
                "products": products
                if products
                else [{"name": "Total order", "unitPrice": amount, "quantity": 1}],
            }
        )

        if notify_url:
            data["notifyUrl"] = notify_url

        if continue_url:
            data["continueUrl"] = continue_url

        if buyer:
            data["buyer"] = buyer

        data["settings"] = {
            "invoiceDisabled": "true"
        }

        headers = self._headers(**kwargs)
        data.update(kwargs)
        encoded = json.dumps(data, cls=DjangoJSONEncoder)

        logger.info(f"PayU request: {encoded}")

        self.last_response = requests.post(
            url, headers=headers, data=encoded, allow_redirects=False
        )
        if self.last_response.status_code in [200, 201, 302]:
            return self._normalize(self.last_response.json())
        raise LockFailure(
            "Error creating order", context={"raw_response": self.last_response}
        )

    @ensure_auth
    def refund(
        self,
        order_id: str,
        amount: Optional[Union[Decimal, float]] = None,
        description: Optional[str] = None,
        **kwargs,
    ) -> RefundResponse:
        url = urljoin(self.api_url, f"/api/v2_1/orders/{order_id}/refunds")
        data = {"description": description if description else "Refund"}
        if amount:
            data["amount"] = amount
        encoded = json.dumps(
            {"refund": self._centify(data), "orderId": order_id}, cls=DjangoJSONEncoder
        )
        self.last_response = requests.post(
            url, headers=self._headers(**kwargs), data=encoded,
        )
        if self.last_response.status_code == 200:
            return self._normalize(self.last_response.json())
        raise RefundFailure(
            "Error creating refund", context={"raw_response": self.last_response}
        )

    @ensure_auth
    def cancel_order(self, order_id: str, **kwargs) -> CancellationResponse:
        url = urljoin(self.api_url, f"/api/v2_1/orders/{order_id}")
        self.last_response = requests.delete(url, headers=self._headers(**kwargs))
        if self.last_response.status_code == 200:
            return self._normalize(self.last_response.json())
        raise GetPaidException(
            "Error cancelling order", context={"raw_response": self.last_response}
        )

    @ensure_auth
    def capture(self, order_id: str, **kwargs) -> ChargeResponse:
        url = urljoin(self.api_url, f"/api/v2_1/orders/{order_id}/status")
        data = {"orderId": order_id, "orderStatus": OrderStatus.COMPLETED}
        self.last_response = requests.put(url, headers=self._headers(**kwargs))
        if self.last_response.status_code == 200:
            return self._normalize(self.last_response.json())
        raise ChargeFailure(
            "Error charging locked payment",
            context={"raw_response": self.last_response},
        )

    @ensure_auth
    def get_order_info(self, order_id: str, **kwargs) -> RetrieveOrderInfoResponse:
        url = urljoin(self.api_url, f"/api/v2_1/orders/{order_id}")
        self.last_response = requests.get(url, headers=self._headers(**kwargs))
        if self.last_response.status_code == 200:
            return self._normalize(self.last_response.json())
        raise CommunicationError(context={"raw_response": self.last_response})

    @ensure_auth
    def get_order_transactions(self, order_id: str, **kwargs):
        raise NotImplementedError

    @ensure_auth
    def get_shop_info(self, shop_id: str, **kwargs):
        """
        Get own shop info

        :param shop_id: Public shop_id
        :param kwargs:
        :return:
        """
        url = urljoin(self.api_url, f"/api/v2_1/shops/{shop_id}")
        self.last_response = requests.get(url, headers=self._headers(**kwargs))
        if self.last_response.status_code == 200:
            return self._normalize(self.last_response.json())
        raise CommunicationError(
            "Error getting shop info", context={"raw_response": self.last_response}
        )

    def get_paymethods(self, lang: Optional[str] = None):
        raise NotImplementedError
