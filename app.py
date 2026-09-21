import os
import threading
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

IIKO_BASE_URL = "https://api-ru.iiko.services"
TOKEN_TTL_SECONDS = 50 * 60
DEPARTMENTS_TTL_SECONDS = 10 * 60

_read_token_cache = {"value": None, "expires_at": 0.0}
_kiosk_token_cache = {"value": None, "expires_at": 0.0}
_departments_cache = {"value": None, "expires_at": 0.0}

_read_token_lock = threading.Lock()
_kiosk_token_lock = threading.Lock()
_departments_lock = threading.Lock()


def _cache_valid(cache):
    return cache.get("value") is not None and cache.get("expires_at", 0) > time.time()


def _get_token(api_key, cache, lock, force_refresh=False):
    if not api_key:
        raise RuntimeError("iikoCloud API key is not configured")
    if not force_refresh and _cache_valid(cache):
        return cache["value"]

    with lock:
        if not force_refresh and _cache_valid(cache):
            return cache["value"]

        app_id = os.environ.get("IIKO_KIOSK_APP_ID") or os.environ.get("IIKO_APP_ID")
        client_secret = os.environ.get("IIKO_KIOSK_CLIENT_SECRET") or os.environ.get("IIKO_CLIENT_SECRET")
        if not app_id or not client_secret:
            raise RuntimeError("iikoCloud appId/clientSecret are not configured")

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
        raise RuntimeError("Dedicated IIKO_KIOSK_API_KEY is not configured")
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


def find_department(point):
    normalized = (point or "").strip().lower()
    departments = get_departments()

    for department in departments:
        code = (department.get("code") or "").strip().lower()
        name = (department.get("name") or "").strip().lower()
        if code == normalized or name == normalized:
            return department, departments

    for department in departments:
        code = (department.get("code") or "").strip().lower()
        name = (department.get("name") or "").strip().lower()
        if normalized in code or normalized in name:
            return department, departments

    return None, departments


def get_external_menus(organization_id):
    response = iiko_post(
        "/api/2/menu",
        {"organizationIds": [organization_id]},
        timeout=35,
    )
    response.raise_for_status()
    return response.json()


def get_external_menu_by_id(external_menu_id, organization_id):
    response = iiko_post(
        "/api/2/menu/by_id",
        {
            "externalMenuId": str(external_menu_id),
            "organizationIds": [organization_id],
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def _menu_price(prices, organization_id):
    prices = prices or []
    for entry in prices:
        if str(entry.get("organizationId")) == str(organization_id):
            try:
                return round(float(entry.get("price")), 2)
            except (TypeError, ValueError):
                pass
    for entry in prices:
        try:
            return round(float(entry.get("price")), 2)
        except (TypeError, ValueError):
            continue
    return None


def _normalize_modifier_item(item, organization_id):
    prices = item.get("prices") or []
    image_url = item.get("buttonImage") or item.get("buttonImageUrl")
    restrictions = item.get("restrictions") or {}

    if not prices:
        sizes = item.get("itemSizes") or []
        size = next((s for s in sizes if s.get("isDefault")), None) or (sizes[0] if sizes else {})
        prices = size.get("prices") or []
        image_url = image_url or size.get("buttonImageUrl")
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
                    "imageUrl": (
                        size.get("buttonImageUrl")
                        or category.get("buttonImageUrl")
                        or category.get("headerImageUrl")
                    ),
                    "modifierGroups": modifier_groups,
                })

    return categories, products


def get_terminal_groups_for_organization(organization_id):
    response = iiko_post(
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

    response = iiko_post(
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
    order_items = []

    for source_item in incoming_items:
        product_id = str(source_item.get("productId") or "").strip()
        if not product_id:
            continue

        try:
            amount = float(source_item.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0

        if amount <= 0:
            continue

        order_item = {
            "productId": product_id,
            "type": "Product",
            "amount": amount,
        }

        product_size_id = source_item.get("productSizeId")
        if product_size_id:
            order_item["productSizeId"] = str(product_size_id)

        modifiers = []
        for modifier in source_item.get("modifiers") or []:
            modifier_product_id = str(modifier.get("productId") or "").strip()
            if not modifier_product_id:
                continue

            modifier_payload = {
                "productId": modifier_product_id,
                "amount": float(modifier.get("amount") or 1),
            }
            product_group_id = modifier.get("productGroupId")
            if product_group_id:
                modifier_payload["productGroupId"] = str(product_group_id)
            modifiers.append(modifier_payload)

        if modifiers:
            order_item["modifiers"] = modifiers

        order_items.append(order_item)

    return order_items


def _validate_point_terminal_table(point, terminal_group_id, table_id):
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
            "message": "Selected terminal group does not belong to Arai.",
        }, 400

    sections, _ = get_restaurant_sections_for_terminal_groups([terminal_group_id])
    allowed_table_ids = {
        str(table.get("id"))
        for section in sections
        for table in (section.get("tables") or [])
        if table.get("id")
    }

    if table_id not in allowed_table_ids:
        return None, {
            "success": False,
            "code": "INVALID_TABLE",
            "message": "Selected table is not available for this terminal group.",
        }, 400

    return {
        "department": department,
        "organizationId": organization_id,
    }, None, 200


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
def home():
    return app.send_static_file("kiosk-preview.html")


@app.route("/health")
def health():
    return jsonify({"service": "Doner Club Kiosk", "status": "online"})


@app.route("/kiosk-menu")
def kiosk_menu():
    point = request.args.get("point", "Arai").strip()
    requested_menu = request.args.get("menu", "Kiosk Арай").strip()

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

        selected = next(
            (
                menu for menu in external_menus
                if (menu.get("name") or "").strip().casefold() == requested_menu.casefold()
            ),
            None,
        )

        if not selected:
            response = jsonify({
                "success": False,
                "code": "EXTERNAL_MENU_NOT_FOUND",
                "message": f"External menu '{requested_menu}' is not available for point '{point}'.",
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
        sections, _ = get_restaurant_sections_for_terminal_groups(terminal_group_ids)

        for group in terminal_groups:
            group["isAlive"] = alive.get(str(group["id"]))

        tables = []
        for section in sections:
            tables.extend(section.get("tables") or [])

        response = jsonify({
            "success": True,
            "point": {
                "code": department.get("code"),
                "name": department.get("name"),
                "organizationId": organization_id,
            },
            "terminalGroups": terminal_groups,
            "restaurantSections": sections,
            "tables": tables,
            "canSendTestOrder": bool(terminal_groups and tables),
        })
        response.headers["Cache-Control"] = "no-store"
        return response

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

    if data.get("confirm") != "SEND_TEST_ORDER":
        return jsonify({
            "success": False,
            "code": "EXPLICIT_CONFIRMATION_REQUIRED",
            "message": "Test order was NOT sent. Explicit confirmation is required.",
        }), 400

    point = (data.get("point") or "Arai").strip()
    terminal_group_id = str(data.get("terminalGroupId") or "").strip()
    table_id = str(data.get("tableId") or "").strip()
    incoming_items = data.get("items") or []

    if not terminal_group_id or not table_id or not incoming_items:
        return jsonify({
            "success": False,
            "code": "INVALID_TEST_ORDER",
            "message": "terminalGroupId, tableId and items are required.",
        }), 400

    try:
        if not os.environ.get("IIKO_KIOSK_API_KEY"):
            return jsonify({
                "success": False,
                "code": "KIOSK_API_NOT_CONFIGURED",
                "message": "IIKO_KIOSK_API_KEY is not configured.",
            }), 503

        validated, error_payload, status_code = _validate_point_terminal_table(
            point, terminal_group_id, table_id
        )
        if error_payload:
            return jsonify(error_payload), status_code

        order_items = _normalize_order_items(incoming_items)
        if not order_items:
            return jsonify({
                "success": False,
                "code": "NO_VALID_ITEMS",
                "message": "No valid order items were supplied.",
            }), 400

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
                "tableIds": [table_id],
                "items": order_items,
                "guests": {"count": 1, "splitBetweenPersons": False},
                "comment": "TEST KIOSK — БЕЗ ОПЛАТЫ",
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

        return jsonify({
            "success": True,
            "message": "Test order request was sent to iiko.",
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "tableId": table_id,
            "servicePrintRequested": True,
            "commandStatus": command_status,
            "iiko": result,
        })

    except requests.Timeout:
        return jsonify({"success": False, "code": "IIKO_TIMEOUT", "message": "iiko did not answer in time."}), 504
    except Exception as error:
        return jsonify({"success": False, "code": "KIOSK_TEST_ORDER_ERROR", "message": str(error)}), 500


@app.route("/kiosk-test-paid-order", methods=["POST"])
def kiosk_test_paid_order():
    data = request.get_json(silent=True) or {}

    if (
        data.get("confirm") != "SEND_PAID_TEST_ORDER"
        or data.get("acknowledgeFinancialEffect") is not True
    ):
        return jsonify({
            "success": False,
            "code": "PAID_TEST_CONFIRMATION_REQUIRED",
            "message": "Paid test order was NOT sent. Explicit financial confirmation is required.",
        }), 400

    point = (data.get("point") or "Arai").strip()
    terminal_group_id = str(data.get("terminalGroupId") or "").strip()
    table_id = str(data.get("tableId") or "").strip()
    incoming_items = data.get("items") or []

    try:
        payment_sum = round(float(data.get("paymentSum") or 0), 2)
    except (TypeError, ValueError):
        payment_sum = 0

    if payment_sum <= 0 or payment_sum > 5000:
        return jsonify({
            "success": False,
            "code": "PAID_TEST_SUM_OUT_OF_RANGE",
            "message": "For the temporary paid test, paymentSum must be greater than 0 and no more than 5000 KZT.",
        }), 400

    if not terminal_group_id or not table_id or not incoming_items:
        return jsonify({
            "success": False,
            "code": "INVALID_PAID_TEST_ORDER",
            "message": "terminalGroupId, tableId and items are required.",
        }), 400

    try:
        if not os.environ.get("IIKO_KIOSK_API_KEY"):
            return jsonify({
                "success": False,
                "code": "KIOSK_API_NOT_CONFIGURED",
                "message": "IIKO_KIOSK_API_KEY is not configured.",
            }), 503

        validated, error_payload, status_code = _validate_point_terminal_table(
            point, terminal_group_id, table_id
        )
        if error_payload:
            return jsonify(error_payload), status_code

        order_items = _normalize_order_items(incoming_items)
        if not order_items:
            return jsonify({
                "success": False,
                "code": "NO_VALID_ITEMS",
                "message": "No valid order items were supplied.",
            }), 400

        organization_id = validated["organizationId"]
        payment_type_id = os.environ.get(
            "IIKO_KIOSK_PAYMENT_TYPE_ID",
            "d89a8bf4-b3d1-4625-8de3-6b0ef162e0c3",
        )

        payload = {
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "createOrderSettings": {
                "servicePrint": True,
                "transportToFrontTimeout": 10,
                "checkStopList": True,
            },
            "order": {
                "tableIds": [table_id],
                "items": order_items,
                "guests": {"count": 1, "splitBetweenPersons": False},
                "payments": [{
                    "paymentTypeKind": "Card",
                    "sum": payment_sum,
                    "paymentTypeId": payment_type_id,
                    "isProcessedExternally": True,
                    "isFiscalizedExternally": False,
                    "isPrepay": False,
                }],
                "comment": "PAID TEST KIOSK — ВНЕШНЯЯ ТЕСТОВАЯ ОПЛАТА",
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
            "success": bool(not close_status or close_status.get("state") != "Error"),
            "message": "Paid test order was created and close was requested in iiko.",
            "organizationId": organization_id,
            "terminalGroupId": terminal_group_id,
            "tableId": table_id,
            "payment": {
                "paymentTypeId": payment_type_id,
                "paymentTypeKind": "Card",
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
        })

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
