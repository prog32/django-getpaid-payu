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
        json_data = json.loads(request.body)

        # external_id = json_data.get("paymentId")
        external_id = json_data.get('order', {}).get("extOrderId")

        logger.info(f"external_id: {external_id}; json_data: {json_data}")
        Payment = swapper.load_model("getpaid", "Payment")
        query_kwargs = {
            Payment.UNIQUE_ID_FIELD: external_id,
            # 'backend': PaymentProcessor.slug
            # backend is equal to slug or module name! (may be different)
        }
        payment = get_object_or_404(
            Payment, **query_kwargs
        )
        return payment.handle_paywall_callback(request, *args, **kwargs)
