# cart/views.py
import json
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from inventory.models import Product
from .models import Cart, displayed_items


# ============================================================
# Money helpers
# ============================================================

def safe_decimal(value, default=Decimal("0.00")):
    if value is None or value == "":
        return default

    if isinstance(value, Decimal):
        try:
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return default

    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return default


def fmt_amount(value):
    return f"{safe_decimal(value):,.2f}"


# ============================================================
# Barcode helpers
# ============================================================

def clean_barcode(value):
    """
    Scanner-safe barcode cleaner.

    Fixes:
    - barcode with spaces
    - barcode with tabs/newlines
    - hidden control characters
    - Excel-style 12345.0
    """
    raw = str(value or "")

    raw = raw.strip()
    raw = raw.replace("\r", "")
    raw = raw.replace("\n", "")
    raw = raw.replace("\t", "")
    raw = raw.replace(" ", "")

    # Remove non-printable scanner/control characters
    raw = "".join(ch for ch in raw if ch.isprintable())

    # Remove common scanner separators
    raw = raw.replace("\ufeff", "")
    raw = raw.replace("\u200b", "")
    raw = raw.replace("\u200c", "")
    raw = raw.replace("\u200d", "")

    # Excel issue: 123456.0
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]

    return raw


def clean_barcode_for_compare(value):
    """
    More aggressive version for comparing DB barcode vs scanned barcode.
    Keeps letters and numbers only.
    """
    raw = clean_barcode(value)
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper()


def find_product_by_barcode(barcode):
    """
    Forgiving barcode lookup.

    Steps:
    1. Clean scanner input.
    2. Exact match.
    3. Case-insensitive match.
    4. Compare normalized stored barcodes.
    """
    barcode = clean_barcode(barcode)

    if not barcode:
        return None

    product = Product.objects.filter(barcode=barcode).first()
    if product:
        return product

    product = Product.objects.filter(barcode__iexact=barcode).first()
    if product:
        return product

    # If barcode is numeric, sometimes leading zero may be removed by scanner/Excel.
    # Try both normal and zero-stripped comparison carefully.
    normalized_scanned = clean_barcode_for_compare(barcode)
    normalized_no_leading_zero = normalized_scanned.lstrip("0")

    candidates = Product.objects.filter(barcode__icontains=barcode[:6])[:50] if len(barcode) >= 6 else Product.objects.all()[:200]

    for p in candidates:
        db_norm = clean_barcode_for_compare(getattr(p, "barcode", ""))

        if db_norm == normalized_scanned:
            return p

        if normalized_no_leading_zero and db_norm.lstrip("0") == normalized_no_leading_zero:
            return p

    return None


# ============================================================
# Cart totals
# ============================================================

def _build_cart_totals(cart_obj):
    """
    Correct total calculation.

    Important:
    - line_total is already the real amount customer pays.
    - tax_value is only extracted tax for display/reporting.
    - Do not add tax_value again.
    """
    total = Decimal("0.00")
    tax_total = Decimal("0.00")
    deposit_total = Decimal("0.00")
    count = 0

    for item in cart_obj:
        qty = int(item.get("quantity", 0) or 0)
        line_total = safe_decimal(item.get("line_total", 0))
        tax_value = safe_decimal(item.get("tax_value", 0))
        deposit_value = safe_decimal(item.get("deposit_value", 0))

        total += line_total
        tax_total += tax_value
        deposit_total += deposit_value
        count += qty

    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax_total = tax_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    deposit_total = deposit_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    subtotal = (total - tax_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if subtotal < Decimal("0.00"):
        subtotal = Decimal("0.00")

    return {
        "subtotal": subtotal,
        "tax_total": tax_total,
        "deposit_total": deposit_total,
        "grand_total": total,
        "count": count,
        "subtotal_formatted": fmt_amount(subtotal),
        "tax_total_formatted": fmt_amount(tax_total),
        "deposit_total_formatted": fmt_amount(deposit_total),
        "grand_total_formatted": fmt_amount(total),
    }


def _get_json_payload(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _cart_response_payload(cart, barcode=None, item=None, removed=False):
    totals = _build_cart_totals(cart)

    data = {
        "success": True,
        "removed": bool(removed),
        "barcode": barcode or "",

        "cart_subtotal": str(totals["subtotal"]),
        "cart_tax": str(totals["tax_total"]),
        "cart_deposit": str(totals["deposit_total"]),
        "cart_total": str(totals["grand_total"]),

        "cart_subtotal_formatted": totals["subtotal_formatted"],
        "cart_tax_formatted": totals["tax_total_formatted"],
        "cart_deposit_formatted": totals["deposit_total_formatted"],
        "cart_total_formatted": totals["grand_total_formatted"],

        "count": totals["count"],
        "cart_is_empty": totals["count"] <= 0,
    }

    if item:
        data.update({
            "name": item.get("name", ""),
            "new_quantity": int(item.get("quantity", 0) or 0),
            "price": str(safe_decimal(item.get("price", 0))),

            "line_total": str(safe_decimal(item.get("line_total", 0))),
            "line_tax": str(safe_decimal(item.get("tax_value", 0))),
            "line_deposit": str(safe_decimal(item.get("deposit_value", 0))),

            "line_total_formatted": fmt_amount(item.get("line_total", 0)),
            "line_tax_formatted": fmt_amount(item.get("tax_value", 0)),
            "line_deposit_formatted": fmt_amount(item.get("deposit_value", 0)),
        })

    return data


# ============================================================
# Views
# ============================================================

@login_required(login_url="/user/login")
def cart_add(request, id, qty):
    """
    Add product to cart using barcode.
    Scanner-safe lookup.
    """
    cart = Cart(request)

    scanned_barcode = clean_barcode(id)
    product = find_product_by_barcode(scanned_barcode)

    if not product:
        request.session["stock_error"] = f"Product not found for barcode: {scanned_barcode}"
        request.session.modified = True
        return redirect("register")

    try:
        quantity = int(qty)
    except Exception:
        quantity = 1

    if quantity <= 0:
        return redirect("register")

    final_price = request.GET.get("price")

    result = cart.add(product=product, quantity=quantity, variable_price=final_price)

    if isinstance(result, dict) and result.get("status") == "error":
        request.session["stock_error"] = result.get("message", "Insufficient stock")
        request.session.modified = True
        return redirect("register")

    return redirect("register")


@login_required(login_url="/user/login")
def item_clear(request, id):
    cart = Cart(request)

    barcode = clean_barcode(id)
    product = find_product_by_barcode(barcode)

    if product:
        cart.remove(product)
    else:
        cart.remove(barcode)

    return redirect("register")


@login_required(login_url="/user/login")
def item_increment(request, id):
    cart = Cart(request)

    barcode = clean_barcode(id)
    product = find_product_by_barcode(barcode)

    if not product:
        request.session["stock_error"] = f"Product not found for barcode: {barcode}"
        request.session.modified = True
        return redirect("register")

    result = cart.add(product=product, quantity=1)

    if isinstance(result, dict) and result.get("status") == "error":
        request.session["stock_error"] = result.get("message", "Insufficient stock")
        request.session.modified = True

    return redirect("register")


@login_required(login_url="/user/login")
def item_decrement(request, id):
    cart = Cart(request)

    barcode = clean_barcode(id)
    product = find_product_by_barcode(barcode)

    if product:
        result = cart.decrement(product, amount=1)
    else:
        result = cart.decrement(barcode, amount=1)

    if isinstance(result, dict) and result.get("status") == "error":
        request.session["stock_error"] = result.get("message", "Failed to decrement")
        request.session.modified = True

    return redirect("register")


@login_required(login_url="/user/login")
def cart_clear(request):
    cart = Cart(request)
    cart.clear()
    return redirect("register")


@login_required(login_url="/user/login")
def cart_detail(request):
    cart = Cart(request)

    items = []
    for item in cart:
        items.append({
            "barcode": item.get("barcode", ""),
            "name": item.get("name", ""),
            "quantity": int(item.get("quantity", 0) or 0),
            "price": safe_decimal(item.get("price", 0)),
            "line_total": safe_decimal(item.get("line_total", 0)),
            "tax_value": safe_decimal(item.get("tax_value", 0)),
            "deposit_value": safe_decimal(item.get("deposit_value", 0)),
            "low_stock": bool(item.get("low_stock", False)),
            "stock_left": int(item.get("stock_left", 0) or 0),
        })

    totals = _build_cart_totals(cart)

    context = {
        "cart_items": items,
        "subtotal": totals["subtotal"],
        "tax_total": totals["tax_total"],
        "deposit_total": totals["deposit_total"],
        "grand_total": totals["grand_total"],
        "count": totals["count"],
    }

    return render(request, "cart/cart_detail.html", context)


@login_required(login_url="/user/login")
@require_POST
@csrf_protect
def cart_update_quantity(request):
    """
    Manual quantity update from register screen.
    """
    payload = _get_json_payload(request)

    barcode = clean_barcode(
        payload.get("barcode") or request.POST.get("barcode") or ""
    )

    quantity = payload.get("quantity") or request.POST.get("quantity") or ""

    if not barcode:
        return JsonResponse({"success": False, "error": "Barcode required"}, status=400)

    try:
        quantity = int(quantity)
    except Exception:
        return JsonResponse({"success": False, "error": "Invalid quantity"}, status=400)

    cart = Cart(request)

    # Use stored barcode if DB product exists.
    product = find_product_by_barcode(barcode)
    if product:
        barcode = str(product.barcode)

    result = cart.set_quantity(barcode, quantity)

    if isinstance(result, dict) and result.get("status") == "error":
        request.session["stock_error"] = result.get("message", "Failed to update quantity")
        request.session.modified = True
        return JsonResponse({
            "success": False,
            "error": result.get("message", "Failed to update quantity"),
        }, status=400)

    item = cart.to_dict().get(str(barcode))

    removed = quantity <= 0 or not item or (isinstance(result, dict) and result.get("removed"))

    return JsonResponse(
        _cart_response_payload(cart, barcode=barcode, item=item, removed=removed)
    )


@login_required(login_url="/user/login")
@require_POST
@csrf_protect
def cart_void_item_ajax(request):
    """
    Remove one cart item using AJAX Void button.
    """
    payload = _get_json_payload(request)

    barcode = clean_barcode(
        payload.get("barcode") or request.POST.get("barcode") or ""
    )

    if not barcode:
        return JsonResponse({"success": False, "error": "Barcode required"}, status=400)

    cart = Cart(request)

    product = find_product_by_barcode(barcode)
    if product:
        barcode = str(product.barcode)

    cart.remove(barcode)

    return JsonResponse(
        _cart_response_payload(cart, barcode=barcode, item=None, removed=True)
    )


@require_GET
@login_required(login_url="/user/login")
def product_search(request):
    """
    AJAX product search:
        /ajax/product_search/?q=milk
    Scanner-safe.
    """
    q_raw = request.GET.get("q", "")
    q = clean_barcode(q_raw)

    data = []

    if not q:
        return JsonResponse({"results": data})

    try:
        qs_by_barcode = Product.objects.filter(barcode__istartswith=q)
        qs_by_name = Product.objects.filter(name__icontains=q)

        # If exact/startswith barcode fails because DB barcode has hidden chars,
        # add normalized candidates later.
    except Exception:
        qs_by_barcode = Product.objects.none()
        qs_by_name = Product.objects.none()

    seen = set()
    limit = 20

    def add_product(product):
        barcode = str(getattr(product, "barcode", "") or "")
        if barcode in seen:
            return

        seen.add(barcode)
        data.append({
            "barcode": barcode,
            "name": getattr(product, "name", ""),
            "sales_price": str(getattr(product, "sales_price", "")),
            "qty": int(getattr(product, "qty", 0) or 0),
        })

    for product in qs_by_barcode[:limit]:
        add_product(product)
        if len(data) >= limit:
            break

    if len(data) < limit:
        for product in qs_by_name[:limit]:
            add_product(product)
            if len(data) >= limit:
                break

    # Normalized fallback for barcode searches
    if len(data) == 0 and len(q) >= 4:
        q_norm = clean_barcode_for_compare(q)

        for product in Product.objects.all()[:500]:
            db_norm = clean_barcode_for_compare(getattr(product, "barcode", ""))
            name_text = str(getattr(product, "name", "") or "").lower()

            if q_norm and (db_norm.startswith(q_norm) or q_norm in db_norm):
                add_product(product)
            elif str(q_raw).strip().lower() in name_text:
                add_product(product)

            if len(data) >= limit:
                break

    return JsonResponse({"results": data})


@require_POST
@login_required(login_url="/user/login")
@csrf_protect
def cart_add_ajax(request):
    """
    AJAX endpoint for barcode scanning.

    Body:
        {
            "barcode": "123",
            "quantity": 1
        }

    If product exists in cart, it increases quantity.
    Scanner-safe.
    """
    payload = _get_json_payload(request)

    scanned_barcode = clean_barcode(payload.get("barcode", ""))
    quantity = payload.get("quantity", 1)

    if not scanned_barcode:
        return JsonResponse({"success": False, "error": "Barcode missing"}, status=400)

    try:
        quantity = int(quantity)
    except Exception:
        quantity = 1

    if quantity <= 0:
        quantity = 1

    product = find_product_by_barcode(scanned_barcode)

    if not product:
        return JsonResponse({
            "success": False,
            "error": f"Product not found for barcode: {scanned_barcode}",
        }, status=404)

    # Use real stored barcode from DB so cart key is always consistent.
    barcode = str(product.barcode)

    cart = Cart(request)

    result = cart.add(product=product, quantity=quantity)

    if isinstance(result, dict) and result.get("status") == "error":
        return JsonResponse({
            "success": False,
            "error": result.get("message", "Insufficient stock"),
        }, status=400)

    item = cart.to_dict().get(barcode)

    if not item:
        return JsonResponse({
            "success": False,
            "error": "Cart update failed",
        }, status=500)

    return JsonResponse(
        _cart_response_payload(cart, barcode=barcode, item=item, removed=False)
    )