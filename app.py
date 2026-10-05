import hmac
import json
import math
import os
import re
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request
from availability import AvailabilityUnavailable, crm_state, needs_catalog, catalog_stop_ids

app = Flask(__name__)


@app.errorhandler(AvailabilityUnavailable)
def availability_unavailable(error):
    response = jsonify(success=False, code="AVAILABILITY_UNAVAILABLE", message=str(error))
    response.headers["Cache-Control"] = "no-store"
    return response, 503


def combined_availability(department):
    organization_id = department["organizationId"]
    code = str(department.get("code") or "").strip().upper()
    if str(organization_id).lower() == "9f2c2c10-a4e8-4e80-ac1d-beedf7d5182e":
        code = "RESPUBLIKA"
    location, manual_codes = crm_state(CRM_BASE_URL, CRM_API_KEY, code)
    try:
        payload = get_stop_lists(organization_id)
        if not isinstance(payload, dict) or not isinstance(payload.get("terminalGroupStopLists"), list):
            raise ValueError("Incomplete iiko stop list")
        stopped, items = normalize_stop_list(payload, organization_id)
        merged = {str(value).casefold() for value in stopped} | manual_codes
        if needs_catalog(manual_codes):
            menus = get_external_menus(organization_id)
            for menu in menus.get("externalMenus", []):
                merged.update(catalog_stop_ids(get_external_menu_by_id(menu["id"], organization_id), manual_codes))
        return sorted(merged), items, location
    except (requests.RequestException, ValueError, TypeError, KeyError, ConfigurationError) as error:
        raise AvailabilityUnavailable("Не удалось проверить стоп-лист. Попробуйте ещё раз или обратитесь к кассиру.") from error


def check_order_availability(validated, order_items):
    stopped, _, location = combined_availability(validated["department"])
    if not location["acceptsOrders"]:
        return {"success": False, "code": "POINT_CLOSED", "message": "Приём заказов временно остановлен. Обратитесь к кассиру."}
    ids = {str(item["productId"]).casefold() for item in order_items}
    ids.update(str(mod["productId"]).casefold() for item in order_items for mod in item.get("modifiers", []))
    blocked = sorted(ids.intersection(stopped))
    if blocked:
        return {"success": False, "code": "PRODUCT_STOPPED", "message": "Некоторые позиции больше недоступны. Обновите корзину.", "stoppedProductIds": blocked}
    return None

SITE_ORIGINS = {
    "https://doner-club-site.onrender.com",
    "https://site.donerclub.kz",
}


@app.after_request
def allow_site_menu_origin(response):
    origin = request.headers.get("Origin")
    if origin in SITE_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

IIKO_BASE_URL = "https://api-ru.iiko.services"
CRM_BASE_URL = os.environ.get("CRM_BASE_URL", "").rstrip("/")
CRM_API_KEY = os.environ.get("CRM_API_KEY", "")
TOKEN_TTL_SECONDS = 50 * 60
DEPARTMENTS_TTL_SECONDS = 10 * 60

_read_token_cache = {"value": None, "expires_at": 0.0}
_kiosk_token_cache = {"value": None, "expires_at": 0.0}
_departments_cache = {"value": None, "expires_at": 0.0}

_read_token_lock = threading.Lock()
_kiosk_token_lock = threading.Lock()
_departments_lock = threading.Lock()


class ConfigurationError(RuntimeError):
    pass


def _cache_valid(cache):
    return cache.get("value") is not None and cache.get("expires_at", 0) > time.time()


def _get_token(api_key, cache, lock, force_refresh=False):
    if not api_key:
        raise ConfigurationError("iikoCloud API key is not configured")
    if not force_refresh and _cache_valid(cache):
        return cache["value"]

    with lock:
        if not force_refresh and _cache_valid(cache):
            return cache["value"]

        app_id = os.environ.get("IIKO_KIOSK_APP_ID") or os.environ.get("IIKO_APP_ID")
        client_secret = os.environ.get("IIKO_KIOSK_CLIENT_SECRET") or os.environ.get("IIKO_CLIENT_SECRET")
        if not app_id or not client_secret:
            raise ConfigurationError("iikoCloud appId/clientSecret are not configured")

        response = requests.post(
            f"{IIKO_BASE_URL}/api/v2/access_token",
            json={"appId": app_id, "clientSecret": client_secret, "apiKey": api_key},
            timeout=20,
        )
        response.raise_for_status()
        token = response.json()["token"]
        cache["value"] = token
        cache["expires_at"] = time.time() + TOKEN_TTL_SECONDS
        return token


def get_iiko_read_token(force_refresh=False):
    # Keep compatibility with the currently working Render setup:
    # read/menu endpoints prefer IIKO_API_KEY, but can fall back to the kiosk key.
    api_key = os.environ.get("IIKO_API_KEY") or os.environ.get("IIKO_KIOSK_API_KEY")
    return _get_token(api_key, _read_token_cache, _read_token_lock, force_refresh)


def get_iiko_kiosk_token(force_refresh=False):
    api_key = os.environ.get("IIKO_KIOSK_API_KEY")
    if not api_key:
        raise ConfigurationError("Dedicated IIKO_KIOSK_API_KEY is not configured")
    return _get_token(api_key, _kiosk_token_cache, _kiosk_token_lock, force_refresh)


def _post(path, payload, token_getter, timeout=30, retry_auth=True):
    response = requests.post(
        f"{IIKO_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {token_getter()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if response.status_code == 401 and retry_auth:
        token_getter(force_refresh=True)
        return _post(path, payload, token_getter, timeout=timeout, retry_auth=False)
    return response


def iiko_post(path, payload, timeout=30, retry_auth=True):
    return _post(path, payload, get_iiko_read_token, timeout, retry_auth)


def iiko_kiosk_post(path, payload, timeout=30, retry_auth=True):
    return _post(path, payload, get_iiko_kiosk_token, timeout, retry_auth)


def get_departments(force_refresh=False):
    if not force_refresh and _cache_valid(_departments_cache):
        return _departments_cache["value"]

    with _departments_lock:
        if not force_refresh and _cache_valid(_departments_cache):
            return _departments_cache["value"]

        response = iiko_post("/api/inventory/v1/organizations/tree", {}, timeout=30)
        response.raise_for_status()
        tree = response.json()
        departments = []

        def walk(value):
            if isinstance(value, dict):
                if value.get("type") == "DEPARTMENT" and value.get("organizationId"):
                    departments.append({
                        "organizationId": value.get("organizationId"),
                        "name": value.get("name"),
                        "code": value.get("code"),
                        "parentId": value.get("parentId"),
                    })
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(tree)
        _departments_cache["value"] = departments
        _departments_cache["expires_at"] = time.time() + DEPARTMENTS_TTL_SECONDS
        return departments


POINT_ALIASES = {
    "arai": ("arai", "aray", "арай"),
    "aray": ("arai", "aray", "арай"),
    "арай": ("arai", "aray", "арай"),
    "respublica": ("respublica", "respublika", "republic", "republica", "республика"),
    "respublika": ("respublica", "respublika", "republic", "republica", "республика"),
    "republic": ("respublica", "respublika", "republic", "republica", "республика"),
    "republica": ("respublica", "respublika", "republic", "republica", "республика"),
    "республика": ("respublica", "respublika", "republic", "republica", "республика"),
}


def point_search_terms(point):
    normalized = (point or "").strip().casefold()
    if not normalized:
        return ()
    terms = [normalized]
    for alias in POINT_ALIASES.get(normalized, ()):
        alias = alias.casefold()
        if alias not in terms:
            terms.append(alias)
    return tuple(terms)


def find_department(point):
    terms = point_search_terms(point)
    if not terms:
        return None, []
    departments = get_departments()

    for department in departments:
        code = (department.get("code") or "").strip().casefold()
        name = (department.get("name") or "").strip().casefold()
        if any(code == term or name == term for term in terms):
            return department, departments

    for department in departments:
        code = (department.get("code") or "").strip().casefold()
        name = (department.get("name") or "").strip().casefold()
        if any(term in code or term in name for term in terms):
            return department, departments

    return None, departments


def select_external_menu(external_menus, requested_menu, department):
    requested = (requested_menu or "").strip()
    if requested and requested != "__AUTO__":
        exact = next(
            (
                menu for menu in external_menus
                if (menu.get("name") or "").strip().casefold() == requested.casefold()
            ),
            None,
        )
        if exact:
            return exact

    if requested != "__AUTO__":
        return None

    point_terms = set(point_search_terms(department.get("code")))
    point_terms.update(point_search_terms(department.get("name")))
    generic = {"doner", "club", "donerclub", "точка", "point"}
    point_terms = {term for term in point_terms if term and term not in generic}

    kiosk_menus = [
        menu for menu in external_menus
        if "kiosk" in (menu.get("name") or "").casefold()
    ]

    matched = []
    for menu in kiosk_menus:
        name = (menu.get("name") or "").casefold()
        if any(term in name for term in point_terms):
            matched.append(menu)

    if len(matched) == 1:
        return matched[0]
    if len(kiosk_menus) == 1:
        return kiosk_menus[0]
    if len(external_menus) == 1:
        return external_menus[0]
    return None


def kiosk_read_post(path, payload, timeout):
    # Kiosk menu and stop-list access use the dedicated KIOSK integration.
    # Preserve the previous reader during rollout if that integration rejects a menu.
    kiosk_payload = dict(payload)
    if path == "/api/2/menu/by_id":
        kiosk_payload["priceCategoryId"] = (
            os.environ.get("IIKO_KIOSK_PRICE_CATEGORY_ID", "").strip() or None
        )
    response = iiko_kiosk_post(path, kiosk_payload, timeout=timeout)
    if response.status_code in (400, 403) and os.environ.get("IIKO_API_KEY"):
        fallback = iiko_post(path, payload, timeout=timeout)
        if fallback.ok:
            return fallback
    return response


def get_external_menus(organization_id):
    response = kiosk_read_post(
        "/api/2/menu",
        {"organizationIds": [organization_id]},
        timeout=35,
    )
    response.raise_for_status()
    return response.json()


def get_external_menu_by_id(external_menu_id, organization_id):
    response = kiosk_read_post(
        "/api/2/menu/by_id",
        {
            "externalMenuId": str(external_menu_id),
            "organizationIds": [organization_id],
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def get_stop_lists(organization_id):
    response = kiosk_read_post(
        "/api/1/stop_lists",
        {"organizationIds": [organization_id]},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def normalize_stop_list(payload, organization_id):
    """Read iiko's organization -> terminal group -> product hierarchy."""
    items = []
    stopped_ids = set()
    groups = payload.get("terminalGroupStopLists") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        raise ValueError("Incomplete iiko stop list")

    def product_items(group):
        children = group.get("items")
        if not isinstance(children, list):
            raise ValueError("Incomplete iiko stop-list group")
        terminal_id = group.get("terminalGroupId") or group.get("id")
        for child in children:
            if not isinstance(child, dict):
                raise ValueError("Invalid iiko stop-list item")
            if child.get("productId"):
                yield child, terminal_id
            elif isinstance(child.get("items"), list):
                # The API wraps terminal lists in an organization object.
                yield from product_items(child)
            else:
                raise ValueError("Unrecognized iiko stop-list item")

    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid iiko stop-list group")
        group_org = group.get("organizationId")
        if group_org and str(group_org).casefold() != str(organization_id).casefold():
            continue
        for item, terminal_group_id in product_items(group):
            product_id = str(item["productId"]).strip()
            raw_balance = item.get("balance")
            try:
                balance = float(raw_balance) if raw_balance is not None else None
            except (TypeError, ValueError, OverflowError):
                balance = None
            if balance is not None and not math.isfinite(balance):
                balance = None

            stopped = balance is None or balance <= 0
            if stopped:
                stopped_ids.add(product_id)

            items.append({
                "productId": product_id,
                "balance": balance,
                "stopped": stopped,
                "terminalGroupId": terminal_group_id,
            })

    return sorted(stopped_ids), items


def _menu_price(prices, organization_id):
    prices = prices or []
    matching = [entry for entry in prices if str(entry.get("organizationId")) == str(organization_id)]
    for entry in matching or prices:
        try:
            price = float(entry.get("price"))
            if not isinstance(entry.get("price"), bool) and math.isfinite(price) and price >= 0:
                return round(price, 2)
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _first_image_url(*values):
    """Return the first usable image URL from iiko menu fields."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("url", "imageUrl", "buttonImageUrl", "buttonImage"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        if isinstance(value, list):
            for item in value:
                candidate = _first_image_url(item)
                if candidate:
                    return candidate
    return None


def _normalize_modifier_item(item, organization_id):
    prices = item.get("prices") or []
    image_url = _first_image_url(item.get("buttonImage"), item.get("buttonImageUrl"), item.get("imageUrl"), item.get("imageLinks"), item.get("images"))
    restrictions = item.get("restrictions") or {}

    if not prices:
        sizes = item.get("itemSizes") or []
        size = next((s for s in sizes if s.get("isDefault")), None) or (sizes[0] if sizes else {})
        prices = size.get("prices") or []
        image_url = image_url or _first_image_url(size.get("buttonImageUrl"), size.get("buttonImage"), size.get("imageUrl"), size.get("imageLinks"), size.get("images"))
        if not restrictions:
            restrictions = size.get("restrictions") or {}

    return {
        "id": item.get("itemId"),
        "sku": item.get("sku"),
        "name": item.get("name"),
        "price": _menu_price(prices, organization_id) or 0,
        "imageUrl": image_url,
        "isHidden": bool(item.get("isHidden")),
        "minQuantity": restrictions.get("minQuantity") or 0,
        "maxQuantity": restrictions.get("maxQuantity") or 0,
        "freeQuantity": restrictions.get("freeQuantity") or 0,
        "byDefault": restrictions.get("byDefault") or 0,
    }


def normalize_external_menu(menu_data, organization_id):
    categories = []
    products = []

    for category in menu_data.get("itemCategories", []) or []:
        if category.get("isHidden"):
            continue

        category_id = category.get("id")
        categories.append({
            "id": category_id,
            "name": category.get("name"),
            "description": category.get("description"),
            "imageUrl": category.get("buttonImageUrl") or category.get("headerImageUrl"),
        })

        for item in category.get("items", []) or []:
            if item.get("isHidden"):
                continue

            sizes = item.get("itemSizes") or []
            if not sizes:
                continue

            for idx, size in enumerate(sizes):
                if size.get("isHidden"):
                    continue

                price = _menu_price(size.get("prices"), organization_id)
                if price is None:
                    continue

                modifier_groups = []
                for group in size.get("itemModifierGroups", []) or []:
                    group_restrictions = group.get("restrictions") or {}
                    modifier_groups.append({
                        "id": group.get("id") or group.get("itemGroupId"),
                        "name": group.get("name"),
                        "minQuantity": (
                            group.get("minQuantity")
                            if group.get("minQuantity") is not None
                            else group_restrictions.get("minQuantity") or 0
                        ),
                        "maxQuantity": (
                            group.get("maxQuantity")
                            if group.get("maxQuantity") is not None
                            else group_restrictions.get("maxQuantity") or 0
                        ),
                        "freeQuantity": group_restrictions.get("freeQuantity") or 0,
                        "byDefault": group_restrictions.get("byDefault") or 0,
                        "items": [
                            _normalize_modifier_item(modifier, organization_id)
                            for modifier in (group.get("items") or [])
                            if not modifier.get("isHidden")
                        ],
                    })

                size_name = (size.get("sizeName") or "").strip()
                display_name = item.get("name") or "Позиция"
                if len(sizes) > 1 and size_name:
                    display_name = f"{display_name} — {size_name}"

                products.append({
                    "id": f"{item.get('itemId')}:{size.get('sizeId') or idx}",
                    "itemId": item.get("itemId"),
                    "sizeId": size.get("sizeId"),
                    "sku": item.get("sku"),
                    "categoryId": category_id,
                    "name": display_name,
                    "description": item.get("description") or "",
                    "price": price,
                    "weightGrams": size.get("portionWeightGrams") or 0,
                    "imageUrl": _first_image_url(
                        size.get("buttonImageUrl"),
                        size.get("buttonImage"),
                        size.get("imageUrl"),
                        size.get("imageLinks"),
                        size.get("images"),
                        item.get("buttonImageUrl"),
                        item.get("buttonImage"),
                        item.get("imageUrl"),
                        item.get("imageLinks"),
                        item.get("images"),
                        category.get("buttonImageUrl"),
                        category.get("headerImageUrl"),
                    ),
                    "modifierGroups": modifier_groups,
                })

    return categories, products


def get_terminal_groups_for_organization(organization_id):
    response = iiko_kiosk_post(
        "/api/1/terminal_groups",
        {
            "organizationIds": [organization_id],
            "includeDisabled": False,
        },
        timeout=35,
    )
    response.raise_for_status()
    data = response.json()
    groups = []

    for block in data.get("terminalGroups", []) or []:
        if str(block.get("organizationId")) != str(organization_id):
            continue
        for item in block.get("items", []) or []:
            group_id = item.get("id")
            if group_id:
                groups.append({
                    "id": group_id,
                    "name": item.get("name") or "Terminal group",
                    "organizationId": item.get("organizationId") or organization_id,
                })

    return groups, data


def get_terminal_groups_alive(organization_id, terminal_group_ids):
    if not terminal_group_ids:
        return {}

    response = iiko_kiosk_post(
        "/api/1/terminal_groups/is_alive",
        {
            "organizationIds": [organization_id],
            "terminalGroupIds": terminal_group_ids,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    return {
        str(item.get("terminalGroupId")): bool(item.get("isAlive"))
        for item in (data.get("isAliveStatus") or [])
        if item.get("terminalGroupId")
    }


def get_restaurant_sections_for_terminal_groups(terminal_group_ids):
    if not terminal_group_ids:
        return [], {}

    response = iiko_post(
        "/api/1/reserve/available_restaurant_sections",
        {
            "terminalGroupIds": terminal_group_ids,
            "returnSchema": True,
            "revision": 0,
        },
        timeout=40,
    )
    response.raise_for_status()
    data = response.json()
    sections = []

    for section in data.get("restaurantSections", []) or []:
        section_id = section.get("id")
        terminal_group_id = section.get("terminalGroupId")
        section_name = section.get("name") or "Зал"
        tables = []

        for table in section.get("tables", []) or []:
            if table.get("isDeleted"):
                continue
            table_id = table.get("id")
            if not table_id:
                continue
            tables.append({
                "id": table_id,
                "number": table.get("number"),
                "name": table.get("name") or "",
                "seatingCapacity": table.get("seatingCapacity"),
                "sectionId": section_id,
                "sectionName": section_name,
                "terminalGroupId": terminal_group_id,
            })

        sections.append({
            "id": section_id,
            "name": section_name,
            "terminalGroupId": terminal_group_id,
            "tables": tables,
        })

    return sections, data


def _normalize_order_items(incoming_items):
    if not isinstance(incoming_items, list) or not incoming_items:
        raise ValueError("items must be a non-empty array")
    order_items = []

    for source_item in incoming_items:
        if not isinstance(source_item, dict):
            raise ValueError("Each item must be an object")
        product_id = str(source_item.get("productId") or "").strip()
        if not product_id:
            raise ValueError("productId is required")

        try:
            amount = float(source_item.get("amount") or 0)
        except (TypeError, ValueError, OverflowError):
            amount = 0

        if isinstance(source_item.get("amount"), bool) or not math.isfinite(amount) or amount <= 0:
            raise ValueError("Item amount must be a finite positive number")

        order_item = {
            "productId": product_id,
            "type": "Product",
            "amount": amount,
        }

        item_price = source_item.get("price")
        if item_price is not None:
            try:
                item_price = float(item_price)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Item price must be a finite non-negative number") from None
            if isinstance(source_item.get("price"), bool) or not math.isfinite(item_price) or item_price < 0:
                raise ValueError("Item price must be a finite non-negative number")
            order_item["price"] = item_price

        product_size_id = source_item.get("productSizeId")
        if product_size_id:
            order_item["productSizeId"] = str(product_size_id)

        modifiers = []
        incoming_modifiers = source_item.get("modifiers", [])
        if not isinstance(incoming_modifiers, list):
            raise ValueError("modifiers must be an array")
        for modifier in incoming_modifiers:
            if not isinstance(modifier, dict):
                raise ValueError("Each modifier must be an object")
            modifier_product_id = str(modifier.get("productId") or "").strip()
            if not modifier_product_id:
                raise ValueError("Modifier productId is required")
            try:
                modifier_amount = float(modifier.get("amount", 1))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Modifier amount must be a finite positive number") from None
            if isinstance(modifier.get("amount"), bool) or not math.isfinite(modifier_amount) or modifier_amount <= 0:
                raise ValueError("Modifier amount must be a finite positive number")

            modifier_payload = {
                "productId": modifier_product_id,
                "amount": modifier_amount,
            }
            modifier_price = modifier.get("price")
            if modifier_price is not None:
                try:
                    modifier_price = float(modifier_price)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("Modifier price must be a finite non-negative number") from None
                if isinstance(modifier.get("price"), bool) or not math.isfinite(modifier_price) or modifier_price < 0:
                    raise ValueError("Modifier price must be a finite non-negative number")
                modifier_payload["price"] = modifier_price

            product_group_id = modifier.get("productGroupId")
            if product_group_id:
                modifier_payload["productGroupId"] = str(product_group_id)
            modifiers.append(modifier_payload)

        if modifiers:
            order_item["modifiers"] = modifiers

        order_items.append(order_item)

    return order_items


def _validate_point_terminal(point, terminal_group_id):
    department, available_departments = find_department(point)
    if not department:
        return None, {
            "success": False,
            "code": "POINT_NOT_FOUND",
            "message": f"Point '{point}' not found",
            "availablePoints": [
                {"code": d.get("code"), "name": d.get("name")}
                for d in available_departments
            ],
        }, 404

    organization_id = department["organizationId"]
    terminal_groups, _ = get_terminal_groups_for_organization(organization_id)
    allowed_group_ids = {str(group["id"]) for group in terminal_groups}

    if terminal_group_id not in allowed_group_ids:
        return None, {
            "success": False,
            "code": "INVALID_TERMINAL_GROUP",
            "message": f"Selected terminal group does not belong to {department.get('name') or point}.",
        }, 400

    return {
        "department": department,
        "organizationId": organization_id,
    }, None, 200



def _kiosk_point_env_prefix(point):
    normalized = str(point or "").strip().upper()
    if normalized in {"RESPUBLIKA", "RESPUBLICA", "REPUBLIC", "REPUBLICA", "РЕСПУБЛИКА"}:
        return "RESPUBLIKA"
    return "ARAI"


def _resolve_kiosk_order_target(point):
    department, available_departments = find_department(point)
    if not department:
        return None, {
            "success": False,
            "code": "POINT_NOT_FOUND",
            "message": f"Point '{point}' not found",
            "availablePoints": [
                {"code": d.get("code"), "name": d.get("name")}
                for d in available_departments
            ],
        }, 404

    organization_id = department["organizationId"]
    terminal_groups, _ = get_terminal_groups_for_organization(organization_id)
    if not terminal_groups:
        return None, {
            "success": False,
            "code": "TERMINAL_GROUP_NOT_FOUND",
            "message": "iiko did not return an available terminal group.",
        }, 503

    group_ids = [str(group["id"]) for group in terminal_groups]
    alive = get_terminal_groups_alive(organization_id, group_ids)
    prefix = _kiosk_point_env_prefix(point)
    configured_group = str(os.environ.get(f"IIKO_KIOSK_{prefix}_TERMINAL_GROUP_ID") or "").strip()

    if configured_group:
        selected_group = next((group for group in terminal_groups if str(group["id"]) == configured_group), None)
        if not selected_group:
            return None, {
                "success": False,
                "code": "CONFIGURED_TERMINAL_GROUP_INVALID",
                "message": f"Configured kiosk terminal group is not available for {point}.",
            }, 503
    else:
        selected_group = next(
            (group for group in terminal_groups if alive.get(str(group["id"])) is True),
            terminal_groups[0],
        )

    terminal_group_id = str(selected_group["id"])
    return {
        "department": department,
        "organizationId": organization_id,
        "terminalGroupId": terminal_group_id,
        "terminalGroupName": selected_group.get("name"),
        "terminalGroupAlive": alive.get(terminal_group_id),
    }, None, 200


def _table_order_by_id(organization_id, order_id):
    response = iiko_kiosk_post(
        "/api/1/order/by_id",
        {"organizationIds": [organization_id], "orderIds": [order_id]},
        timeout=30,
    )
    if not response.ok:
        return None
    payload = response.json()
    orders = payload.get("orders") or []
    return orders[0] if orders else None


def _close_kiosk_order(organization_id, order_id):
    response = iiko_kiosk_post(
        "/api/1/order/close",
        {"organizationId": organization_id, "orderId": order_id},
        timeout=45,
    )
    if not response.ok:
        return None, {
            "success": False,
            "code": "IIKO_ORDER_CLOSE_FAILED",
            "statusCode": response.status_code,
            "details": response.text[:3000],
            "orderId": order_id,
        }, 502

    result = response.json()
    status = _wait_command(organization_id, result.get("correlationId"), attempts=12)
    if status and status.get("state") == "Success":
        return status, None, 200
    if status and status.get("state") == "Error":
        return status, {
            "success": False,
            "code": "IIKO_ORDER_CLOSE_COMMAND_ERROR",
            "message": "iikoFront reported an error while closing the paid kiosk order.",
            "commandStatus": status,
            "orderId": order_id,
        }, 502
    return status, {
        "success": False,
        "code": "IIKO_ORDER_CLOSE_PENDING",
        "message": "Order closure is not confirmed yet. Do not charge the guest again.",
        "commandStatus": status,
        "orderId": order_id,
    }, 202


def _wait_command(organization_id, correlation_id, attempts=10):
    if not correlation_id:
        return None

    status = None
    for _ in range(attempts):
        time.sleep(1)
        response = iiko_kiosk_post(
            "/api/1/commands/status",
            {
                "organizationId": organization_id,
                "correlationId": correlation_id,
            },
            timeout=20,
        )
        if not response.ok:
            break
        status = response.json()
        if status.get("state") in ("Success", "Error"):
            break

    return status


@app.route("/")
@app.route("/kiosk")
@app.route("/Aray")
@app.route("/aray")
@app.route("/respublica")
@app.route("/Respublica")
def home():
    response = app.send_static_file("kiosk-preview.html")
    # The kiosk UI changes frequently; never let iPad/desktop browsers keep an
    # older HTML/JS version after a deploy.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/health")
def health():
    return jsonify({"service": "Doner Club Kiosk", "status": "online"})


@app.route("/kiosk-menu")
def kiosk_menu():
    point = request.args.get("point", "Arai").strip()
    requested_menu = request.args.get("menu", "").strip()

    try:
        department, available_departments = find_department(point)
        if not department:
            return jsonify({
                "success": False,
                "code": "POINT_NOT_FOUND",
                "message": f"Point '{point}' not found",
                "availablePoints": [
                    {"code": d.get("code"), "name": d.get("name")}
                    for d in available_departments
                ],
            }), 404

        organization_id = department["organizationId"]
        menus_payload = get_external_menus(organization_id)
        external_menus = menus_payload.get("externalMenus", []) or []

        selected = select_external_menu(external_menus, requested_menu, department)

        if not selected:
            response = jsonify({
                "success": False,
                "code": "EXTERNAL_MENU_NOT_FOUND",
                "message": (
                    f"Could not resolve a kiosk menu for point '{point}'."
                    if requested_menu == "__AUTO__"
                    else f"External menu '{requested_menu}' is not available for point '{point}'."
                ),
                "requestedMenu": requested_menu,
                "availableMenus": [
                    {"id": menu.get("id"), "name": menu.get("name")}
                    for menu in external_menus
                ],
            })
            response.headers["Cache-Control"] = "no-store"
            return response, 404

        menu_data = get_external_menu_by_id(selected.get("id"), organization_id)
        categories, products = normalize_external_menu(menu_data, organization_id)

        if not products:
            visible_items = [
                item for category in (menu_data.get("itemCategories") or [])
                if not category.get("isHidden")
                for item in (category.get("items") or []) if not item.get("isHidden")
            ]
            visible_sizes = [
                size for item in visible_items for size in (item.get("itemSizes") or [])
                if not size.get("isHidden")
            ]
            message = (
                "Внешнее меню iiko доступно, но в нём нет открытых блюд. Проверьте состав и публикацию меню."
                if not visible_items else
                "Внешнее меню iiko доступно, но для блюд нет доступных размеров."
                if not visible_sizes else
                "Внешнее меню iiko доступно, но для блюд не получены цены. Проверьте цены для этой точки и ценовой категории."
            )
            response = jsonify(
                success=False, code="EXTERNAL_MENU_EMPTY", message=message,
                menu={"id": selected.get("id"), "name": selected.get("name")},
                diagnostics={"visibleItems": len(visible_items), "visibleSizes": len(visible_sizes),
                             "sizesWithoutPrice": sum(_menu_price(size.get("prices"), organization_id) is None for size in visible_sizes)},
            )
            response.headers["Cache-Control"] = "no-store"
            return response, 422

        response = jsonify({
            "success": True,
            "source": "iikoCloud external menu",
            "point": {
                "code": department.get("code"),
                "name": department.get("name"),
                "organizationId": organization_id,
            },
            "menu": {
                "id": selected.get("id"),
                "name": selected.get("name"),
                "description": menu_data.get("description"),
                "revision": menu_data.get("revision"),
            },
            "categories": categories,
            "products": products,
            "priceCategories": menus_payload.get("priceCategories", []) or [],
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify({"success": False, "code": "IIKO_TIMEOUT", "message": "iiko did not answer in time."}), 504
    except requests.HTTPError as error:
        response = error.response
        return jsonify({
            "success": False,
            "code": "IIKO_HTTP_ERROR",
            "statusCode": response.status_code if response is not None else None,
            "details": response.text[:1500] if response is not None else str(error),
        }), 502
    except Exception as error:
        return jsonify({"success": False, "code": "KIOSK_MENU_ERROR", "message": str(error)}), 500


@app.route("/kiosk-stop-list")
def kiosk_stop_list():
    point = request.args.get("point", "Arai").strip()

    try:
        department, available_departments = find_department(point)
        if not department:
            return jsonify({
                "success": False,
                "code": "POINT_NOT_FOUND",
                "message": f"Point '{point}' not found",
                "availablePoints": [
                    {"code": d.get("code"), "name": d.get("name")}
                    for d in available_departments
                ],
            }), 404

        organization_id = department["organizationId"]
        stopped_product_ids, items, location = combined_availability(department)

        response = jsonify({
            "success": True,
            "source": "iikoCloud + CRM",
            "location": location,
            "point": {
                "code": department.get("code"),
                "name": department.get("name"),
                "organizationId": organization_id,
            },
            "stoppedProductIds": stopped_product_ids,
            "items": items,
            "checkedAt": int(time.time()),
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    except AvailabilityUnavailable:
        raise
    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify({"success": False, "code": "IIKO_TIMEOUT", "message": "iiko stop list did not answer in time."}), 504
    except requests.HTTPError as error:
        response = error.response
        return jsonify({
            "success": False,
            "code": "IIKO_STOP_LIST_HTTP_ERROR",
            "statusCode": response.status_code if response is not None else None,
            "details": response.text[:1500] if response is not None else str(error),
        }), 502
    except Exception as error:
        return jsonify({"success": False, "code": "KIOSK_STOP_LIST_ERROR", "message": str(error)}), 500


@app.route("/kiosk-iiko-config")
def kiosk_iiko_config():
    point = request.args.get("point", "Arai").strip()

    try:
        department, available_departments = find_department(point)
        if not department:
            return jsonify({
                "success": False,
                "code": "POINT_NOT_FOUND",
                "message": f"Point '{point}' not found",
                "availablePoints": [
                    {"code": d.get("code"), "name": d.get("name")}
                    for d in available_departments
                ],
            }), 404

        organization_id = department["organizationId"]
        terminal_groups, _ = get_terminal_groups_for_organization(organization_id)
        terminal_group_ids = [group["id"] for group in terminal_groups]
        alive = get_terminal_groups_alive(organization_id, terminal_group_ids)

        for group in terminal_groups:
            group["isAlive"] = alive.get(str(group["id"]))

        response = jsonify({
            "success": True,
            "point": {
                "code": department.get("code"),
                "name": department.get("name"),
                "organizationId": organization_id,
            },
            "terminalGroups": terminal_groups,
            "selfService": True,
            "canSendTestOrder": bool(terminal_groups),
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify({"success": False, "code": "IIKO_TIMEOUT", "message": "iiko did not answer in time."}), 504
    except requests.HTTPError as error:
        response = error.response
        return jsonify({
            "success": False,
            "code": "IIKO_HTTP_ERROR",
            "statusCode": response.status_code if response is not None else None,
            "details": response.text[:1500] if response is not None else str(error),
        }), 502
    except Exception as error:
        return jsonify({"success": False, "code": "KIOSK_IIKO_CONFIG_ERROR", "message": str(error)}), 500


@app.route("/kiosk-test-order", methods=["POST"])
def kiosk_test_order():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(success=False, code="INVALID_TEST_ORDER", message="JSON object required."), 400

    if data.get("confirm") != "SEND_TEST_ORDER":
        return jsonify({
            "success": False,
            "code": "EXPLICIT_CONFIRMATION_REQUIRED",
            "message": "Test order was NOT sent. Explicit confirmation is required.",
        }), 400

    point = str(data.get("point") or "Arai").strip()
    terminal_group_id = str(data.get("terminalGroupId") or "").strip()
    incoming_items = data.get("items") or []

    if not terminal_group_id or not incoming_items:
        return jsonify({
            "success": False,
            "code": "INVALID_TEST_ORDER",
            "message": "terminalGroupId and items are required.",
        }), 400

    try:
        order_items = _normalize_order_items(incoming_items)
    except ValueError as error:
        return jsonify(success=False, code="INVALID_TEST_ORDER", message=str(error)), 400

    try:
        if not os.environ.get("IIKO_KIOSK_API_KEY"):
            return jsonify({
                "success": False,
                "code": "KIOSK_API_NOT_CONFIGURED",
                "message": "IIKO_KIOSK_API_KEY is not configured.",
            }), 503

        validated, error_payload, status_code = _validate_point_terminal(point, terminal_group_id)
        if error_payload:
            return jsonify(error_payload), status_code

        rejection = check_order_availability(validated, order_items)
        if rejection:
            return jsonify(rejection), 409

        organization_id = validated["organizationId"]
        payload = {
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "createOrderSettings": {
                "servicePrint": True,
                "transportToFrontTimeout": 10,
                "checkStopList": True,
            },
            "order": {
                "items": order_items,
                "guests": {"count": 1, "splitBetweenPersons": False},
                "tabName": "",
            },
        }

        response = iiko_kiosk_post("/api/1/order/create", payload, timeout=45)
        if not response.ok:
            return jsonify({
                "success": False,
                "code": "IIKO_ORDER_CREATE_FAILED",
                "statusCode": response.status_code,
                "details": response.text[:2500],
            }), 502

        result = response.json()
        command_status = _wait_command(
            organization_id,
            result.get("correlationId"),
            attempts=6,
        )
        state = (command_status or {}).get("state")
        if state != "Success":
            return jsonify(
                success=False,
                code="TEST_ORDER_COMMAND_ERROR" if state == "Error" else "TEST_ORDER_PENDING",
                message="Order submission was attempted. Check iiko before retrying.",
                commandStatus=command_status, iiko=result,
            ), 502 if state == "Error" else 202

        return jsonify({
            "success": True,
            "message": "Test order request was sent to iiko.",
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "servicePrintRequested": True,
            "commandStatus": command_status,
            "iiko": result,
        })

    except AvailabilityUnavailable:
        raise
    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify({"success": False, "code": "IIKO_TIMEOUT", "message": "iiko did not answer in time."}), 504
    except Exception as error:
        return jsonify({"success": False, "code": "KIOSK_TEST_ORDER_ERROR", "message": str(error)}), 500




def _live_kiosk_payments_enabled():
    return str(os.environ.get("KIOSK_LIVE_PAYMENTS_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


@app.get("/kiosk-payment-readiness")
def kiosk_payment_readiness():
    bridge_token_configured = bool(str(os.environ.get("KIOSK_BRIDGE_ORDER_TOKEN") or "").strip())
    iiko_configured = bool(os.environ.get("IIKO_KIOSK_API_KEY"))
    ready = _live_kiosk_payments_enabled() and iiko_configured and bridge_token_configured
    warning = None
    if ready:
        try:
            target, warning, _ = _resolve_kiosk_order_target("RESPUBLIKA")
            if not warning:
                _, warning = _resolve_kiosk_payment(target["organizationId"], target["terminalGroupId"], "RESPUBLIKA")
            ready = not warning
        except Exception:
            ready = False
            warning = {"code": "PAYMENT_CONFIGURATION_UNAVAILABLE"}
    return jsonify(
        success=True,
        ready=ready,
        warning=warning,
        point="RESPUBLIKA",
        iikoConfigured=iiko_configured,
        bridgeTokenRequired=bridge_token_configured,
    )


def _record_kiosk_order_in_crm(data, *, order_id, order_number, payment_sum):
    if not CRM_BASE_URL or not CRM_API_KEY:
        return {"ok": False, "skipped": True, "reason": "CRM_NOT_CONFIGURED"}

    phone = str(data.get("phone") or "").strip() or None
    try:
        subtotal = float(data.get("subtotal") if data.get("subtotal") is not None else payment_sum)
        discount_amount = float(data.get("discountAmount") or 0)
        discount_percent = float(data.get("discountPercent") or 0)
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "reason": "INVALID_CRM_TOTALS"}

    if any(not math.isfinite(value) for value in (subtotal, discount_amount, discount_percent)):
        return {"ok": False, "reason": "INVALID_CRM_TOTALS"}
    if abs((subtotal - discount_amount) - float(payment_sum)) > 0.011:
        return {"ok": False, "reason": "CRM_TOTAL_MISMATCH"}
    if discount_amount > 0 and not phone:
        return {"ok": False, "reason": "DISCOUNT_WITHOUT_PHONE"}

    point_code = _kiosk_point_env_prefix(data.get("point"))
    payload = {
        "orderNumber": str(order_number or ""),
        "iikoOrderId": str(order_id),
        "locationCode": point_code,
        "source": "KIOSK",
        "subtotal": round(subtotal, 2),
        "discountAmount": round(discount_amount, 2),
        "discountPercent": round(discount_percent, 2),
        "total": round(float(payment_sum), 2),
        "paymentMethod": "KASPI_SMART_POS",
        "status": "COMPLETED",
        "phone": phone or "",
        "discountReason": "Скидка 5% на первый заказ в киоске" if discount_amount > 0 else "",
    }
    if discount_amount > 0:
        payload["campaignCode"] = "FIRST_KIOSK_5"

    try:
        response = requests.post(
            f"{CRM_BASE_URL}/api/v1/orders",
            json=payload,
            headers={"X-API-Key": CRM_API_KEY, "Content-Type": "application/json"},
            timeout=(5, 20),
        )
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}
        if response.ok and isinstance(body, dict) and body.get("ok"):
            return {"ok": True, "response": body}
        return {"ok": False, "statusCode": response.status_code, "response": body}
    except requests.RequestException as error:
        return {"ok": False, "reason": "CRM_UNAVAILABLE", "message": str(error)}


@app.route("/kiosk-paid-order", methods=["POST"])
def kiosk_paid_order():
    if not _live_kiosk_payments_enabled():
        return jsonify(
            success=False,
            code="LIVE_PAYMENTS_NOT_ENABLED",
            message="Republic kiosk live payments are prepared but not activated yet.",
        ), 503

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(success=False, code="INVALID_KIOSK_ORDER", message="JSON object required."), 400

    # Production paid orders must arrive through the Republic LAN bridge.
    # The bridge injects the shared secret server-side; it is never exposed in kiosk JavaScript.
    expected_bridge_token = str(os.environ.get("KIOSK_BRIDGE_ORDER_TOKEN") or "").strip()
    if not expected_bridge_token:
        return jsonify(
            success=False,
            code="BRIDGE_TOKEN_NOT_CONFIGURED",
            message="Live kiosk payments require the Republic bridge token.",
        ), 503

    supplied_bridge_token = str(request.headers.get("X-Kiosk-Bridge-Token") or "").strip()
    if not supplied_bridge_token or not hmac.compare_digest(expected_bridge_token, supplied_bridge_token):
        return jsonify(success=False, code="BRIDGE_UNAUTHORIZED"), 401

    point = str(data.get("point") or "RESPUBLIKA").strip()
    process_id = str(data.get("paymentProcessId") or "").strip()
    payment_status = str(data.get("paymentSubStatus") or "").strip()
    phone = str(data.get("phone") or "").strip() or None
    incoming_items = data.get("items") or []

    if not process_id:
        return jsonify(success=False, code="PAYMENT_PROCESS_ID_REQUIRED", message="Kaspi payment process ID is required."), 400
    if payment_status not in {"QrTransactionSuccess", "CardTransactionSuccess"}:
        return jsonify(success=False, code="PAYMENT_NOT_CONFIRMED", message="Kaspi payment is not confirmed as successful."), 409

    try:
        payment_sum = float(data.get("paymentSum"))
    except (TypeError, ValueError, OverflowError):
        payment_sum = 0
    if isinstance(data.get("paymentSum"), bool) or not math.isfinite(payment_sum) or payment_sum <= 0 or payment_sum > 500000:
        return jsonify(success=False, code="INVALID_PAYMENT_SUM", message="Invalid paid amount."), 400

    try:
        subtotal = float(data.get("subtotal") if data.get("subtotal") is not None else payment_sum)
        discount_amount = float(data.get("discountAmount") or 0)
        discount_percent = float(data.get("discountPercent") or 0)
    except (TypeError, ValueError, OverflowError):
        return jsonify(success=False, code="INVALID_ORDER_TOTALS", message="Invalid order totals."), 400
    if any(not math.isfinite(value) for value in (subtotal, discount_amount, discount_percent)):
        return jsonify(success=False, code="INVALID_ORDER_TOTALS", message="Invalid order totals."), 400
    if subtotal < 0 or discount_amount < 0 or discount_percent < 0 or discount_percent > 100:
        return jsonify(success=False, code="INVALID_ORDER_TOTALS", message="Invalid order totals."), 400
    if abs((subtotal - discount_amount) - payment_sum) > 0.011:
        return jsonify(success=False, code="ORDER_PAYMENT_SUM_MISMATCH", message="Paid amount does not match order total."), 409
    if discount_amount > 0 and not phone:
        return jsonify(success=False, code="DISCOUNT_PHONE_REQUIRED", message="Phone is required for the welcome discount."), 400

    try:
        order_items = _normalize_order_items(incoming_items)
    except ValueError as error:
        return jsonify(success=False, code="INVALID_KIOSK_ORDER", message=str(error)), 400

    try:
        if not os.environ.get("IIKO_KIOSK_API_KEY"):
            return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message="IIKO_KIOSK_API_KEY is not configured."), 503

        target, error_payload, status_code = _resolve_kiosk_order_target(point)
        if error_payload:
            return jsonify(error_payload), status_code

        rejection = check_order_availability(target, order_items)
        if rejection:
            return jsonify(rejection), 409

        organization_id = target["organizationId"]
        terminal_group_id = target["terminalGroupId"]

        order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"donerclub:kiosk:{point}:{process_id}"))
        external_number = process_id[-50:]
        payment_info, payment_error = _resolve_kiosk_payment(organization_id, terminal_group_id, point)
        if payment_error:
            return jsonify(success=False, **payment_error), 409
        payment_type_id = payment_info["id"]

        existing = _table_order_by_id(organization_id, order_id)
        if existing and existing.get("creationStatus") == "Success":
            existing_order = existing.get("order") or {}
            existing_sum = float(existing_order.get("sum") or 0)
            if abs(existing_sum - payment_sum) > 0.011:
                return jsonify(
                    success=False,
                    code="EXISTING_ORDER_SUM_MISMATCH",
                    message="Paid kiosk order already exists in iiko with a different sum. Do not charge again.",
                    orderId=order_id,
                    iiko=existing,
                ), 409
            if str(existing_order.get("status") or "") == "Closed":
                crm_result = _record_kiosk_order_in_crm(
                    data,
                    order_id=order_id,
                    order_number=existing_order.get("number"),
                    payment_sum=payment_sum,
                )
                return jsonify(
                    success=True,
                    duplicate=True,
                    message="Paid kiosk order was already completed.",
                    orderId=order_id,
                    order=existing_order,
                    target=target,
                    crmRecorded=bool(crm_result.get("ok")),
                )
            close_status, close_error, close_code = _close_kiosk_order(organization_id, order_id)
            if close_error:
                return jsonify(close_error), close_code
            crm_result = _record_kiosk_order_in_crm(
                data,
                order_id=order_id,
                order_number=existing_order.get("number"),
                payment_sum=payment_sum,
            )
            return jsonify(
                success=True,
                duplicate=True,
                message="Existing paid kiosk order was closed.",
                orderId=order_id,
                order=existing_order,
                close={"commandStatus": close_status},
                target=target,
                crmRecorded=bool(crm_result.get("ok")),
            )

        order_payload = {
            "id": order_id,
            "externalNumber": external_number,
            "items": order_items,
            "guests": {"count": 1, "splitBetweenPersons": False},
                "tabName": "",
            "payments": [{
                "paymentTypeKind": payment_info["paymentTypeKind"],
                "sum": payment_sum,
                "paymentTypeId": payment_type_id,
                "isProcessedExternally": True,
                "isFiscalizedExternally": False,
                "isPrepay": False,
            }],
        }
        if phone:
            order_payload["phone"] = phone

        payload = {
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "createOrderSettings": {
                "servicePrint": True,
                "transportToFrontTimeout": 10,
                "checkStopList": True,
            },
            "order": order_payload,
        }

        response = iiko_kiosk_post("/api/1/order/create", payload, timeout=45)
        if not response.ok:
            # A repeated deterministic order ID can surface as a create error.
            # Re-read it before telling the kiosk to retry.
            existing = _table_order_by_id(organization_id, order_id)
            if existing and existing.get("creationStatus") == "Success":
                existing_order = existing.get("order") or {}
                if abs(float(existing_order.get("sum") or 0) - payment_sum) <= 0.011:
                    close_status, close_error, close_code = _close_kiosk_order(organization_id, order_id)
                    if close_error:
                        return jsonify(close_error), close_code
                    crm_result = _record_kiosk_order_in_crm(
                        data,
                        order_id=order_id,
                        order_number=existing_order.get("number"),
                        payment_sum=payment_sum,
                    )
                    return jsonify(
                        success=True,
                        duplicate=True,
                        message="Paid kiosk order already existed and was closed.",
                        orderId=order_id,
                        order=existing_order,
                        close={"commandStatus": close_status},
                        target=target,
                        crmRecorded=bool(crm_result.get("ok")),
                    )
            return jsonify(
                success=False,
                code="IIKO_PAID_ORDER_CREATE_FAILED",
                statusCode=response.status_code,
                details=response.text[:3000],
                orderId=order_id,
            ), 502

        result = response.json()
        command_status = _wait_command(organization_id, result.get("correlationId"), attempts=10)
        order_info = result.get("orderInfo") or {}
        if command_status and command_status.get("state") == "Error":
            return jsonify(
                success=False,
                code="IIKO_PAID_ORDER_CREATE_COMMAND_ERROR",
                message="iikoFront reported an error while creating the paid kiosk order.",
                orderId=order_id,
                iiko=result,
                commandStatus=command_status,
            ), 502
        if not command_status or command_status.get("state") != "Success":
            return jsonify(
                success=False,
                code="IIKO_PAID_ORDER_CREATE_PENDING",
                message="Creation is not confirmed yet. Do not charge the guest again.",
                orderId=order_id,
                iiko=result,
                commandStatus=command_status,
            ), 202

        confirmed = _table_order_by_id(organization_id, order_id) or order_info
        confirmed_order = confirmed.get("order") if isinstance(confirmed, dict) else {}
        if not isinstance(confirmed_order, dict):
            confirmed_order = {}
        iiko_sum = float(confirmed_order.get("sum") or 0)
        if abs(iiko_sum - payment_sum) > 0.011:
            return jsonify(
                success=False,
                code="IIKO_SUM_MISMATCH",
                message="Payment succeeded, but the iiko order total differs. Do not charge again; cashier intervention is required.",
                paymentSum=payment_sum,
                iikoSum=iiko_sum,
                orderId=order_id,
                iiko=confirmed,
            ), 409

        close_status, close_error, close_code = _close_kiosk_order(organization_id, order_id)
        if close_error:
            return jsonify(close_error), close_code

        final_order = _table_order_by_id(organization_id, order_id)
        final_order_body = (final_order or {}).get("order") if isinstance(final_order, dict) else confirmed_order
        if not isinstance(final_order_body, dict):
            final_order_body = confirmed_order
        crm_result = _record_kiosk_order_in_crm(
            data,
            order_id=order_id,
            order_number=final_order_body.get("number"),
            payment_sum=payment_sum,
        )
        if not crm_result.get("ok"):
            print(
                "KIOSK_CRM_ORDER_WARNING " + json.dumps({
                    "orderId": order_id,
                    "reason": crm_result.get("reason"),
                    "statusCode": crm_result.get("statusCode"),
                }, ensure_ascii=False),
                flush=True,
            )
        return jsonify(
            success=True,
            message="Paid kiosk order was created and closed in iiko.",
            orderId=order_id,
            externalNumber=external_number,
            payment={
                "method": "KASPI_SMART_POS",
                "name": payment_info.get("name"),
                "paymentTypeId": payment_type_id if payment_sum > 0 else None,
                "paymentTypeKind": payment_info.get("paymentTypeKind") if payment_sum > 0 else None,
                "sum": payment_sum,
                "isProcessedExternally": bool(payment_info.get("isProcessedExternally", True)) if payment_sum > 0 else None,
                "isFiscalizedExternally": False if payment_sum > 0 else None,
            },
            servicePrintRequested=True,
            commandStatus=command_status,
            close={"commandStatus": close_status},
            order=final_order_body,
            target=target,
            crmRecorded=bool(crm_result.get("ok")),
        )

    except AvailabilityUnavailable:
        raise
    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify(success=False, code="IIKO_TIMEOUT", message="iiko did not answer in time. Do not charge again."), 504
    except requests.HTTPError as error:
        response = error.response
        return jsonify(
            success=False,
            code="IIKO_HTTP_ERROR",
            statusCode=response.status_code if response is not None else None,
            details=response.text[:3000] if response is not None else str(error),
        ), 502
    except Exception as error:
        return jsonify(success=False, code="KIOSK_PAID_ORDER_ERROR", message=str(error)), 500


@app.route("/kiosk-test-paid-order", methods=["POST"])
def kiosk_test_paid_order():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(success=False, code="INVALID_PAID_TEST_ORDER", message="JSON object required."), 400

    if (
        data.get("confirm") != "SEND_PAID_TEST_ORDER"
        or data.get("acknowledgeFinancialEffect") is not True
    ):
        return jsonify({
            "success": False,
            "code": "PAID_TEST_CONFIRMATION_REQUIRED",
            "message": "Paid test order was NOT sent. Explicit financial confirmation is required.",
        }), 400

    point = str(data.get("point") or "Arai").strip()
    terminal_group_id = str(data.get("terminalGroupId") or "").strip()
    incoming_items = data.get("items") or []

    try:
        payment_sum = round(float(data.get("paymentSum") or 0), 2)
    except (TypeError, ValueError, OverflowError):
        payment_sum = 0

    if isinstance(data.get("paymentSum"), bool) or not math.isfinite(payment_sum) or payment_sum <= 0 or payment_sum > 5000:
        return jsonify({
            "success": False,
            "code": "PAID_TEST_SUM_OUT_OF_RANGE",
            "message": "For the temporary paid test, paymentSum must be greater than 0 and no more than 5000 KZT.",
        }), 400

    if not terminal_group_id or not incoming_items:
        return jsonify({
            "success": False,
            "code": "INVALID_PAID_TEST_ORDER",
            "message": "terminalGroupId and items are required.",
        }), 400

    try:
        order_items = _normalize_order_items(incoming_items)
    except ValueError as error:
        return jsonify(success=False, code="INVALID_PAID_TEST_ORDER", message=str(error)), 400

    try:
        if not os.environ.get("IIKO_KIOSK_API_KEY"):
            return jsonify({
                "success": False,
                "code": "KIOSK_API_NOT_CONFIGURED",
                "message": "IIKO_KIOSK_API_KEY is not configured.",
            }), 503

        validated, error_payload, status_code = _validate_point_terminal(point, terminal_group_id)
        if error_payload:
            return jsonify(error_payload), status_code

        rejection = check_order_availability(validated, order_items)
        if rejection:
            return jsonify(rejection), 409

        organization_id = validated["organizationId"]
        payment_info, payment_error = _resolve_kiosk_payment(organization_id, terminal_group_id, point)
        if payment_error:
            return jsonify(success=False, **payment_error), 409
        payment_type_id = payment_info["id"]

        payload = {
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "createOrderSettings": {
                "servicePrint": True,
                "transportToFrontTimeout": 10,
                "checkStopList": True,
            },
            "order": {
                "items": order_items,
                "guests": {"count": 1, "splitBetweenPersons": False},
                "tabName": "",
                "payments": [{
                    "paymentTypeKind": payment_info["paymentTypeKind"],
                    "sum": payment_sum,
                    "paymentTypeId": payment_type_id,
                    "isProcessedExternally": True,
                    "isFiscalizedExternally": False,
                    "isPrepay": False,
                }],
            },
        }

        response = iiko_kiosk_post("/api/1/order/create", payload, timeout=45)
        if not response.ok:
            return jsonify({
                "success": False,
                "code": "IIKO_PAID_ORDER_CREATE_FAILED",
                "statusCode": response.status_code,
                "details": response.text[:3000],
            }), 502

        result = response.json()
        command_status = _wait_command(
            organization_id,
            result.get("correlationId"),
            attempts=10,
        )

        order_info = result.get("orderInfo") or {}
        order_id = str(order_info.get("id") or "").strip()
        if not order_id:
            return jsonify({
                "success": False,
                "code": "PAID_TEST_ORDER_ID_MISSING",
                "message": "Order was created but iiko did not return an order ID, so it was NOT closed.",
                "iiko": result,
                "commandStatus": command_status,
            }), 502

        if command_status and command_status.get("state") == "Error":
            return jsonify({
                "success": False,
                "code": "PAID_TEST_CREATE_COMMAND_ERROR",
                "message": "iikoFront reported an error while creating the paid test order. Close was NOT requested.",
                "iiko": result,
                "commandStatus": command_status,
            }), 502

        if not command_status or command_status.get("state") != "Success":
            return jsonify(
                success=False, code="PAID_TEST_CREATE_PENDING",
                message="Creation is not confirmed. Close was NOT requested. Check iiko before retrying.",
                orderId=order_id, iiko=result, commandStatus=command_status,
            ), 202

        close_response = iiko_kiosk_post(
            "/api/1/order/close",
            {"organizationId": organization_id, "orderId": order_id},
            timeout=45,
        )
        if not close_response.ok:
            return jsonify({
                "success": False,
                "code": "IIKO_PAID_ORDER_CLOSE_FAILED",
                "statusCode": close_response.status_code,
                "details": close_response.text[:3000],
                "orderId": order_id,
                "iiko": result,
            }), 502

        close_result = close_response.json()
        close_status = _wait_command(
            organization_id,
            close_result.get("correlationId"),
            attempts=12,
        )

        return jsonify({
            "success": bool(close_status and close_status.get("state") == "Success"),
            "message": "Paid test order was created and close was requested in iiko.",
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "payment": {
                "paymentTypeId": payment_type_id,
                "paymentTypeKind": payment_info["paymentTypeKind"],
                "sum": payment_sum,
                "isProcessedExternally": True,
                "isFiscalizedExternally": False,
            },
            "servicePrintRequested": True,
            "commandStatus": command_status,
            "close": {
                "requested": True,
                "correlationId": close_result.get("correlationId"),
                "commandStatus": close_status,
                "response": close_result,
            },
            "iiko": result,
        }), (200 if close_status and close_status.get("state") == "Success"
             else 502 if close_status and close_status.get("state") == "Error" else 202)

    except AvailabilityUnavailable:
        raise
    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify({"success": False, "code": "IIKO_TIMEOUT", "message": "iiko did not answer in time."}), 504
    except requests.HTTPError as error:
        response = error.response
        return jsonify({
            "success": False,
            "code": "IIKO_HTTP_ERROR",
            "statusCode": response.status_code if response is not None else None,
            "details": response.text[:3000] if response is not None else str(error),
        }), 502
    except Exception as error:
        return jsonify({"success": False, "code": "KIOSK_PAID_TEST_ORDER_ERROR", "message": str(error)}), 500






def _call_centre_key_authorized():
    expected_key = str(os.environ.get("CALL_CENTRE_API_KEY") or "").strip()
    supplied_key = str(request.headers.get("X-Call-Centre-Key") or "").strip()
    return bool(expected_key and supplied_key and hmac.compare_digest(expected_key, supplied_key))


def _collect_payment_type_rows(value, out):
    if isinstance(value, dict):
        kind = value.get("paymentTypeKind") or value.get("kind")
        if value.get("id") and value.get("name") and kind:
            out.append({
                "id": str(value.get("id")),
                "name": str(value.get("name") or ""),
                "paymentTypeKind": str(kind),
                "paymentProcessingType": str(value.get("paymentProcessingType") or ""),
                "terminalGroupIds": [
                    str(group.get("id"))
                    for group in (value.get("terminalGroups") or [])
                    if isinstance(group, dict) and group.get("id")
                ],
                "isDeleted": bool(value.get("isDeleted")),
            })
        for child in value.values():
            if isinstance(child, (dict, list)):
                _collect_payment_type_rows(child, out)
    elif isinstance(value, list):
        for child in value:
            _collect_payment_type_rows(child, out)


def _call_centre_payment_types(organization_id):
    response = iiko_kiosk_post(
        "/api/1/payment_types",
        {"organizationIds": [organization_id]},
        timeout=30,
    )
    if not response.ok:
        return [], {
            "code": "IIKO_PAYMENT_TYPES_FAILED",
            "statusCode": response.status_code,
            "details": response.text[:1000],
        }
    rows = []
    _collect_payment_type_rows(response.json(), rows)
    unique = {}
    for row in rows:
        if row["isDeleted"]:
            continue
        unique[row["id"]] = row
    return list(unique.values()), None


def _payment_name_key(value):
    return re.sub(r"[^a-zа-я0-9]+", " ", str(value or "").casefold()).strip()


def _is_technical_payment(row):
    return bool(set(_payment_name_key(row.get("name")).split()) & {"kiosk", "analytics"})


def _resolve_kiosk_payment(organization_id, terminal_group_id, point):
    prefix = _kiosk_point_env_prefix(point)
    payment_id = str(os.environ.get(f"IIKO_KIOSK_{prefix}_PAYMENT_TYPE_ID")
                     or os.environ.get("IIKO_KIOSK_PAYMENT_TYPE_ID") or "").strip()
    if not payment_id:
        return None, {"code": "KIOSK_PAYMENT_MAPPING_REQUIRED",
                      "message": "Настройте существующий тип оплаты iiko для полученной оплаты. Тестовый тип Kiosk не используется."}
    rows, error = _call_centre_payment_types(organization_id)
    if error:
        return None, error
    row = next((r for r in rows if r["id"] == payment_id), None)
    if (not row or row.get("paymentTypeKind") != "Card"
            or _is_technical_payment(row)
            or (row.get("terminalGroupIds") and terminal_group_id not in row["terminalGroupIds"])):
        return None, {"code": "KIOSK_PAYMENT_MAPPING_INVALID",
                      "message": "Нужен действующий тип безналичной оплаты этой точки; технические Kiosk/Analytics запрещены."}
    return dict(row, isProcessedExternally=True), None


def _find_internal_payment_type(rows, method, terminal_group_id=None):
    method = str(method or "").strip().upper()
    aliases = {
        "DEPOSIT": ("депозит", "deposit"),
        "FOOD": ("питание", "staff food", "stafffood", "еда персонала", "питание персонала", "стафф"),
    }
    candidates = aliases.get(method, ())
    for row in rows:
        groups = row.get("terminalGroupIds") or []
        if terminal_group_id and groups and str(terminal_group_id) not in groups:
            continue
        key = _payment_name_key(row.get("name"))
        if key in candidates or any(alias in key for alias in candidates):
            return row
    return None


def _resolve_call_centre_payment(organization_id, method, terminal_group_id=None):
    method = str(method or "REMOTE").strip().upper()
    if method == "REMOTE":
        rows, error = _call_centre_payment_types(organization_id)
        if error:
            return None, error

        override_id = str(os.environ.get("IIKO_CALL_CENTRE_BASE_PAYMENT_TYPE_ID") or "").strip()
        row = None
        if override_id:
            row = next((item for item in rows if str(item.get("id") or "") == override_id), None)
            if not row:
                return None, {
                    "code": "CALL_CENTRE_BASE_PAYMENT_TYPE_ID_NOT_FOUND",
                    "message": "IIKO_CALL_CENTRE_BASE_PAYMENT_TYPE_ID не найден среди типов оплаты iiko.",
                    "availablePaymentTypes": [x.get("name") for x in rows if x.get("name")][:80],
                }
        else:
            wanted = {"call centre base", "call center base"}
            candidates = []
            for item in rows:
                groups = item.get("terminalGroupIds") or []
                if terminal_group_id and groups and str(terminal_group_id) not in groups:
                    continue
                if _payment_name_key(item.get("name")) in wanted:
                    candidates.append(item)
            row = candidates[0] if candidates else None

        if not row:
            return None, {
                "code": "CALL_CENTRE_BASE_PAYMENT_TYPE_NOT_FOUND",
                "message": "Тип оплаты «CALL CENTRE BASE» не найден в iiko для этой точки.",
                "availablePaymentTypes": [x.get("name") for x in rows if x.get("name")][:80],
            }

        if _is_technical_payment(row):
            return None, {"code": "TECHNICAL_PAYMENT_TYPE_FORBIDDEN",
                          "message": "Технические типы оплаты Kiosk/Analytics больше не используются."}
        processing = str(row.get("paymentProcessingType") or "")
        return {
            "method": "REMOTE",
            "id": row["id"],
            "name": row["name"],
            "paymentTypeKind": row.get("paymentTypeKind") or "Card",
            "paymentProcessingType": processing,
            "isProcessedExternally": processing.casefold() == "external",
        }, None

    rows, error = _call_centre_payment_types(organization_id)
    if error:
        return None, error
    row = _find_internal_payment_type(rows, method, terminal_group_id)
    if not row:
        return None, {
            "code": "PAYMENT_TYPE_NOT_FOUND",
            "message": f"Тип оплаты {method} не найден в iiko.",
            "availablePaymentTypes": [x.get("name") for x in rows if x.get("name")][:50],
        }
    return {
        "method": method,
        "id": row["id"],
        "name": row["name"],
        "paymentTypeKind": row.get("paymentTypeKind") or "Card",
        "paymentProcessingType": row.get("paymentProcessingType") or "",
        "isProcessedExternally": str(row.get("paymentProcessingType") or "").casefold() == "external",
    }, None



def _call_centre_discounts(organization_id):
    response = iiko_kiosk_post(
        "/api/1/discounts",
        {"organizationIds": [organization_id]},
        timeout=30,
    )
    if not response.ok:
        return [], {
            "code": "IIKO_DISCOUNTS_FAILED",
            "statusCode": response.status_code,
            "details": response.text[:1200],
        }

    payload = response.json()
    rows = []
    for wrapper in payload.get("discounts") or []:
        if not isinstance(wrapper, dict):
            continue
        if str(wrapper.get("organizationId") or "") != str(organization_id):
            continue
        for item in wrapper.get("items") or []:
            if not isinstance(item, dict) or item.get("isDeleted"):
                continue
            row = {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "percent": float(item.get("percent") or 0),
                "sum": float(item.get("sum") or 0),
                "mode": str(item.get("mode") or ""),
                "comment": str(item.get("comment") or ""),
                "minOrderSum": float(item.get("minOrderSum") or 0),
                "isManual": bool(item.get("isManual")),
                "isCard": bool(item.get("isCard")),
                "isAutomatic": bool(item.get("isAutomatic")),
                "isCategorisedDiscount": bool(item.get("isCategorisedDiscount")),
                "canBeAppliedSelectively": bool(item.get("canBeAppliedSelectively")),
                "canApplyByCardNumber": bool(item.get("canApplyByCardNumber")),
                "productCategoryDiscounts": item.get("productCategoryDiscounts") or [],
            }
            if row["id"] and row["name"]:
                rows.append(row)
    return rows, None


def _call_centre_discount_supported(row):
    if not row or row.get("isCategorisedDiscount"):
        return False
    mode = str(row.get("mode") or "")
    if mode == "Percent":
        return float(row.get("percent") or 0) > 0
    if mode in {"FixedSum", "FlexibleSum"}:
        return float(row.get("sum") or 0) > 0
    return False


def _call_centre_discount_amount(row, subtotal):
    subtotal = max(0.0, float(subtotal or 0))
    if not row:
        return 0.0
    if subtotal + 1e-9 < float(row.get("minOrderSum") or 0):
        raise ValueError("ORDER_BELOW_DISCOUNT_MINIMUM")
    mode = str(row.get("mode") or "")
    if row.get("isCategorisedDiscount"):
        raise ValueError("CATEGORISED_DISCOUNT_NOT_SUPPORTED")
    if mode == "Percent":
        amount = subtotal * max(0.0, float(row.get("percent") or 0)) / 100.0
    elif mode in {"FixedSum", "FlexibleSum"}:
        amount = max(0.0, float(row.get("sum") or 0))
    else:
        raise ValueError("DISCOUNT_MODE_NOT_SUPPORTED")
    return round(min(subtotal, amount) + 1e-9, 2)


@app.get("/call-centre-discounts")
def call_centre_discounts():
    if not _call_centre_key_authorized():
        return jsonify(success=False, code="CALL_CENTRE_UNAUTHORIZED"), 401
    point = str(request.args.get("point") or "RESPUBLIKA").strip()
    try:
        department, _ = find_department(point)
        if not department:
            return jsonify(success=False, code="POINT_NOT_FOUND"), 404
        # Discounts are organization-scoped; terminal/table discovery is only
        # required when submitting an order, not when loading the selector.
        rows, error = _call_centre_discounts(department["organizationId"])
        if error:
            return jsonify(success=False, **error), 502
        return jsonify(
            success=True,
            point=point,
            discounts=[
                {
                    **row,
                    "supported": _call_centre_discount_supported(row),
                }
                for row in sorted(rows, key=lambda x: x.get("name", "").casefold())
            ],
        )
    except Exception as error:
        return jsonify(success=False, code="CALL_CENTRE_DISCOUNTS_ERROR", message=str(error)), 500


def _normalize_loyalty_phone(value):
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return ""
    return "+" + digits


def _normalize_loyalty_wallets(payload):
    rows = []
    for item in (payload.get("walletBalances") or []):
        if not isinstance(item, dict):
            continue
        wallet = item.get("wallet") if isinstance(item.get("wallet"), dict) else item
        try:
            balance = float(item.get("balance") if item.get("balance") is not None else wallet.get("balance") or 0)
        except (TypeError, ValueError, OverflowError):
            balance = 0.0
        rows.append({
            "id": str(wallet.get("id") or item.get("id") or ""),
            "name": str(wallet.get("name") or item.get("name") or ""),
            "type": wallet.get("type") if wallet.get("type") is not None else item.get("type"),
            "programType": wallet.get("programType") if wallet.get("programType") is not None else item.get("programType"),
            "balance": round(balance + 1e-9, 2),
        })
    return rows


@app.get("/call-centre-loyalty-customer")
def call_centre_loyalty_customer():
    if not _call_centre_key_authorized():
        return jsonify(success=False, code="CALL_CENTRE_UNAUTHORIZED"), 401

    point = str(request.args.get("point") or "RESPUBLIKA").strip()
    phone = _normalize_loyalty_phone(request.args.get("phone"))
    if not phone:
        return jsonify(success=False, code="INVALID_PHONE", message="Введите полный номер телефона сотрудника."), 400

    try:
        department, _ = find_department(point)
        if not department:
            return jsonify(success=False, code="POINT_NOT_FOUND"), 404

        response = iiko_kiosk_post(
            "/api/1/loyalty/iiko/customer/info",
            {
                "type": "phone",
                "phone": phone,
                "organizationId": department["organizationId"],
            },
            timeout=30,
        )
        if not response.ok:
            details = response.text[:1200]
            status = 404 if response.status_code in (400, 404) else 502
            return jsonify(
                success=False,
                code="LOYALTY_CUSTOMER_NOT_FOUND" if status == 404 else "IIKO_LOYALTY_LOOKUP_FAILED",
                message="Сотрудник не найден в iikoCard." if status == 404 else "Не удалось получить данные iikoCard.",
                statusCode=response.status_code,
                details=details,
            ), status

        data = response.json()
        categories = []
        for item in (data.get("categories") or []):
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                categories.append(name)

        wallets = _normalize_loyalty_wallets(data)
        nutrition_wallets = [
            row for row in wallets
            if row.get("type") == 0 or "пит" in str(row.get("name") or "").casefold()
        ]

        return jsonify(
            success=True,
            point=point,
            employee={
                "id": str(data.get("id") or ""),
                "phone": str(data.get("phone") or phone),
                "name": str(data.get("name") or ""),
                "surname": str(data.get("surname") or data.get("surName") or ""),
                "middleName": str(data.get("middleName") or ""),
                "categories": categories,
                "wallets": wallets,
                "nutritionWallets": nutrition_wallets,
            },
        )
    except Exception as error:
        return jsonify(success=False, code="LOYALTY_LOOKUP_ERROR", message=str(error)), 500


@app.get("/call-centre-payment-options")
def call_centre_payment_options():
    if not _call_centre_key_authorized():
        return jsonify(success=False, code="CALL_CENTRE_UNAUTHORIZED"), 401
    point = str(request.args.get("point") or "RESPUBLIKA").strip()
    try:
        target, error_payload, status_code = _resolve_kiosk_order_target(point)
        if error_payload:
            return jsonify(error_payload), status_code
        organization_id = target["organizationId"]
        rows, payment_error = _call_centre_payment_types(organization_id)
        remote_payment, remote_error = _resolve_call_centre_payment(
            organization_id,
            "REMOTE",
            target.get("terminalGroupId"),
        )
        options = [{
            "code": "REMOTE",
            "name": remote_payment.get("name") if remote_payment else "CALL CENTRE BASE",
            "available": bool(remote_payment),
            "paymentTypeId": remote_payment.get("id") if remote_payment else None,
            "paymentTypeKind": remote_payment.get("paymentTypeKind") if remote_payment else None,
            "paymentProcessingType": remote_payment.get("paymentProcessingType") if remote_payment else None,
        }]
        for code, label in (("DEPOSIT", "Депозит"), ("FOOD", "Питание")):
            row = _find_internal_payment_type(rows, code, target.get("terminalGroupId")) if not payment_error else None
            options.append({
                "code": code,
                "name": row.get("name") if row else label,
                "available": bool(row),
                "paymentTypeId": row.get("id") if row else None,
            })
        return jsonify(
            success=True,
            point=point,
            options=options,
            warning=payment_error or remote_error,
        )
    except Exception as error:
        return jsonify(success=False, code="CALL_CENTRE_OPTIONS_ERROR", message=str(error)), 500



def _call_centre_items_subtotal(order_items):
    total = 0.0
    for item in order_items or []:
        amount = float(item.get("amount") or 0)
        price = float(item.get("price") or 0)
        total += price * amount
        for modifier in item.get("modifiers") or []:
            modifier_amount = float(modifier.get("amount") or 0)
            modifier_price = float(modifier.get("price") or 0)
            total += modifier_price * modifier_amount * amount
    return round(total + 1e-9, 2)


@app.post("/call-centre-order")
def call_centre_order():
    expected_key = str(os.environ.get("CALL_CENTRE_API_KEY") or "").strip()
    supplied_key = str(request.headers.get("X-Call-Centre-Key") or "").strip()
    if not expected_key:
        return jsonify(success=False, code="CALL_CENTRE_NOT_CONFIGURED"), 503
    if not supplied_key or not hmac.compare_digest(expected_key, supplied_key):
        return jsonify(success=False, code="CALL_CENTRE_UNAUTHORIZED"), 401

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(success=False, code="INVALID_CALL_CENTRE_ORDER", message="JSON object required."), 400
    payment_method = "REMOTE"
    expected_confirmation = "CLIENT_PAID"
    if data.get("confirm") != expected_confirmation:
        return jsonify(
            success=False,
            code="PAYMENT_CONFIRMATION_REQUIRED",
            message="Order confirmation does not match the selected payment method.",
        ), 400

    point = str(data.get("point") or "RESPUBLIKA").strip()
    request_id = str(data.get("requestId") or "").strip()
    operator = str(data.get("operator") or "operator").strip()[:80]
    phone = str(data.get("phone") or "").strip() or None
    discount_id = str(data.get("discountId") or "").strip()
    discount_name = str(data.get("discountName") or "").strip()[:160]
    try:
        requested_discount_sum = float(data.get("discountSum") or 0)
    except (TypeError, ValueError, OverflowError):
        requested_discount_sum = -1
    incoming_items = data.get("items") or []

    if not request_id or len(request_id) > 120:
        return jsonify(success=False, code="REQUEST_ID_REQUIRED"), 400

    try:
        payment_sum = float(data.get("paymentSum"))
    except (TypeError, ValueError, OverflowError):
        payment_sum = 0
    if isinstance(data.get("paymentSum"), bool) or not math.isfinite(payment_sum) or payment_sum < 0 or payment_sum > 500000:
        return jsonify(success=False, code="INVALID_PAYMENT_SUM"), 400

    try:
        order_items = _normalize_order_items(incoming_items)
    except ValueError as error:
        return jsonify(success=False, code="INVALID_CALL_CENTRE_ORDER", message=str(error)), 400
    if not order_items:
        return jsonify(success=False, code="EMPTY_ORDER"), 400

    try:
        if not os.environ.get("IIKO_KIOSK_API_KEY"):
            return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED"), 503

        target, error_payload, status_code = _resolve_kiosk_order_target(point)
        if error_payload:
            return jsonify(error_payload), status_code

        rejection = check_order_availability(target, order_items)
        if rejection:
            return jsonify(rejection), 409

        organization_id = target["organizationId"]
        terminal_group_id = target["terminalGroupId"]
        subtotal = _call_centre_items_subtotal(order_items)
        applied_discount = None
        discount_sum = 0.0
        if discount_id:
            discount_rows, discount_error = _call_centre_discounts(organization_id)
            if discount_error:
                return jsonify(success=False, **discount_error), 502
            applied_discount = next((row for row in discount_rows if row.get("id") == discount_id), None)
            if not applied_discount:
                return jsonify(
                    success=False,
                    code="DISCOUNT_NOT_FOUND",
                    message="Selected iiko discount is no longer available.",
                ), 409
            if not _call_centre_discount_supported(applied_discount):
                return jsonify(
                    success=False,
                    code="DISCOUNT_NOT_SUPPORTED",
                    message="This iiko discount cannot be safely applied by CALL CENTRE.",
                    discount=applied_discount,
                ), 409
            try:
                discount_sum = _call_centre_discount_amount(applied_discount, subtotal)
            except ValueError as error:
                return jsonify(
                    success=False,
                    code=str(error),
                    message="Selected iiko discount cannot be applied to this order.",
                    discount=applied_discount,
                ), 409
            if requested_discount_sum < 0 or abs(requested_discount_sum - discount_sum) > 0.011:
                return jsonify(
                    success=False,
                    code="DISCOUNT_SUM_MISMATCH",
                    expectedDiscountSum=discount_sum,
                    requestedDiscountSum=requested_discount_sum,
                ), 409

        expected_payment_sum = round(max(0.0, subtotal - discount_sum) + 1e-9, 2)
        if abs(payment_sum - expected_payment_sum) > 0.011:
            return jsonify(
                success=False,
                code="PAYMENT_SUM_MISMATCH",
                subtotal=subtotal,
                discountSum=discount_sum,
                expectedPaymentSum=expected_payment_sum,
                paymentSum=payment_sum,
            ), 409
        order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"donerclub:call-centre:{point}:{request_id}"))
        external_number = request_id[-50:]
        payment_info, payment_error = _resolve_call_centre_payment(
            organization_id,
            "REMOTE",
            terminal_group_id,
        )
        if payment_error:
            return jsonify(success=False, **payment_error), 409
        payment_type_id = payment_info["id"]

        existing = _table_order_by_id(organization_id, order_id)
        if existing and existing.get("creationStatus") == "Success":
            existing_order = existing.get("order") or {}
            if abs(float(existing_order.get("sum") or 0) - payment_sum) > 0.011:
                return jsonify(
                    success=False,
                    code="EXISTING_ORDER_SUM_MISMATCH",
                    message="Call-centre order already exists in iiko with another sum. Do not resend payment.",
                    orderId=order_id,
                ), 409
            if str(existing_order.get("status") or "") != "Closed":
                close_status, close_error, close_code = _close_kiosk_order(organization_id, order_id)
                if close_error:
                    return jsonify(close_error), close_code
            else:
                close_status = None
            return jsonify(
                success=True,
                duplicate=True,
                orderId=order_id,
                externalNumber=external_number,
                order=existing_order,
                close={"commandStatus": close_status},
                target=target,
            )

        order_payload = {
            "id": order_id,
            "externalNumber": external_number,
            "items": order_items,
            "guests": {"count": 1, "splitBetweenPersons": False},
                "tabName": "",
        }
        if applied_discount:
            order_payload["discountsInfo"] = {
                "discounts": [{
                    "discountTypeId": applied_discount["id"],
                    "sum": discount_sum,
                    "type": "RMS",
                }],
                "fixedLoyaltyDiscounts": True,
            }
        if payment_sum > 0:
            order_payload["payments"] = [{
                "paymentTypeKind": payment_info.get("paymentTypeKind") or "Card",
                "sum": payment_sum,
                "paymentTypeId": payment_type_id,
                "isProcessedExternally": bool(payment_info.get("isProcessedExternally", True)),
                "isFiscalizedExternally": False,
                "isPrepay": False,
            }]
        if phone:
            order_payload["phone"] = phone

        payload = {
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "createOrderSettings": {
                "servicePrint": True,
                "transportToFrontTimeout": 10,
                "checkStopList": True,
            },
            "order": order_payload,
        }

        response = iiko_kiosk_post("/api/1/order/create", payload, timeout=45)
        if not response.ok:
            existing = _table_order_by_id(organization_id, order_id)
            if existing and existing.get("creationStatus") == "Success":
                existing_order = existing.get("order") or {}
                if abs(float(existing_order.get("sum") or 0) - payment_sum) <= 0.011:
                    close_status, close_error, close_code = _close_kiosk_order(organization_id, order_id)
                    if close_error:
                        return jsonify(close_error), close_code
                    return jsonify(
                        success=True,
                        duplicate=True,
                        orderId=order_id,
                        externalNumber=external_number,
                        order=existing_order,
                        close={"commandStatus": close_status},
                        target=target,
                    )
            return jsonify(
                success=False,
                code="IIKO_CALL_CENTRE_ORDER_CREATE_FAILED",
                statusCode=response.status_code,
                details=response.text[:3000],
                orderId=order_id,
            ), 502

        result = response.json()
        command_status = _wait_command(organization_id, result.get("correlationId"), attempts=10)
        if command_status and command_status.get("state") == "Error":
            return jsonify(
                success=False,
                code="IIKO_CALL_CENTRE_CREATE_COMMAND_ERROR",
                orderId=order_id,
                commandStatus=command_status,
                iiko=result,
            ), 502
        if not command_status or command_status.get("state") != "Success":
            return jsonify(
                success=False,
                code="IIKO_CALL_CENTRE_CREATE_PENDING",
                message="Creation is not confirmed. Check iiko before retrying.",
                orderId=order_id,
                commandStatus=command_status,
                iiko=result,
            ), 202

        confirmed = _table_order_by_id(organization_id, order_id) or (result.get("orderInfo") or {})
        confirmed_order = confirmed.get("order") if isinstance(confirmed, dict) else {}
        if not isinstance(confirmed_order, dict):
            confirmed_order = {}
        iiko_sum = float(confirmed_order.get("sum") or 0)
        if abs(iiko_sum - payment_sum) > 0.011:
            return jsonify(
                success=False,
                code="IIKO_SUM_MISMATCH",
                message="Client paid, but iiko total differs. Do not charge again.",
                paymentSum=payment_sum,
                iikoSum=iiko_sum,
                orderId=order_id,
            ), 409

        close_status, close_error, close_code = _close_kiosk_order(organization_id, order_id)
        if close_error:
            return jsonify(close_error), close_code

        final_order = _table_order_by_id(organization_id, order_id)
        final_order_body = (final_order or {}).get("order") if isinstance(final_order, dict) else confirmed_order
        if not isinstance(final_order_body, dict):
            final_order_body = confirmed_order

        return jsonify(
            success=True,
            orderId=order_id,
            externalNumber=external_number,
            order=final_order_body,
            payment={
                "paymentTypeId": payment_type_id if payment_sum > 0 else None,
                "paymentTypeKind": payment_info.get("paymentTypeKind") if payment_sum > 0 else None,
                "sum": payment_sum,
                "isProcessedExternally": bool(payment_info.get("isProcessedExternally", True)) if payment_sum > 0 else None,
                "isFiscalizedExternally": False if payment_sum > 0 else None,
            },
            discount={
                "id": applied_discount.get("id") if applied_discount else None,
                "name": applied_discount.get("name") if applied_discount else None,
                "sum": discount_sum,
            },
            servicePrintRequested=True,
            commandStatus=command_status,
            close={"commandStatus": close_status},
            target=target,
        )
    except AvailabilityUnavailable:
        raise
    except ConfigurationError as error:
        return jsonify(success=False, code="KIOSK_API_NOT_CONFIGURED", message=str(error)), 503
    except requests.Timeout:
        return jsonify(success=False, code="IIKO_TIMEOUT", message="iiko did not answer in time. Do not resend payment."), 504
    except Exception as error:
        return jsonify(success=False, code="CALL_CENTRE_ORDER_ERROR", message=str(error)), 500




REPUBLIC_ORGANIZATION_ID = "9f2c2c10-a4e8-4e80-ac1d-beedf7d5182e"
ARAI_ORGANIZATION_ID = "58e718ee-54ec-4604-beff-a172cc016879"
IIKO_WEBHOOK_POINTS = {
    REPUBLIC_ORGANIZATION_ID.casefold(): "RESPUBLIKA",
    ARAI_ORGANIZATION_ID.casefold(): "ARAI",
}
IIKO_ITEM_STATUSES = {
    "Added",
    "PrintedNotCooking",
    "CookingStarted",
    "CookingCompleted",
    "Served",
}


def _iiko_webhook_authorized():
    expected = str(os.environ.get("IIKO_WEBHOOK_AUTH_TOKEN") or "").strip()
    if not expected:
        # Fail closed in production: iiko webhook receiver is useless without
        # a shared secret and must not accept arbitrary public requests.
        return False

    supplied = str(request.headers.get("Authorization") or "").strip()
    candidates = [supplied]
    if supplied.lower().startswith("bearer "):
        candidates.append(supplied[7:].strip())
    return any(hmac.compare_digest(expected, value) for value in candidates if value)


def _collect_iiko_item_statuses(value, out):
    if isinstance(value, dict):
        status = value.get("status")
        if status in IIKO_ITEM_STATUSES:
            out.append(str(status))
        for key, child in value.items():
            if key in {"customer", "payments"}:
                continue
            if isinstance(child, (dict, list)):
                _collect_iiko_item_statuses(child, out)
    elif isinstance(value, list):
        for child in value:
            _collect_iiko_item_statuses(child, out)


def _guest_kitchen_stage(order):
    statuses = []
    _collect_iiko_item_statuses((order or {}).get("items") or [], statuses)
    unique = sorted(set(statuses))

    if not statuses:
        return "unknown", unique
    if all(status == "Served" for status in statuses):
        return "served", unique
    if all(status in {"CookingCompleted", "Served"} for status in statuses):
        return "ready", unique
    if any(status == "CookingStarted" for status in statuses):
        return "cooking", unique
    if any(status in {"Added", "PrintedNotCooking"} for status in statuses):
        return "accepted", unique
    return "unknown", unique


@app.route("/iiko/webhook", methods=["GET", "POST"])
def iiko_webhook():
    if request.method == "GET":
        return jsonify(
            success=True,
            service="Doner Club iiko webhook",
            configured=bool(os.environ.get("IIKO_WEBHOOK_AUTH_TOKEN")),
        )

    if not _iiko_webhook_authorized():
        return jsonify(success=False, code="UNAUTHORIZED"), 401

    payload = request.get_json(silent=True)
    if payload is None:
        # iiko may omit/alter Content-Type while still sending valid JSON.
        # Parse the raw body as a fallback.
        raw = request.get_data(cache=True, as_text=True)
        try:
            payload = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            payload = None

    # iikoCloud webhook notifications are delivered as a JSON array, even when
    # there is only one event. Keep dict support for manual diagnostics.
    if isinstance(payload, list):
        events = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        events = [payload]
    else:
        return jsonify(success=False, code="INVALID_JSON"), 400

    tracked = 0
    ignored = 0
    for event in events:
        organization_id = str(event.get("organizationId") or "").strip()
        point_code = IIKO_WEBHOOK_POINTS.get(organization_id.casefold())
        if not point_code:
            ignored += 1
            continue

        event_type = str(event.get("eventType") or "").strip()
        event_info = event.get("eventInfo") if isinstance(event.get("eventInfo"), dict) else {}
        order = event_info.get("order") if isinstance(event_info.get("order"), dict) else {}
        guest_stage, item_statuses = _guest_kitchen_stage(order)

        diagnostic = {
            "eventType": event_type,
            "point": point_code,
            "organizationId": organization_id,
            "orderId": event_info.get("id"),
            "posId": event_info.get("posId"),
            "externalNumber": event_info.get("externalNumber"),
            "creationStatus": event_info.get("creationStatus"),
            "orderStatus": order.get("status"),
            "itemStatuses": item_statuses,
            "guestStage": guest_stage,
        }
        print(
            "IIKO_WEBHOOK_EVENT " + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
        tracked += 1

    # Telegram delivery stays disabled until we confirm the real KDS sequence.
    return jsonify(success=True, received=len(events), tracked=tracked, ignored=ignored)




@app.post("/kiosk-crm-register")
def kiosk_crm_register():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(success=False, code="INVALID_REQUEST", message="Expected a JSON object"), 400
    phone = str(payload.get("phone") or "").strip()
    if not phone:
        return jsonify(success=False, code="PHONE_REQUIRED", message="phone is required"), 400
    if not CRM_BASE_URL or not CRM_API_KEY:
        return jsonify(success=False, code="CRM_NOT_CONFIGURED", message="CRM connection is not configured"), 503

    response = None
    last_error = None
    retryable_statuses = {429, 502, 503, 504}
    location_code = _kiosk_point_env_prefix(payload.get("locationCode") or payload.get("point") or "ARAI")

    for attempt in range(4):
        try:
            response = requests.post(
                f"{CRM_BASE_URL}/api/v1/customers/register",
                json={"phone": phone, "source": "KIOSK", "locationCode": location_code},
                headers={"X-API-Key": CRM_API_KEY, "Content-Type": "application/json"},
                timeout=(4, 12),
            )
            if response.status_code not in retryable_statuses:
                break
            last_error = f"CRM HTTP {response.status_code}"
        except requests.RequestException as error:
            last_error = str(error)
            response = None

        if attempt < 3:
            time.sleep(0.8 + attempt * 0.8)

    if response is None:
        return jsonify(success=False, code="CRM_UNAVAILABLE", message=last_error or "CRM unavailable"), 502

    try:
        data = response.json()
    except ValueError:
        return jsonify(success=False, code="CRM_INVALID_RESPONSE", message="CRM returned invalid JSON"), 502

    if not response.ok or not isinstance(data, dict) or not data.get("ok"):
        return jsonify(
            success=False,
            code="CRM_REGISTER_FAILED",
            statusCode=response.status_code,
            details=data if isinstance(data, dict) else None,
        ), 502 if response.status_code >= 500 else response.status_code

    return jsonify(
        success=True,
        created=bool(data.get("created")),
        customerId=data.get("customerId"),
        phone=data.get("phone"),
    )


@app.route("/kiosk-crm-customer", methods=["GET"])
def kiosk_crm_customer():
    phone = str(request.args.get("phone") or "").strip()
    if not phone:
        return jsonify(success=False, code="PHONE_REQUIRED", message="phone is required"), 400
    if not CRM_BASE_URL or not CRM_API_KEY:
        return jsonify(success=False, code="CRM_NOT_CONFIGURED", message="CRM connection is not configured"), 503

    response = None
    last_error = None
    retryable_statuses = {429, 502, 503, 504}

    # The CRM runs on a free Render service and may need time to wake up.
    # Retry the same idempotent lookup before showing an error to the guest.
    for attempt in range(3):
        try:
            response = requests.get(
                f"{CRM_BASE_URL}/api/v1/customers/lookup",
                params={"phone": phone},
                headers={"X-API-Key": CRM_API_KEY},
                timeout=(5, 15),
            )
            if response.status_code not in retryable_statuses:
                break
            last_error = f"CRM HTTP {response.status_code}"
        except requests.Timeout:
            last_error = "CRM did not answer in time"
            response = None
        except requests.RequestException as error:
            last_error = str(error)
            response = None

        if attempt < 2:
            time.sleep(1.5 + attempt)

    if response is None:
        return jsonify(
            success=False,
            code="CRM_UNAVAILABLE",
            message=last_error or "CRM is temporarily unavailable",
        ), 502

    try:
        payload = response.json()
    except ValueError:
        return jsonify(success=False, code="CRM_INVALID_RESPONSE", message="CRM returned invalid JSON"), 502
    if not isinstance(payload, dict):
        return jsonify(success=False, code="CRM_INVALID_RESPONSE", message="CRM returned an invalid response"), 502

    if not response.ok:
        return jsonify(
            success=False,
            code="CRM_LOOKUP_FAILED",
            statusCode=response.status_code,
            details=payload,
        ), 502 if response.status_code >= 500 else response.status_code

    return jsonify(
        success=True,
        exists=bool(payload.get("exists")),
        customerId=payload.get("customerId"),
        phone=payload.get("phone"),
        welcomeDiscount=payload.get("welcomeDiscount") or {},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
