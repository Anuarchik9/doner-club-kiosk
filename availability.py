"""Fresh channel availability. CRM credentials never leave the server."""
from urllib.parse import quote
from uuid import UUID

import requests


class AvailabilityUnavailable(RuntimeError):
    pass


def crm_state(base_url, api_key, location_code):
    if not base_url or not api_key or not location_code:
        raise AvailabilityUnavailable("Не удалось проверить доступность точки. Обратитесь к кассиру.")
    try:
        results = []
        for resource in ("availability", "stop-list"):
            response = requests.get(
                f"{base_url}/api/v1/locations/{quote(location_code, safe='')}/{resource}",
                headers={"X-API-Key": api_key}, timeout=(5, 15),
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ValueError("Invalid CRM response")
            results.append(data)
        state, stops = results
        location = state.get("location")
        if (not isinstance(location, dict)
                or not isinstance(location.get("acceptsOrders"), bool)
                or not isinstance(stops.get("items"), list)):
            raise ValueError("Incomplete CRM response")
        codes = set()
        for item in stops["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("productCode"), str):
                raise ValueError("Invalid stop item")
            codes.add(item["productCode"].strip().casefold())
        return location, codes
    except (requests.RequestException, ValueError, TypeError) as error:
        raise AvailabilityUnavailable("Доступность временно не подтверждена. Попробуйте ещё раз или обратитесь к кассиру.") from error


def needs_catalog(codes):
    for code in codes:
        try:
            UUID(code)
        except ValueError:
            return True
    return False


def catalog_stop_ids(value, codes):
    """Match SKU of dishes and modifiers, retaining iiko's product IDs."""
    result = set()
    if isinstance(value, dict):
        product_id = value.get("itemId") or value.get("productId")
        if not product_id and "sku" in value:
            product_id = value.get("id")
        if product_id and str(value.get("sku", "")).strip().casefold() in codes:
            result.add(str(product_id).casefold())
        for child in value.values():
            result.update(catalog_stop_ids(child, codes))
    elif isinstance(value, list):
        for child in value:
            result.update(catalog_stop_ids(child, codes))
    return result
