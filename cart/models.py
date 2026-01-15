# cart/models.py
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from colorfield.fields import ColorField

# Attempt to import the Product model (local app). If your import path differs, adjust it.
from inventory.models import Product as Product

# Session key used for cart storage
DEFAULT_CART_SESSION_KEY = getattr(settings, "CART_SESSION_ID", "cart")


def _to_decimal(value):
    """Safely convert value to Decimal quantized to 2 decimal places."""
    try:
        if isinstance(value, Decimal):
            d = value
        else:
            d = Decimal(str(value))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")

# cart/cart.py  (or wherever your Cart class lives)
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, getcontext
from typing import Optional, Tuple, Dict, Any

from django.conf import settings

# Try to import your Product model (your model class is named `product` in inventory.models)
try:
    from inventory.models import Product as ProductModel
except Exception:
    ProductModel = None  # best-effort: DB lookups will be skipped if import fails

# ensure Decimal precision is generous
getcontext().prec = 28

DEFAULT_CART_SESSION_KEY = "cart"  # keep the same key you used before (change if needed)


def _to_decimal(value) -> Decimal:
    """Safe conversion to Decimal with fallback to 0.00"""
    try:
        if value is None:
            return Decimal("0.00")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


class Cart:
    """
    Session-backed cart.

    Usage:
        cart = Cart(request)
        result = cart.add(product, quantity=1, variable_price=None)
        if result.get("status") == "error": handle
    """

    def __init__(self, request):
        self.request = request
        self.session = request.session
        self.key = getattr(settings, "DEFAULT_CART_SESSION_KEY", DEFAULT_CART_SESSION_KEY)
        cart = self.session.get(self.key)
        if not isinstance(cart, dict):
            cart = {}
            self.session[self.key] = cart
        # self._cart stores raw session dict (values are stored as strings for session safety)
        self._cart = cart

    def _format_str(self, d: Decimal) -> str:
        """Format Decimal to string with 2 decimal places for session storage."""
        d = _to_decimal(d)
        return f"{d:.2f}"

    def _resolve_tax_pct_and_applicability(self, product) -> Tuple[Decimal, bool]:
        """
        Return (tax_pct_decimal, is_taxable_bool).
        Preference order:
          - product.is_vat_applicable (if present) controls applicability
          - product.tax_percentage if present (use that percent)
          - fallback to product.tax_category.tax_percentage if exists
          - default for Tanzania: tax_pct = 18 and is_taxable = True
        """
        # detect applicability flag
        is_vat_flag = getattr(product, "is_vat_applicable", None)
        # fallback to older flags
        if is_vat_flag is None:
            is_vat_flag = getattr(product, "is_taxable", True) and bool(getattr(product, "tax_category", None))

        # tax percentage resolution
        tax_pct = None
        # direct field
        try:
            tax_pct_val = getattr(product, "tax_percentage", None)
            if tax_pct_val is not None:
                tax_pct = _to_decimal(tax_pct_val)
        except Exception:
            tax_pct = None

        # try tax_category.tax_percentage
        if tax_pct is None:
            try:
                tc = getattr(product, "tax_category", None)
                tax_pct = _to_decimal(getattr(tc, "tax_percentage", None) if tc is not None else None)
            except Exception:
                tax_pct = None

        # final fallback: Tanzania default 18% when taxable
        if tax_pct is None or tax_pct == Decimal("0.00"):
            tax_pct = Decimal("18.00") if is_vat_flag else Decimal("0.00")

        return tax_pct, bool(is_vat_flag)

    # ----- Core operations -----
    def add(self, product: ProductModel, quantity: int = 1, variable_price=None) -> Dict[str, Any]:
        """
        Add a product or increase its quantity.
        Returns dict with status:
            {"status":"ok"} or {"status":"error","message": "..."}
        """
        if product is None:
            return {"status": "error", "message": "No product provided."}

        try:
            qty = int(quantity)
        except Exception:
            qty = 1
        if qty == 0:
            return {"status": "noop"}

        barcode = str(getattr(product, "barcode", "")).strip()
        if not barcode:
            return {"status": "error", "message": "Product barcode missing."}

        # per-unit (selling) price (Decimal)
        if variable_price is not None:
            unit_price = _to_decimal(variable_price)
            var_flag = True
        else:
            unit_price = _to_decimal(getattr(product, "sales_price", getattr(product, "selling_price", "0")))
            var_flag = False

        # deposit (if you use deposit categories; optional)
        deposit_val = Decimal("0.00")
        if getattr(product, "deposit_category", None):
            try:
                deposit_val = _to_decimal(getattr(product.deposit_category, "deposit_value", 0))
            except Exception:
                deposit_val = Decimal("0.00")

        # STOCK CHECK (prevent oversell)
        available_stock = int(getattr(product, "qty", 0) or 0)
        current_in_cart = int(self._cart.get(barcode, {}).get("quantity", 0) or 0)
        requested_total = current_in_cart + qty
        if requested_total > available_stock:
            return {
                "status": "error",
                "message": f"Insufficient stock. Available: {available_stock}, Requested in cart: {requested_total}"
            }

        # Determine tax percentage & applicability (per product)
        tax_pct, is_vat_applicable = self._resolve_tax_pct_and_applicability(product)

        # VAT should be calculated from the selling price (VAT-inclusive extraction)
        # We compute per-line totals and per-line VAT below.

        # Merge with existing entry if present
        if barcode in self._cart:
            existing = self._cart[barcode].copy()
            existing_qty = int(existing.get("quantity", 0) or 0)
            new_qty = existing_qty + qty

            if new_qty <= 0:
                # remove entirely
                del self._cart[barcode]
                self.save()
                return {"status": "ok"}

            # Determine unit price to use: if caller provided variable_price use that, otherwise keep stored one
            stored_price = _to_decimal(existing.get("price", unit_price))
            unit_price_used = unit_price if var_flag else stored_price

            # deposit total for new_qty
            deposit_total = (deposit_val * Decimal(new_qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # recompute line_total (what customer pays for this line)
            line_total = (unit_price_used * Decimal(new_qty) + deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # compute VAT on this line by extracting from line_total (if taxable)
            if is_vat_applicable and tax_pct > 0:
                denom = (Decimal("100.00") + tax_pct)
                raw_line_vat = (unit_price_used * Decimal(new_qty) * tax_pct) / denom
                total_vat = raw_line_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                total_vat = Decimal("0.00")

            # cost price for profit calc (if available)
            cost_price_val = _to_decimal(getattr(product, "cost_price", getattr(product, "purchase_price", 0)))

            # profit per line = SP*qty - (CP*qty + total_vat)
            profit_per_line = (unit_price_used * Decimal(new_qty) - (cost_price_val * Decimal(new_qty) + total_vat)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            existing["quantity"] = int(new_qty)
            existing["price"] = self._format_str(unit_price_used)
            existing["tax_value"] = self._format_str(total_vat)
            existing["deposit_value"] = self._format_str(deposit_total)
            existing["profit_value"] = self._format_str(profit_per_line)
            existing["line_total"] = self._format_str(line_total)
            existing["variable_price"] = bool(var_flag) or bool(existing.get("variable_price", False))

            # compute remaining stock after this addition
            remaining = available_stock - new_qty
            existing["low_stock"] = bool(remaining <= getattr(product, "low_stock_threshold", 5))
            existing["stock_left"] = int(max(0, remaining))

            self._cart[barcode] = existing
        else:
            # New entry
            deposit_total = (deposit_val * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            # line_total is what the customer pays for this line
            line_total = (unit_price * Decimal(qty) + deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # compute VAT by extracting from line_total (VAT-inclusive)
            if is_vat_applicable and tax_pct > 0:
                denom = (Decimal("100.00") + tax_pct)
                raw_line_vat = (unit_price * Decimal(qty) * tax_pct) / denom
                total_vat = raw_line_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                total_vat = Decimal("0.00")

            # get cost price for profit calc
            cost_price_val = _to_decimal(getattr(product, "cost_price", getattr(product, "purchase_price", 0)))
            profit_per_line = (unit_price * Decimal(qty) - (cost_price_val * Decimal(qty) + total_vat)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            remaining = available_stock - qty

            self._cart[barcode] = {
                "barcode": barcode,
                "name": str(getattr(product, "name", "") or getattr(product, "display_name", "")),
                "price": self._format_str(unit_price),         # per-unit price as string
                "quantity": int(qty),
                "tax_value": self._format_str(total_vat),      # total tax for the line (string)
                "deposit_value": self._format_str(deposit_total), # total deposit for the line (string)
                "profit_value": self._format_str(profit_per_line), # profit for the line
                "line_total": self._format_str(line_total),
                "variable_price": bool(var_flag),
                # stock metadata
                "low_stock": bool(remaining <= getattr(product, "low_stock_threshold", 5)),
                "stock_left": int(max(0, remaining)),
            }

        self.save()
        return {"status": "ok"}

    def save(self):
        """Persist cart to session and mark modified."""
        self.session[self.key] = self._cart
        self.session.modified = True

    def remove(self, product_or_barcode):
        """Remove item entirely by product instance or barcode string."""
        barcode = str(getattr(product_or_barcode, "barcode", product_or_barcode)).strip()
        if barcode in self._cart:
            del self._cart[barcode]
            self.save()

    def decrement(self, product_or_barcode, amount=1):
        """Decrease quantity by `amount` (default 1). Removes item if quantity <= 0."""
        barcode = str(getattr(product_or_barcode, "barcode", product_or_barcode)).strip()
        if barcode not in self._cart:
            return {"status": "error", "message": "Item not in cart."}
        try:
            amt = int(amount)
        except Exception:
            amt = 1
        if amt <= 0:
            return {"status": "noop"}

        existing = self._cart[barcode].copy()
        existing_qty = int(existing.get("quantity", 0) or 0)
        new_qty = existing_qty - amt
        if new_qty <= 0:
            del self._cart[barcode]
            self.save()
            return {"status": "ok"}

        # recalc totals based on stored unit price
        unit_price = _to_decimal(existing.get("price", 0))

        # attempt to compute tax rate per item from previous tax_value if present
        try:
            prev_tax_total = _to_decimal(existing.get("tax_value", "0"))
            prev_qty = Decimal(existing_qty)
            tax_per_item = (prev_tax_total / prev_qty) if prev_qty > 0 else Decimal("0.00")
            new_tax_total = (tax_per_item * Decimal(new_qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            # fallback: recompute based on unit price and product info if product exists
            new_tax_total = Decimal("0.00")
            try:
                prod = ProductModel.objects.filter(barcode=barcode).first() if ProductModel else None
                if prod:
                    tax_pct, is_vat = self._resolve_tax_pct_and_applicability(prod)
                    if is_vat and tax_pct > 0:
                        denom = (Decimal("100.00") + tax_pct)
                        raw_vat = (unit_price * Decimal(new_qty) * tax_pct) / denom
                        new_tax_total = raw_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except Exception:
                new_tax_total = Decimal("0.00")

        # deposit per item proportional (if present)
        try:
            prev_deposit_total = _to_decimal(existing.get("deposit_value", "0"))
            prev_qty = Decimal(existing_qty)
            deposit_per_item = (prev_deposit_total / prev_qty) if prev_qty > 0 else Decimal("0.00")
            new_deposit_total = (deposit_per_item * Decimal(new_qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            new_deposit_total = Decimal("0.00")

        # Try to estimate cost_price to update profit proportionally if we can find product model
        try:
            prod = None
            if ProductModel is not None:
                prod = ProductModel.objects.filter(barcode=barcode).first()
            if prod is not None:
                cost_price_val = _to_decimal(getattr(prod, "cost_price", getattr(prod, "purchase_price", 0)))
            else:
                # fallback: estimate from previous profit if present
                prev_profit = _to_decimal(existing.get("profit_value", "0"))
                prev_qty_dec = Decimal(existing_qty) if existing_qty > 0 else Decimal("1")
                profit_per_item_prev = (prev_profit / prev_qty_dec) if prev_qty_dec > 0 else Decimal("0.00")
                # estimate cost per item = unit_price - profit_per_item_prev - tax_per_item (approximate)
                cost_price_val = (unit_price - profit_per_item_prev - tax_per_item).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            cost_price_val = Decimal("0.00")

        new_line_total = (unit_price * Decimal(new_qty) + new_tax_total + new_deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # recalc profit: (SP*qty) - (CP*qty + total_vat)
        new_profit = (unit_price * Decimal(new_qty) - (cost_price_val * Decimal(new_qty) + new_tax_total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        existing["quantity"] = int(new_qty)
        existing["tax_value"] = f"{new_tax_total:.2f}"
        existing["deposit_value"] = f"{new_deposit_total:.2f}"
        existing["line_total"] = f"{new_line_total:.2f}"
        existing["profit_value"] = f"{new_profit:.2f}"

        # update low_stock and stock_left if product exists
        if ProductModel is not None:
            try:
                prod = ProductModel.objects.filter(barcode=barcode).first()
                if prod:
                    available_stock = int(getattr(prod, "qty", 0) or 0)
                    remaining = available_stock - new_qty
                    existing["low_stock"] = bool(remaining <= getattr(prod, "low_stock_threshold", 5))
                    existing["stock_left"] = int(max(0, remaining))
            except Exception:
                pass

        self._cart[barcode] = existing
        self.save()
        return {"status": "ok"}

    def set_quantity(self, product_or_barcode, quantity):
        """Set exact quantity for an item. If quantity <=0 remove it. Returns status dict."""
        barcode = str(getattr(product_or_barcode, "barcode", product_or_barcode)).strip()
        if barcode not in self._cart:
            return {"status": "error", "message": "Item not in cart."}
        try:
            q = int(quantity)
        except Exception:
            q = 0
        if q <= 0:
            del self._cart[barcode]
            self.save()
            return {"status": "ok"}

        # If product model is available, enforce stock check when increasing quantity
        if ProductModel is not None:
            try:
                prod = ProductModel.objects.filter(barcode=barcode).first()
            except Exception:
                prod = None
            if prod:
                available_stock = int(getattr(prod, "qty", 0) or 0)
                if q > available_stock:
                    return {"status": "error", "message": f"Insufficient stock. Available: {available_stock}"}
            else:
                # cannot validate stock without product; best-effort proceed
                pass

        existing = self._cart[barcode].copy()
        unit_price = _to_decimal(existing.get("price", 0))

        prev_qty = int(existing.get("quantity", 0) or 0)

        # estimate tax/deposit proportionally as in decrement/add
        try:
            prev_tax_total = _to_decimal(existing.get("tax_value", "0"))
            tax_per_item = (prev_tax_total / Decimal(prev_qty)) if prev_qty > 0 else Decimal("0.00")
            new_tax_total = (tax_per_item * Decimal(q)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            new_tax_total = Decimal("0.00")
        try:
            prev_dep_total = _to_decimal(existing.get("deposit_value", "0"))
            dep_per_item = (prev_dep_total / Decimal(prev_qty)) if prev_qty > 0 else Decimal("0.00")
            new_dep_total = (dep_per_item * Decimal(q)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            new_dep_total = Decimal("0.00")

        # attempt to fetch product for cost_price to compute profit exactly
        try:
            prod = None
            if ProductModel is not None:
                prod = ProductModel.objects.filter(barcode=barcode).first()
            if prod:
                cost_price_val = _to_decimal(getattr(prod, "cost_price", getattr(prod, "purchase_price", 0)))
                # check VAT applicability on product
                tax_pct, is_vat = self._resolve_tax_pct_and_applicability(prod)
                if is_vat and tax_pct > 0:
                    denom = (Decimal("100.00") + tax_pct)
                    raw_vat = (unit_price * Decimal(q) * tax_pct) / denom
                    new_tax_total = raw_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                else:
                    new_tax_total = Decimal("0.00")
            else:
                # fallback: use proportional tax we computed earlier
                pass
        except Exception:
            cost_price_val = Decimal("0.00")

        new_line_total = (unit_price * Decimal(q) + new_tax_total + new_dep_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # recalc profit: (SP*qty) - (CP*qty + total_vat)
        try:
            new_profit = (unit_price * Decimal(q) - (cost_price_val * Decimal(q) + new_tax_total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            new_profit = Decimal("0.00")

        existing["quantity"] = int(q)
        existing["tax_value"] = f"{new_tax_total:.2f}"
        existing["deposit_value"] = f"{new_dep_total:.2f}"
        existing["line_total"] = f"{new_line_total:.2f}"
        existing["profit_value"] = f"{new_profit:.2f}"

        # update low_stock and stock_left if product exists
        if ProductModel is not None:
            try:
                prod = ProductModel.objects.filter(barcode=barcode).first()
                if prod:
                    available_stock = int(getattr(prod, "qty", 0) or 0)
                    remaining = available_stock - q
                    existing["low_stock"] = bool(remaining <= getattr(prod, "low_stock_threshold", 5))
                    existing["stock_left"] = int(max(0, remaining))
            except Exception:
                pass

        self._cart[barcode] = existing
        self.save()
        return {"status": "ok"}

    def clear(self):
        """Empty cart entirely."""
        self.session[self.key] = {}
        self._cart = {}
        self.session.modified = True

    def isNotEmpty(self):
        """Return True if cart has any items."""
        return bool(self._cart and len(self._cart) > 0)

    def cart_total(self):
        """Return total sum of line_total for all items as Decimal."""
        total = Decimal("0.00")
        for v in self._cart.values():
            total += _to_decimal(v.get("line_total", "0"))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_total_vat(self) -> Decimal:
        """Return total VAT for the cart (sum of tax_value)."""
        total_vat = Decimal("0.00")
        for v in self._cart.values():
            total_vat += _to_decimal(v.get("tax_value", "0"))
        return total_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_total_profit(self) -> Decimal:
        """Return total profit for the cart (sum of profit_value)."""
        total_profit = Decimal("0.00")
        for v in self._cart.values():
            total_profit += _to_decimal(v.get("profit_value", "0"))
        return total_profit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def returns(self):
        """
        Convert current cart into return-typed items (negate quantities and numeric fields).
        Useful for processing refunds/returns. This mutates the session cart.
        """
        for k, v in list(self._cart.items()):
            qty = int(v.get("quantity", 0) or 0)
            v["quantity"] = -abs(qty)
            v["tax_value"] = f"{(-_to_decimal(v.get('tax_value', '0'))):.2f}"
            v["line_total"] = f"{(-_to_decimal(v.get('line_total', '0'))):.2f}"
            v["profit_value"] = f"{(-_to_decimal(v.get('profit_value', '0'))):.2f}"
            # keep per-unit price positive (common practice)
            self._cart[k] = v
        self.save()

    # ----- Iteration & helpers for templates -----
    def __len__(self):
        """Number of items (sum of quantities)."""
        return sum(int(v.get("quantity", 0) or 0) for v in self._cart.values())

    def __iter__(self):
        """
        Iterate over typed item dicts for convenience:
        yields dicts with Decimal price/line_total and ints for quantity.
        """
        for barcode, raw in list(self._cart.items()):
            yield {
                "barcode": barcode,
                "name": raw.get("name", ""),
                "quantity": int(raw.get("quantity", 0) or 0),
                "price": _to_decimal(raw.get("price", "0")),
                "tax_value": _to_decimal(raw.get("tax_value", "0")),
                "deposit_value": _to_decimal(raw.get("deposit_value", "0")),
                "profit_value": _to_decimal(raw.get("profit_value", "0")),
                "line_total": _to_decimal(raw.get("line_total", "0")),
                "variable_price": bool(raw.get("variable_price", False)),
                "low_stock": bool(raw.get("low_stock", False)),
                "stock_left": int(raw.get("stock_left", 0) or 0),
            }

    def get_total_price(self):
        """Return Decimal total of cart (line totals)."""
        return self.cart_total()

    def to_dict(self):
        """Return a deep-copied dict of the raw session cart (strings)"""
        return {k: dict(v) for k, v in self._cart.items()}

    # Provide .items property so templates using `cart.items` work (returns typed values)
    @property
    def items(self):
        """
        Return a list of (barcode, typed_dict) pairs suitable for Django templates:
            {% for key, value in cart.items %}
        """
        out = []
        for barcode, raw in self._cart.items():
            typed = {
                "barcode": barcode,
                "name": raw.get("name", ""),
                "quantity": int(raw.get("quantity", 0) or 0),
                "price": _to_decimal(raw.get("price", "0")),
                "tax_value": _to_decimal(raw.get("tax_value", "0")),
                "deposit_value": _to_decimal(raw.get("deposit_value", "0")),
                "profit_value": _to_decimal(raw.get("profit_value", "0")),
                "line_total": _to_decimal(raw.get("line_total", "0")),
                "variable_price": bool(raw.get("variable_price", False)),
                "low_stock": bool(raw.get("low_stock", False)),
                "stock_left": int(raw.get("stock_left", 0) or 0),
            }
            out.append((barcode, typed))
        return out

# ------------------------------
# DISPLAYED ITEMS MODEL
# ------------------------------
class displayed_items(models.Model):
    """
    Simple model for buttons/display items on the POS screen.
    Must reference an existing product barcode (prevents orphan button entries).
    """
    barcode = models.CharField(unique=True, max_length=64, blank=False, null=False)
    display_name = models.CharField(max_length=125, blank=False, null=False)
    display_info = models.CharField(max_length=125, blank=True, null=False, default="")
    display_color = ColorField(default="#575757")
    variable_price = models.BooleanField(default=False)  # allow variable-price buttons

    def __str__(self):
        return f"{self.display_name} ({self.barcode})"

    def save(self, *args, **kwargs):
        """Prevent saving a displayed item for a non-existent product barcode."""
        if Product.objects.filter(barcode=self.barcode).exists():
            return super().save(*args, **kwargs)
        raise ValidationError(f"Cannot save displayed item: no product with barcode '{self.barcode}' exists.")

    class Meta:
        verbose_name_plural = "Displayed Items"
