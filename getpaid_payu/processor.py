""""
Settings:
    pos_id
    second_key
    client_id
    client_secret
"""
import hashlib
import json
import logging
from collections import OrderedDict
from urllib.parse import urljoin

from django import http
from django.db.transaction import atomic
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.utils.http import urlencode

from django_fsm import can_proceed
from getpaid.adapter import get_order_adapter
from getpaid.exceptions import LockFailure
from getpaid.post_forms import PaymentHiddenInputsPostForm
from getpaid.processor import BaseProcessor
from getpaid.types import BackendMethod as bm
from getpaid.types import PaymentStatusResponse

from .client import Client
from .types import Currency, OrderStatus, RefundStatus, ResponseStatus

logger = logging.getLogger(__name__)


class PaymentProcessor(BaseProcessor):
    slug = "payu"
    display_name = "PayU"
    accepted_currencies = [c.value for c in Currency]
    ok_statuses = [200, 201, 302]
    method = "REST"  #: Supported modes: REST, POST (not recommended!)
    sandbox_url = "https://secure.snd.payu.com/"
    production_url = "https://secure.payu.com/"
    confirmation_method = "PUSH"  #: PUSH - paywall will send POST request to your server; PULL - you need to check the payment status
    post_form_class = PaymentHiddenInputsPostForm
    post_template_name = "getpaid_payu/payment_post_form.html"
    client_class = Client
    _token = None
    _token_expires = None

    # Specifics

    def validate_config(self, config):
        pass
        """
        validate config, raise exception on error e.g.
        """
        keys = [
            'pos_id',
            'second_key',
            'oauth_id',
            'oauth_secret',
        ]
        for key in keys:
            if not config.get(key, None):
                raise ImproperlyConfigured(
                    "Invalid config GETPAID_BACKEND_SETTINGS[{}][]".format(
                        self.path, key
                    )
                )

    # def get_our_baseurl(self, request):
    #     if request is None:
    #         raise Exception("Missing request")
    #     return super().get_our_baseurl(request)

    def get_client_params(self) -> dict:
        return {
            "api_url": self.get_paywall_baseurl(),
            "pos_id": self.get_setting("pos_id"),
            "second_key": self.get_setting("second_key"),
            "oauth_id": self.get_setting("oauth_id"),
            "oauth_secret": self.get_setting("oauth_secret"),
        }

    def prepare_form_data(self, post_data):
        pos_id = self.get_setting("pos_id")
        second_key = self.get_setting("second_key")
        algorithm = self.get_setting("algorithm", "SHA-256").upper()
        hasher = getattr(hashlib, algorithm.replace("-", "").lower())
        encoded = urlencode(OrderedDict(sorted(post_data.items())))
        prepared = f"{encoded}&{second_key}".encode("ascii")
        signature = hasher(prepared).hexdigest()
        post_data[
            "OpenPayu-Signature"
        ] = f"signature={signature};algorithm={algorithm};sender={pos_id}"
        return post_data

    # Helper methods
    def get_buyer_info(self):
        # Map user info to PayU buuyer info
        # TODO: how to handle missing data?
        order_adapter = get_order_adapter(self.payment.order)
        user_info = order_adapter.get_user_info()

        # http://developers.payu.com/pl/restapi.html#creating_order_buyer_section_description

        if not user_info.get('email', None):
            return None

        payu_user_info = {
            "email": user_info['email'],
        }

        # Set optional keys
        keys = (
            ("phone", 'phone'),
            ("firstName", 'first_name'),
            ("last_name", 'last_name'),
            ("language", 'language'),
            # ('', 'nin') # PESEL lub zagraniczny ekwiwalent
            # ('', 'extCustomerId') # Identyfikator kupującego używany w systemie klienta
            # ('', 'customerId') # Id kupującego

        )
        for src_key, dst_key in keys:
            if user_info.get(src_key, None):
                payu_user_info[dst_key] = user_info[src_key]

        # data["buyer"] = {
        #     "email": "john.doe@example.com",
        #     "phone": "654111654",
        #     "firstName": "John",
        #     "lastName": "Doe",
        #     "language": "pl"
        # }
        return payu_user_info


    def get_return_url(self, request=None):
        return self.get_full_url(
            self.payment.get_return_url(),
            request=request,
        )


    def get_paywall_context(self, request=None, camelize_keys=False, **kwargs):
        # TODO: configurable buyer info inclusion
        """
        "buyer" is optional
        :param request: request creating the payment
        :return: dict that unpacked will be accepted by :meth:`Client.new_order`
        """

        # our_baseurl = self.get_our_baseurl(request)
        key_trans = {
            "unit_price": "unitPrice",
            "first_name": "firstName",
            "last_name": "lastName",
            "order_id": "extOrderId",
            "customer_ip": "customerIp",
            "notify_url": "notifyUrl",
            "continue_url": "continueUrl",
        }
        raw_products = self.payment.get_items()
        products = [
            {key_trans.get(k, k): v for k, v in product.items()}
            for product in raw_products
        ]

        context = {
            "order_id": self.payment.get_unique_id(),
            "customer_ip": self.get_real_ip(request),
            "description": self.payment.description,
            "currency": self.payment.currency,
            "amount": self.payment.amount_required,
            "products": products,
            "buyer": self.get_buyer_info(),
            "continue_url": self.get_return_url(request=request)
        }
        if self.get_setting("confirmation_method", self.confirmation_method) == "PUSH":
            context["notify_url"] = self.get_callback_url(
                self.payment, request=request
            )
        if camelize_keys:
            return {key_trans.get(k, k): v for k, v in context.items()}
        return context

    def get_paywall_method(self):
        return self.get_setting("paywall_method", self.method)

    # Communication with paywall

    @atomic()
    def prepare_transaction(self, request=None, view=None, **kwargs):
        method = self.get_paywall_method().upper()
        if method == bm.REST:
            try:
                results = self.prepare_lock(request=request, **kwargs)
                response = http.HttpResponseRedirect(results["url"])
            except LockFailure as exc:
                logger.error(exc, extra=getattr(exc, "context", None))
                self.payment.fail()
                response = http.HttpResponseRedirect(
                    self.get_failure_url(
                        self.payment, request=request
                    )
                )
            self.payment.save()
            return response
        elif method == bm.POST:
            data = self.get_paywall_context(
                request=request, camelize_keys=True, **kwargs
            )
            data["merchantPosId"] = self.get_setting("pos_id")
            url = self.get_main_url()
            form = self.get_form(data)
            return TemplateResponse(
                request=request,
                template=self.get_template_names(view=view),
                context={"form": form, "paywall_url": url},
            )

    def handle_paywall_callback(self, request, **kwargs):

        payu_header_raw = request.headers.get(
            "Openpayu-Signature"
        ) or request.headers.get("X-Openpayu-Signature", "")

        if not payu_header_raw:
            logger.warning("PayU callback: no signature")
            logger.warning("PayU callback: no signature, msg: {}".format(
                request.body.decode()
            ))

            return HttpResponse("NO SIGNATURE", status=400)
        payu_header = {
            k: v for k, v in [i.split("=") for i in payu_header_raw.split(";")]
        }
        algo_name = payu_header.get("algorithm", "MD5")
        signature = payu_header.get("signature")
        second_key = self.get_setting("second_key")
        algorithm = getattr(hashlib, algo_name.replace("-", "").lower())

        body = request.body.decode()
        logger.warning(f"PayU callback: {body}")
        expected_signature = algorithm(
            f"{body}{second_key}".encode("utf-8")
        ).hexdigest()

        if expected_signature == signature:
            data = json.loads(body)

            if "order" in data:
                order_data = data.get("order")
                status = order_data.get("status")
                if status == OrderStatus.COMPLETED:
                    if can_proceed(self.payment.confirm_payment):
                        self.payment.confirm_payment()
                        if can_proceed(self.payment.mark_as_paid):
                            self.payment.mark_as_paid()
                    else:
                        logger.debug(
                            "Cannot confirm payment",
                            extra={
                                "payment_id": self.payment.id,
                                "payment_status": self.payment.status,
                            },
                        )
                elif status == OrderStatus.CANCELED:
                    self.payment.fail()
                elif status == OrderStatus.WAITING_FOR_CONFIRMATION:
                    if can_proceed(self.payment.confirm_lock):
                        self.payment.confirm_lock()
                    else:
                        logger.debug(
                            "Already locked",
                            extra={
                                "payment_id": self.payment.id,
                                "payment_status": self.payment.status,
                            },
                        )
            elif "refund" in data:
                refund_data = data.get("refund")
                status = refund_data.get("status")
                if status == RefundStatus.FINALIZED:
                    amount = refund_data.get("amount") / 100
                    self.payment.confirm_refund(amount)
                    if can_proceed(self.payment.mark_as_refunded):
                        self.payment.mark_as_refunded()
                elif status == RefundStatus.CANCELED:
                    self.payment.cancel_refund()
                    if can_proceed(self.payment.mark_as_paid):
                        self.payment.mark_as_paid()
            self.payment.save()
            return HttpResponse("OK")
        else:
            logger.error(
                f"Received bad signature for payment {self.payment.id}! "
                f"Got '{signature}', expected '{expected_signature}'"
            )
            return HttpResponse(
                "BAD SIGNATURE", status=422
            )  # https://httpstatuses.com/422

    def fetch_payment_status(self) -> PaymentStatusResponse:
        response = self.client.get_order_info(self.payment.external_id)
        results = {"raw_response": self.client.last_response}
        order_data = response.get("orders", [None])[0]

        status = order_data.get("status")
        if status == OrderStatus.NEW:
            results["callback"] = "confirm_prepared"
        elif status == OrderStatus.PENDING:
            results["callback"] = "confirm_prepared"
        elif status == OrderStatus.CANCELED:
            results["callback"] = "fail"
        elif status == OrderStatus.COMPLETED:
            results["callback"] = "confirm_payment"
        elif status == OrderStatus.WAITING_FOR_CONFIRMATION:
            results["callback"] = "confirm_lock"
        return results

    def get_main_url(self, data=None) -> str:
        baseurl = self.get_paywall_baseurl()
        return urljoin(baseurl, "/api/v2_1/orders")

    def prepare_lock(self, request=None, **kwargs):
        results = {}
        params = self.get_paywall_context(request=request, **kwargs)
        logger.info("PayU requested: {}".format(params))
        response = self.client.new_order(**params)
        results["raw_response"] = self.client.last_response
        results["url"] = response.get("redirectUri")
        self.payment.confirm_prepared()
        self.payment.external_id = results["ext_order_id"] = response.get("orderId", "")
        return results

    def charge(self, **kwargs):
        response = self.client.capture(self.payment.external_id)
        result = {
            "raw_response": self.client.last_response,
            "status_desc": response.get("status", {}).get("statusDesc"),
        }
        if response.get("status", {}).get("statusCode") == ResponseStatus.SUCCESS:
            result["success"] = True

        return result

    def release_lock(self):
        response = self.client.cancel_order(self.payment.external_id)
        status = response.get("status", {}).get("statusCode")
        if status == ResponseStatus.SUCCESS:
            return self.payment.amount_locked
