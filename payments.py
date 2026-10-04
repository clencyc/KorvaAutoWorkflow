import json
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class PaymentApiError(Exception):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("Payment API request failed")


class PaymentClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.opener = opener

    def initiate(
        self,
        *,
        plan_tier: str,
        phone_number: str,
        account_reference: str,
        transaction_desc: str,
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "plan_tier": plan_tier,
                "phone_number": phone_number,
                "account_reference": account_reference,
                "transaction_desc": transaction_desc,
            }
        ).encode()
        request = Request(
            f"{self.base_url}/api/v1/payments/initiate",
            data=body,
            headers={
                "Accept": "*/*",
                "Authorization": "Bearer " + self.access_token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=30) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            raise PaymentApiError(exc.code) from None
        except Exception:
            raise PaymentApiError() from None
        if not isinstance(result, dict):
            raise PaymentApiError()
        return result
