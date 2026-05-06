# cart/models.py
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, getcontext
from typing import Tuple, Dict, Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from colorfield.fields import ColorField

from inventory.models import Product

getcontext().prec = 28

DEFAULT_CART_SESSION_KEY = "cart"


# ============================================================
# Helpers
# ============================================================

def _to_decimal(value) -> Decimal:
    """
    Safely convert value to Decimal.
    Keep calculations accurate for POS money values.
    """
    try:
        if value is None or value == "":
            return Decimal("0.00")
        if isinstance(value, Decimal):
            d = value
        else:
            d = Decimal(str(value))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def clean_barcode(value) -> str:
    """
    Scanner-safe barcode cleaner.

    Fixes:
    - spaces
    - tabs/newlines
    - hidden scanner characters
    - Excel-style 12345.0
    """
    raw = str(value or "")

    raw = raw.strip()
    raw = raw.replace("\r", "")
    raw = raw.replace("\n", "")
    raw = raw.replace("\t", "")
    raw = raw.replace(" ", "")

    raw = raw.replace("\ufeff", "")
    raw = raw.replace("\u200b", "")
    raw = raw.replace("\u200c", "")
    raw = raw.replace("\u200d", "")

    raw = "".join(ch for ch in raw if ch.isprintable())

    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]

    return raw


def normalize_barcode_compare(value) -> str:
    raw = clean_barcode(value)
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper()


# ============================================================
# Session Cart
# ============================================================

class Cart:
    """
    Session-backed cart.

    Important rule:
    - price is VAT-inclusive selling price.
    - tax_value is extracted VAT for display/reporting only.
    - line_total is the actual amount customer pays.
    - Do NOT add tax_value again to line_total.
    """

    def __init__(self, request):
        self.request = request
        self.session = request.session

        # IMPORTANT: all views must use same key.
        self.key = getattr(settings, "CART_SESSION_ID", DEFAULT_CART_SESSION_KEY)

        cart = self.session.get(self.key)

        if not isinstance(cart, dict):
            cart = {}

        self._cart = self._clean_existing_cart(cart)
        self.session[self.key] = self._cart
        self.session.modified = True

    def _clean_existing_cart(self, cart: Dict[str, Any]) -> Dict[str, Any]:
        """
        Cleans old session cart data.

        This fixes cases where suspended/recalled carts have barcode keys with
        hidden characters, spaces, or stored barcode not matching item['barcode'].
        """
        clean_cart = {}

        if not isinstance(cart, dict):
            return clean_cart

        for raw_key, raw_item in cart.items():
            if not isinstance(raw_item, dict):
                continue

            item = dict(raw_item)

            key_from_item = item.get("barcode") or raw_key
            clean_key = clean_barcode(key_from_item)

            if not clean_key:
                continue

            # Try to align with actual stored barcode in DB.
            product = self._find_product_for_barcode(clean_key)
            if product is not None:
                clean_key = clean_barcode(getattr(product, "barcode", clean_key))

            item["barcode"] = clean_key

            try:
                item["quantity"] = int(item.get("quantity", 1) or 1)
            except Exception:
                item["quantity"] = 1

            if item["quantity"] == 0:
                continue

            item["price"] = self._format_str(item.get("price", 0))
            item["tax_percentage"] = self._format_str(item.get("tax_percentage", 0))
            item["tax_value"] = self._format_str(item.get("tax_value", 0))
            item["deposit_value"] = self._format_str(item.get("deposit_value", 0))
            item["profit_value"] = self._format_str(item.get("profit_value", 0))
            item["line_total"] = self._format_str(item.get("line_total", 0))
            item["variable_price"] = bool(item.get("variable_price", False))
            item["low_stock"] = bool(item.get("low_stock", False))

            try:
                item["stock_left"] = int(item.get("stock_left", 0) or 0)
            except Exception:
                item["stock_left"] = 0

            # If duplicate cleaned keys exist, merge quantities and recalc.
            if clean_key in clean_cart:
                existing_qty = int(clean_cart[clean_key].get("quantity", 0) or 0)
                new_qty = existing_qty + int(item.get("quantity", 0) or 0)
                clean_cart[clean_key]["quantity"] = new_qty
                temp_cart_before = self.__dict__.get("_cart")
                self._cart = clean_cart
                self._recalculate_item(clean_key, product=product, quantity=new_qty)
                if temp_cart_before is not None:
                    self._cart = temp_cart_before
            else:
                clean_cart[clean_key] = item

        return clean_cart

    def _format_str(self, value) -> str:
        return f"{_to_decimal(value):.2f}"

    def _find_product_for_barcode(self, barcode):
        barcode = clean_barcode(barcode)

        if not barcode:
            return None

        try:
            product = Product.objects.filter(barcode=barcode).first()
            if product:
                return product

            product = Product.objects.filter(barcode__iexact=barcode).first()
            if product:
                return product

            norm = normalize_barcode_compare(barcode)
            norm_no_zero = norm.lstrip("0")

            candidates = (
                Product.objects.filter(barcode__icontains=barcode[:6])[:50]
                if len(barcode) >= 6
                else Product.objects.all()[:200]
            )

            for product in candidates:
                db_norm = normalize_barcode_compare(getattr(product, "barcode", ""))

                if db_norm == norm:
                    return product

                if norm_no_zero and db_norm.lstrip("0") == norm_no_zero:
                    return product

        except Exception:
            return None

        return None

    def _resolve_tax_pct_and_applicability(self, product) -> Tuple[Decimal, bool]:
        """
        Returns:
            tax_pct, is_taxable

        Supports different product model styles:
        - product.is_vat_applicable
        - product.is_taxable
        - product.tax_percentage
        - product.tax_category.tax_percentage
        """
        is_taxable = True

        try:
            is_vat_applicable = getattr(product, "is_vat_applicable", None)
            if is_vat_applicable is not None:
                is_taxable = bool(is_vat_applicable)
            else:
                is_taxable = bool(getattr(product, "is_taxable", True))
        except Exception:
            is_taxable = True

        tax_pct = Decimal("0.00")

        try:
            direct_pct = getattr(product, "tax_percentage", None)
            if direct_pct is not None:
                tax_pct = _to_decimal(direct_pct)
        except Exception:
            tax_pct = Decimal("0.00")

        if tax_pct == Decimal("0.00"):
            try:
                tax_category = getattr(product, "tax_category", None)
                if tax_category is not None:
                    tax_pct = _to_decimal(getattr(tax_category, "tax_percentage", 0))
            except Exception:
                tax_pct = Decimal("0.00")

        if not is_taxable:
            tax_pct = Decimal("0.00")

        return tax_pct, is_taxable

    def _get_unit_price(self, product, variable_price=None) -> Tuple[Decimal, bool]:
        if variable_price is not None and str(variable_price).strip() != "":
            return _to_decimal(variable_price), True

        price = _to_decimal(
            getattr(
                product,
                "sales_price",
                getattr(product, "selling_price", getattr(product, "price", 0)),
            )
        )
        return price, False

    def _get_unit_deposit(self, product) -> Decimal:
        deposit = Decimal("0.00")

        try:
            deposit_category = getattr(product, "deposit_category", None)
            if deposit_category is not None:
                deposit = _to_decimal(getattr(deposit_category, "deposit_value", 0))
        except Exception:
            deposit = Decimal("0.00")

        return deposit

    def _get_cost_price(self, product) -> Decimal:
        try:
            return _to_decimal(
                getattr(
                    product,
                    "cost_price",
                    getattr(product, "purchase_price", getattr(product, "buying_price", 0)),
                )
            )
        except Exception:
            return Decimal("0.00")

    def _extract_vat_from_gross(self, gross_amount: Decimal, tax_pct: Decimal) -> Decimal:
        """
        VAT-inclusive formula:
        VAT = Gross * tax_pct / (100 + tax_pct)
        """
        gross_amount = _to_decimal(gross_amount)
        tax_pct = _to_decimal(tax_pct)

        if gross_amount <= 0 or tax_pct <= 0:
            return Decimal("0.00")

        vat = gross_amount * tax_pct / (Decimal("100.00") + tax_pct)
        return vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _recalculate_item(self, barcode: str, product=None, quantity=None, variable_price=None) -> Dict[str, Any]:
        """
        Recalculate one cart item cleanly.

        line_total = unit_price * qty + deposit_total
        tax_value is NOT added again because price is VAT-inclusive.
        """
        barcode = clean_barcode(barcode)

        if barcode not in self._cart:
            return {"status": "error", "message": "Item not in cart."}

        existing = dict(self._cart[barcode])

        if product is None:
            product = self._find_product_for_barcode(barcode)

        if product is not None:
            real_barcode = clean_barcode(getattr(product, "barcode", barcode))
            if real_barcode and real_barcode != barcode:
                self._cart[real_barcode] = self._cart.pop(barcode)
                barcode = real_barcode
                existing = dict(self._cart[barcode])

        try:
            qty = int(quantity if quantity is not None else existing.get("quantity", 1))
        except Exception:
            qty = 1

        if qty <= 0:
            del self._cart[barcode]
            self.save()
            return {"status": "ok", "removed": True}

        # Stock check
        if product is not None:
            available_stock = int(getattr(product, "qty", 0) or 0)
            if qty > available_stock:
                return {
                    "status": "error",
                    "message": f"Insufficient stock. Available: {available_stock}",
                }
        else:
            available_stock = None

        # Price
        if variable_price is not None and str(variable_price).strip() != "":
            unit_price = _to_decimal(variable_price)
            variable_flag = True
        else:
            unit_price = _to_decimal(existing.get("price", 0))
            variable_flag = bool(existing.get("variable_price", False))

            if unit_price <= 0 and product is not None:
                unit_price, variable_flag = self._get_unit_price(product)

        # Deposit per unit
        if product is not None:
            unit_deposit = self._get_unit_deposit(product)
        else:
            old_qty = int(existing.get("quantity", 1) or 1)
            old_deposit_total = _to_decimal(existing.get("deposit_value", 0))
            unit_deposit = (
                old_deposit_total / Decimal(old_qty)
                if old_qty > 0
                else Decimal("0.00")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Tax
        if product is not None:
            tax_pct, is_taxable = self._resolve_tax_pct_and_applicability(product)
        else:
            tax_pct = _to_decimal(existing.get("tax_percentage", 0))
            is_taxable = tax_pct > 0

        qty_dec = Decimal(qty)

        goods_total = (unit_price * qty_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        deposit_total = (unit_deposit * qty_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        tax_value = self._extract_vat_from_gross(goods_total, tax_pct) if is_taxable else Decimal("0.00")

        # IMPORTANT:
        # Customer pays goods_total + deposit only.
        # Tax is already inside goods_total.
        line_total = (goods_total + deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if product is not None:
            cost_price = self._get_cost_price(product)
        else:
            cost_price = Decimal("0.00")

        profit_value = (goods_total - (cost_price * qty_dec) - tax_value).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        if available_stock is not None:
            remaining = available_stock - qty
            low_stock_threshold = int(getattr(product, "low_stock_threshold", 5) or 5)
            low_stock = remaining <= low_stock_threshold
            stock_left = max(0, remaining)
        else:
            low_stock = bool(existing.get("low_stock", False))
            stock_left = int(existing.get("stock_left", 0) or 0)

        existing.update({
            "barcode": barcode,
            "name": existing.get("name", getattr(product, "name", "") if product else ""),
            "price": self._format_str(unit_price),
            "quantity": int(qty),
            "tax_percentage": self._format_str(tax_pct),
            "tax_value": self._format_str(tax_value),
            "deposit_value": self._format_str(deposit_total),
            "profit_value": self._format_str(profit_value),
            "line_total": self._format_str(line_total),
            "variable_price": bool(variable_flag),
            "low_stock": bool(low_stock),
            "stock_left": int(stock_left),
        })

        self._cart[barcode] = existing
        self.save()

        return {"status": "ok", "removed": False}

    def add(self, product: Product, quantity: int = 1, variable_price=None) -> Dict[str, Any]:
        """
        Add product or increase existing quantity.
        """
        if product is None:
            return {"status": "error", "message": "No product provided."}

        try:
            qty = int(quantity)
        except Exception:
            qty = 1

        if qty <= 0:
            return {"status": "noop"}

        barcode = clean_barcode(getattr(product, "barcode", ""))

        if not barcode:
            return {"status": "error", "message": "Product barcode missing."}

        available_stock = int(getattr(product, "qty", 0) or 0)

        # Handle old dirty key if it exists in cart under normalized version.
        current_qty = int(self._cart.get(barcode, {}).get("quantity", 0) or 0)

        # If barcode not found but normalized match exists, merge it.
        if barcode not in self._cart:
            norm = normalize_barcode_compare(barcode)
            for old_key in list(self._cart.keys()):
                if normalize_barcode_compare(old_key) == norm:
                    self._cart[barcode] = self._cart.pop(old_key)
                    self._cart[barcode]["barcode"] = barcode
                    current_qty = int(self._cart[barcode].get("quantity", 0) or 0)
                    break

        new_qty = current_qty + qty

        if new_qty > available_stock:
            return {
                "status": "error",
                "message": f"Insufficient stock. Available: {available_stock}, Requested in cart: {new_qty}",
            }

        unit_price, variable_flag = self._get_unit_price(product, variable_price)

        if barcode not in self._cart:
            self._cart[barcode] = {
                "barcode": barcode,
                "name": str(getattr(product, "name", "") or getattr(product, "display_name", "")),
                "price": self._format_str(unit_price),
                "quantity": int(qty),
                "tax_percentage": "0.00",
                "tax_value": "0.00",
                "deposit_value": "0.00",
                "profit_value": "0.00",
                "line_total": "0.00",
                "variable_price": bool(variable_flag),
                "low_stock": False,
                "stock_left": 0,
            }
        else:
            if variable_price is not None and str(variable_price).strip() != "":
                self._cart[barcode]["price"] = self._format_str(unit_price)
                self._cart[barcode]["variable_price"] = True

        return self._recalculate_item(
            barcode=barcode,
            product=product,
            quantity=new_qty,
            variable_price=variable_price if variable_price is not None else None,
        )

    def set_quantity(self, product_or_barcode, quantity):
        """
        Set exact quantity manually.
        """
        barcode = clean_barcode(getattr(product_or_barcode, "barcode", product_or_barcode))

        if barcode not in self._cart:
            norm = normalize_barcode_compare(barcode)
            for old_key in list(self._cart.keys()):
                if normalize_barcode_compare(old_key) == norm:
                    self._cart[barcode] = self._cart.pop(old_key)
                    self._cart[barcode]["barcode"] = barcode
                    break

        if barcode not in self._cart:
            return {"status": "error", "message": "Item not in cart."}

        try:
            q = int(quantity)
        except Exception:
            return {"status": "error", "message": "Invalid quantity."}

        if q <= 0:
            del self._cart[barcode]
            self.save()
            return {"status": "ok", "removed": True}

        product = self._find_product_for_barcode(barcode)

        return self._recalculate_item(barcode=barcode, product=product, quantity=q)

    def decrement(self, product_or_barcode, amount=1):
        barcode = clean_barcode(getattr(product_or_barcode, "barcode", product_or_barcode))

        if barcode not in self._cart:
            norm = normalize_barcode_compare(barcode)
            for old_key in list(self._cart.keys()):
                if normalize_barcode_compare(old_key) == norm:
                    barcode = old_key
                    break

        if barcode not in self._cart:
            return {"status": "error", "message": "Item not in cart."}

        try:
            amount = int(amount)
        except Exception:
            amount = 1

        if amount <= 0:
            return {"status": "noop"}

        old_qty = int(self._cart[barcode].get("quantity", 0) or 0)
        new_qty = old_qty - amount

        return self.set_quantity(barcode, new_qty)

    def remove(self, product_or_barcode):
        barcode = clean_barcode(getattr(product_or_barcode, "barcode", product_or_barcode))

        if barcode not in self._cart:
            norm = normalize_barcode_compare(barcode)
            for old_key in list(self._cart.keys()):
                if normalize_barcode_compare(old_key) == norm:
                    barcode = old_key
                    break

        if barcode in self._cart:
            del self._cart[barcode]
            self.save()

        return {"status": "ok"}

    def clear(self):
        self.session[self.key] = {}
        self._cart = {}
        self.session.modified = True

    def save(self):
        self.session[self.key] = self._cart
        self.session.modified = True

    def isNotEmpty(self):
        return bool(self._cart and len(self._cart) > 0)

    def cart_total(self):
        total = Decimal("0.00")
        for item in self._cart.values():
            total += _to_decimal(item.get("line_total", 0))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_total_price(self):
        return self.cart_total()

    def get_total_vat(self):
        total = Decimal("0.00")
        for item in self._cart.values():
            total += _to_decimal(item.get("tax_value", 0))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_total_deposit(self):
        total = Decimal("0.00")
        for item in self._cart.values():
            total += _to_decimal(item.get("deposit_value", 0))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_subtotal_without_tax(self):
        subtotal = self.cart_total() - self.get_total_vat()
        if subtotal < Decimal("0.00"):
            subtotal = Decimal("0.00")
        return subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_total_profit(self):
        total = Decimal("0.00")
        for item in self._cart.values():
            total += _to_decimal(item.get("profit_value", 0))
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def returns(self):
        for barcode, item in list(self._cart.items()):
            qty = abs(int(item.get("quantity", 0) or 0))

            item["quantity"] = -qty
            item["tax_value"] = self._format_str(-_to_decimal(item.get("tax_value", 0)))
            item["deposit_value"] = self._format_str(-_to_decimal(item.get("deposit_value", 0)))
            item["line_total"] = self._format_str(-_to_decimal(item.get("line_total", 0)))
            item["profit_value"] = self._format_str(-_to_decimal(item.get("profit_value", 0)))

            self._cart[barcode] = item

        self.save()

    def __len__(self):
        total_qty = 0
        for item in self._cart.values():
            try:
                total_qty += int(item.get("quantity", 0) or 0)
            except Exception:
                pass
        return total_qty

    def __iter__(self):
        for barcode, raw in list(self._cart.items()):
            yield {
                "barcode": barcode,
                "name": raw.get("name", ""),
                "quantity": int(raw.get("quantity", 0) or 0),
                "price": _to_decimal(raw.get("price", 0)),
                "tax_percentage": _to_decimal(raw.get("tax_percentage", 0)),
                "tax_value": _to_decimal(raw.get("tax_value", 0)),
                "deposit_value": _to_decimal(raw.get("deposit_value", 0)),
                "profit_value": _to_decimal(raw.get("profit_value", 0)),
                "line_total": _to_decimal(raw.get("line_total", 0)),
                "variable_price": bool(raw.get("variable_price", False)),
                "low_stock": bool(raw.get("low_stock", False)),
                "stock_left": int(raw.get("stock_left", 0) or 0),
            }

    @property
    def items(self):
        out = []

        for barcode, raw in self._cart.items():
            typed = {
                "barcode": barcode,
                "name": raw.get("name", ""),
                "quantity": int(raw.get("quantity", 0) or 0),
                "price": _to_decimal(raw.get("price", 0)),
                "tax_percentage": _to_decimal(raw.get("tax_percentage", 0)),
                "tax_value": _to_decimal(raw.get("tax_value", 0)),
                "deposit_value": _to_decimal(raw.get("deposit_value", 0)),
                "profit_value": _to_decimal(raw.get("profit_value", 0)),
                "line_total": _to_decimal(raw.get("line_total", 0)),
                "variable_price": bool(raw.get("variable_price", False)),
                "low_stock": bool(raw.get("low_stock", False)),
                "stock_left": int(raw.get("stock_left", 0) or 0),
            }
            out.append((barcode, typed))

        return out

    def to_dict(self):
        return {barcode: dict(item) for barcode, item in self._cart.items()}


# ============================================================
# Display Buttons Model
# ============================================================

class displayed_items(models.Model):
    barcode = models.CharField(unique=True, max_length=64, blank=False, null=False)
    display_name = models.CharField(max_length=125, blank=False, null=False)
    display_info = models.CharField(max_length=125, blank=True, null=False, default="")
    display_color = ColorField(default="#575757")
    variable_price = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.display_name} ({self.barcode})"

    def save(self, *args, **kwargs):
        barcode = clean_barcode(self.barcode)
        self.barcode = barcode

        if Product.objects.filter(barcode=barcode).exists():
            return super().save(*args, **kwargs)

        raise ValidationError(
            f"Cannot save displayed item: no product with barcode '{barcode}' exists."
        )

    class Meta:
        verbose_name_plural = "Displayed Items"