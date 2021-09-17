import json
import logging

import swapper
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .processor import PaymentProcessor

logger = logging.getLogger(__name__)



@method_decorator(csrf_exempt, name='dispatch')
class CallbackView(View):
    """
    Dedicated callback view, since payNow does not support dynamic callback urls.
    """

    def post(self, request, *args, **kwargs):
        # async request.json()
        # external_id = json.loads(request.data).get("paymentId")
        # json.loads(request.body.decode("utf-8"))
        print(f"request {request}")
        print(f"request.body {request.body}")

        logger.error(f"request {request}")
        logger.error(f"request.body {request.body}")

        # logger.error("request.POST", request.POST)

        json_data = json.loads(request.body)
        logger.error(f"json_data {json_data}")

        external_id = json_data.get("paymentId")
        logger.error(f"external_id {external_id}")
        Payment = swapper.load_model("getpaid", "Payment")
        payment = get_object_or_404(
            Payment, **{
                Payment.UNIQUE_ID_FIELD: external_id,
                'backend': PaymentProcessor.slug
            }
        )
        logger.error(f"payment {payment}")
        return payment.handle_paywall_callback(request, *args, **kwargs)
