# cart/views.py
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django.shortcuts import redirect, render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse

from inventory.models import Product as Product
from .models import Cart  # adjust import path if your Cart lives elsewhere


# ---------- Helpers ----------
def safe_decimal(value, default=Decimal("0.00")):
    """
    Convert value to Decimal safely and quantize to 2 decimals.
    Accepts Decimal, int, float, str. Returns default on failure.
    """
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


def get_unit_tax(product_obj):
    """
    Calculate per-unit tax for a Product object.
    Returns Decimal(0.00) if product is not taxable or on error.
    """
    try:
        if getattr(product_obj, "is_taxable", True) and getattr(product_obj, "tax_category", None):
            pct = safe_decimal(getattr(product_obj.tax_category, "tax_percentage", 0)) / Decimal("100")
            unit_tax = (safe_decimal(getattr(product_obj, "sales_price", 0)) * pct).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            return unit_tax
        return Decimal("0.00")
    except Exception:
        return Decimal("0.00")


def _build_cart_totals(cart_obj):
    """
    Returns (subtotal, tax_total, deposit_total, grand_total, count)
    All are Decimal (except count).
    """
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    deposit_total = Decimal("0.00")
    count = 0

    for entry in cart_obj:
        qty = int(entry.get("quantity", entry.get("qty", 0)) or 0)
        line_total = safe_decimal(entry.get("line_total", entry.get("total_price", 0)))
        tax_val = safe_decimal(entry.get("tax_value", 0))
        deposit_val = safe_decimal(entry.get("deposit_value", 0))

        subtotal += line_total
        # tax_value might already be a total for the line or per-item depending on your cart;
        # attempt to treat provided tax_value as total for the line if it looks right.
        # If your cart stores per-item tax, update accordingly.
        tax_total += tax_val
        deposit_total += deposit_val

        count += qty

    grand_total = (subtotal + tax_total + deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (
        subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        tax_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        deposit_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        grand_total,
        count,
    )


# ---------- Views ----------
@login_required(login_url="/user/login")
def cart_add(request, id, qty):
    """
    Add a product to the session cart.
    - id: barcode
    - qty: integer quantity
    Optional `price` GET param can override unit price (variable_price).
    """
    cart = Cart(request)
    try:
        product = Product.objects.filter(barcode=id).first()
    except Exception:
        product = None

    if not product:
        # set session message for template to show & play alert
        request.session["stock_error"] = "Product not found"
        return redirect("register")

    # parse quantity defensively
    try:
        q = int(qty)
    except Exception:
        q = 1
    # allow only positive adds (if you want to support negative for returns, use returns endpoint)
    if q <= 0:
        return redirect("register")

    final_price = request.GET.get("price")  # optional price override
    result = cart.add(product=product, quantity=q, variable_price=final_price)

    if result.get("status") == "error":
        # store error in session so template can show + trigger alert sound
        request.session["stock_error"] = result.get("message", "Insufficient stock")
        return redirect("register")

    return redirect("register")


@login_required(login_url="/user/login")
def item_clear(request, id):
    """
    Remove an item from the cart completely.
    `id` is the product barcode.
    """
    cart = Cart(request)
    try:
        product = Product.objects.get(barcode=id)
    except Product.DoesNotExist:
        # nothing to remove
        return redirect("register")

    cart.remove(product)
    return redirect("register")


@login_required(login_url="/user/login")
def item_increment(request, id):
    """
    Increment product quantity by 1. Returns redirect to register page.
    If stock insufficient, sets session stock_error to trigger UI alert.
    """
    cart = Cart(request)
    try:
        product = Product.objects.get(barcode=id)
    except Product.DoesNotExist:
        request.session["stock_error"] = "Product not found"
        return redirect("register")

    result = cart.add(product=product, quantity=1)

    if result.get("status") == "error":
        request.session["stock_error"] = result.get("message", "Insufficient stock")
        return redirect("register")

    return redirect("register")


@login_required(login_url="/user/login")
def item_decrement(request, id):
    """
    Decrement product quantity by 1. If count reaches 0, remove from cart.
    """
    cart = Cart(request)
    try:
        product = Product.objects.get(barcode=id)
    except Product.DoesNotExist:
        request.session["stock_error"] = "Product not found"
        return redirect("register")

    # our Cart.decrement returns dicts (or earlier version may not). handle both.
    result = cart.decrement(product)
    if isinstance(result, dict) and result.get("status") == "error":
        request.session["stock_error"] = result.get("message", "Failed to decrement")
    return redirect("register")


@login_required(login_url="/user/login")
def cart_clear(request):
    """
    Clear the entire session cart.
    """
    cart = Cart(request)
    cart.clear()
    return redirect("register")


@login_required(login_url="/user/login")
def cart_detail(request):
    """
    Render the cart detail page. Compute totals using cart items.
    """
    cart = Cart(request)

    items = []
    for entry in cart:
        # ensure keys exist and are typed
        qty = int(entry.get("quantity", entry.get("qty", 0)) or 0)
        price = safe_decimal(entry.get("price", entry.get("sales_price", 0)))
        line_total = safe_decimal(entry.get("line_total", price * qty))
        tax_val = safe_decimal(entry.get("tax_value", 0))
        deposit_val = safe_decimal(entry.get("deposit_value", 0))

        items.append(
            {
                "barcode": entry.get("barcode", ""),
                "name": entry.get("name", ""),
                "quantity": qty,
                "price": price,
                "line_total": line_total,
                "tax_value": tax_val,
                "deposit_value": deposit_val,
                "low_stock": bool(entry.get("low_stock", False)),
                "stock_left": int(entry.get("stock_left", 0) or 0),
            }
        )

    subtotal, tax_total, deposit_total, grand_total, count = _build_cart_totals(cart)

    context = {
        "cart_items": items,
        "subtotal": subtotal,
        "tax_total": tax_total,
        "deposit_total": deposit_total,
        "grand_total": grand_total,
        "count": count,
    }
    return render(request, "cart/cart_detail.html", context)


@login_required(login_url="/user/login")
def cart_update_quantity(request):
    """
    AJAX / form endpoint to set a product quantity in cart.
    Expects POST with 'barcode' and 'quantity' fields.
    Returns JSON with new totals or 400 on bad input.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")

    barcode = request.POST.get("barcode")
    quantity = request.POST.get("quantity")

    if barcode is None or quantity is None:
        return HttpResponseBadRequest("barcode and quantity required")

    try:
        q = int(quantity)
        if q < 0:
            return HttpResponseBadRequest("quantity must be >= 0")
    except Exception:
        return HttpResponseBadRequest("invalid quantity")

    cart = Cart(request)

    try:
        product = Product.objects.get(barcode=barcode)
    except Product.DoesNotExist:
        return HttpResponseBadRequest("product not found")

    # If q == 0 remove the product
    if q == 0:
        cart.remove(product)
    else:
        # set exact quantity using Cart.set_quantity if it exists (it returns status dict)
        if hasattr(cart, "set_quantity"):
            result = cart.set_quantity(product, q)
            if isinstance(result, dict) and result.get("status") == "error":
                # return JSON error so UI can show and play sound
                request.session["stock_error"] = result.get("message", "Insufficient stock")
                return JsonResponse({"error": result.get("message")}, status=400)
        else:
            # fallback: remove then add with desired quantity (best-effort)
            cart.remove(product)
            result = cart.add(product=product, quantity=q)
            if isinstance(result, dict) and result.get("status") == "error":
                request.session["stock_error"] = result.get("message", "Insufficient stock")
                return JsonResponse({"error": result.get("message")}, status=400)

    # Build and return new totals
    subtotal, tax_total, deposit_total, grand_total, count = _build_cart_totals(cart)

    response = {
        "subtotal": str(subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "tax_total": str(tax_total),
        "deposit_total": str(deposit_total),
        "grand_total": str(grand_total),
        "count": count,
    }
    return JsonResponse(response)


from django.views.decorators.http import require_GET
from django.utils.html import escape

@require_GET
@login_required(login_url="/user/login")
def product_search(request):
    """
    AJAX endpoint: ?q=search_text
    Returns JSON list of matching products limited to 20 items.
    Each item: { barcode, name, sales_price, qty }
    """
    q = request.GET.get("q", "").strip()
    data = []
    if not q:
        return JsonResponse({"results": data})

    # Basic heuristic search:
    # - If the query looks numeric or short, prefer barcode startswith,
    # - Otherwise search name icontains and barcode startswith too.
    qs_by_barcode = Product.objects.none()
    qs_by_name = Product.objects.none()

    try:
        # search by barcode (starts with)
        qs_by_barcode = Product.objects.filter(barcode__istartswith=q)
        # search by name anywhere (case-insensitive)
        qs_by_name = Product.objects.filter(name__icontains=q)
    except Exception:
        pass

    # Combine results preserving order: barcode matches first, then name matches (avoid dupes)
    seen = set()
    limit = 20
    for p in list(qs_by_barcode[:limit]):
        seen.add(p.barcode)
        data.append({
            "barcode": p.barcode,
            "name": p.name,
            "sales_price": str(getattr(p, "sales_price", "")),
            "qty": int(getattr(p, "qty", 0) or 0),
        })
        if len(data) >= limit:
            break

    if len(data) < limit:
        for p in list(qs_by_name[:limit]):
            if p.barcode in seen:
                continue
            seen.add(p.barcode)
            data.append({
                "barcode": p.barcode,
                "name": p.name,
                "sales_price": str(getattr(p, "sales_price", "")),
                "qty": int(getattr(p, "qty", 0) or 0),
            })
            if len(data) >= limit:
                break

    return JsonResponse({"results": data})
