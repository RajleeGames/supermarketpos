# transaction/views.py
import copy
from datetime import datetime, timedelta, time as dt_time, timezone as py_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from urllib.parse import urlencode
import traceback
import json
import base64
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256
import pandas as pd
from inventory.models import Product, InventoryHistory
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.db import transaction as db_transaction
from django.db.models import Sum, F, ExpressionWrapper, DecimalField, Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils import timezone as dj_timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from escpos.printer import Usb

from cart.models import Cart
from .forms import ExpenseForm
from .models import (
    transaction,
    productTransaction,
    Expense,
    Debt,
    DebtPayment,
    DayClose,
)

getcontext().prec = 28


# ============================================================
# General Helpers
# ============================================================

def currency_symbol():
    return getattr(settings, "CURRENCY_SYMBOL", "TZS")


def safe_decimal(value, default=Decimal("0.00")):
    if value is None or value == "":
        return default

    if isinstance(value, Decimal):
        try:
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return default

    try:
        d = Decimal(str(value))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as e:
        print("safe_decimal conversion error:", e, "value:", value)
        return default


def fmt(amount, decimals=2):
    try:
        dec = safe_decimal(amount)
        formatted = f"{dec:,.{decimals}f}"
    except Exception:
        formatted = f"{Decimal('0.00'):,.{decimals}f}"
    return f"{currency_symbol()} {formatted}"


def fmt_no_sym(amount, decimals=2):
    try:
        dec = safe_decimal(amount)
        return f"{dec:,.{decimals}f}"
    except Exception:
        return f"{Decimal('0.00'):,.{decimals}f}"


def _fmt_amount(value):
    try:
        return "{:,.2f}".format(float(value or 0))
    except Exception:
        return "0.00"


def _money(value):
    return safe_decimal(value)


def _fmt_money(value):
    return fmt_no_sym(value)


def _today_local_date():
    return dj_timezone.localtime(dj_timezone.now()).date()


def _local_day_range_utc(day_date):
    """
    Build proper local-day range and convert to UTC-aware range.
    This avoids wrong totals when DB stores aware datetimes.
    """
    local_tz = dj_timezone.get_current_timezone()

    start_naive = datetime.combine(day_date, dt_time.min)
    end_naive = datetime.combine(day_date + timedelta(days=1), dt_time.min)

    start_local = dj_timezone.make_aware(start_naive, local_tz)
    end_local = dj_timezone.make_aware(end_naive, local_tz)

    return start_local.astimezone(py_timezone.utc), end_local.astimezone(py_timezone.utc)


def _date_from_str(value, fallback=None):
    try:
        if value:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        pass
    return fallback


# ============================================================
# Cart Helpers
# ============================================================

def get_cart_from_session(request):
    cart_key = getattr(settings, "CART_SESSION_ID", "cart")
    return request.session.get(cart_key, {})


def cart_is_empty(cart):
    if not cart:
        return True
    if isinstance(cart, dict) and len(cart.keys()) == 0:
        return True
    if isinstance(cart, list) and len(cart) == 0:
        return True
    return False


def get_cart_items(cart):
    if cart is None:
        return []

    if isinstance(cart, dict):
        return [v for v in cart.values() if isinstance(v, dict)]

    if isinstance(cart, list):
        return [v for v in cart if isinstance(v, dict)]

    return []


def get_item_name(item):
    return (
        item.get("name")
        or item.get("product_name")
        or item.get("description")
        or item.get("title")
        or item.get("product")
        or "ITEM"
    )


def get_item_qty(item):
    raw = (
        item.get("quantity")
        or item.get("qty")
        or item.get("count")
        or item.get("Quantity")
        or 1
    )

    try:
        qty_dec = Decimal(str(raw))
        if qty_dec <= 0:
            return Decimal("1.00")
        return qty_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("1.00")


def get_item_price(item):
    raw = (
        item.get("price")
        or item.get("selling_price")
        or item.get("unit_price")
        or item.get("rate")
        or item.get("Price")
        or 0
    )
    return safe_decimal(raw)


def get_item_line_total(item):
    raw_total = (
        item.get("line_total")
        or item.get("total")
        or item.get("amount")
        or item.get("subtotal")
        or item.get("sub_total")
        or item.get("lineTotal")
    )

    if raw_total not in (None, ""):
        return safe_decimal(raw_total)

    price = get_item_price(item)
    qty = get_item_qty(item)

    return (price * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def sum_cart_field(cart, field_name="line_total"):
    """
    The real payable cart total comes from line_total.
    Do not add tax again because VAT is already inside selling price.
    """
    total = Decimal("0.00")

    for item in get_cart_items(cart):
        try:
            total += get_item_line_total(item)
        except Exception as e:
            print("sum_cart_field error:", e, "item:", item)

    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ============================================================
# Receipt Helpers
# ============================================================

def format_qty(qty):
    try:
        q = Decimal(str(qty))
        if q == q.to_integral():
            return str(int(q))
        return str(q.normalize())
    except Exception:
        return str(qty)


def split_product_name(name, width=42):
    name = str(name or "").strip().upper()
    if not name:
        return ["ITEM"]

    words = name.split()
    lines = []
    current = ""

    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            while len(word) > width:
                lines.append(word[:width])
                word = word[width:]
            if word:
                current = word
            continue

        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines or ["ITEM"]


def money_line(label, amount, width=42):
    left = str(label)
    right = fmt_no_sym(amount)
    spaces = width - len(left) - len(right)
    if spaces < 1:
        spaces = 1
    return left + (" " * spaces) + right


def build_receipt_text(
    transaction_id,
    user,
    payment_type,
    enhanced_rows,
    total_dec,
    tax_total,
    paid_amount=Decimal("0.00"),
    debtor_name=None,
    phone_number=None,
):
    """
    Builds receipt text saved inside transaction.receipt.

    Layout requested:
    - Header + Sales Receipt centered.
    - TIN, VRN, Till No, Receipt No left aligned.
    - DESCRIPTION / QTY PRICE AMOUNT left aligned.
    """
    receipt_width = 42
    separator = "-" * receipt_width

    merchant_sub_total = (safe_decimal(total_dec) - safe_decimal(tax_total)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )
    if merchant_sub_total < Decimal("0.00"):
        merchant_sub_total = Decimal("0.00")

    try:
        transaction_dt_obj = datetime.strptime(transaction_id[:-6], "%Y%m%d%H%M%S")
        sale_datetime_str = transaction_dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        transaction_dt_obj = datetime.now()
        sale_datetime_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    username_str = getattr(user, "username", "Unknown")

    lines = []

    # Centered store header
    header = getattr(settings, "RECEIPT_HEADER", "")
    if header:
        for line in header.splitlines():
            clean = str(line).strip()
            lines.append(clean.center(receipt_width) if clean else "")
    else:
        lines.append("ADAMS MINI SUPERMARKET".center(receipt_width))
        lines.append("PO BOX 542 MOSHI".center(receipt_width))
        lines.append("J.K. Nyerere Street".center(receipt_width))
        lines.append("+255744844699".center(receipt_width))
        lines.append("adamssupermarket@gmail.com".center(receipt_width))

    lines.append("")
    lines.append("*** NON FISCAL RECEIPT ***".center(receipt_width))
    lines.append("")

    # Left-aligned receipt meta
    lines.append("TIN: 102-188-357")
    lines.append("VRN: 40-318362-M")
    lines.append("Till No: Till003")
    lines.append(f"Receipt No: {transaction_id}")
    lines.append("")
    lines.append(separator)

    # Left-aligned item header
    lines.append("DESCRIPTION")
    lines.append("QTY   PRICE       AMOUNT")
    lines.append("")

    for row in enhanced_rows:
        item_name = str(row.get("name", "ITEM") or "ITEM").strip().upper()

        for name_line in split_product_name(item_name, receipt_width):
            lines.append(name_line)

        qty_text = format_qty(row["qty"])
        price_text = fmt_no_sym(row["price"])
        amount_text = fmt_no_sym(row["amount"])

        # Left-aligned values
        lines.append(f"{qty_text:<5}{price_text:<12}{amount_text}")
        lines.append("")

    lines.append(separator)
    lines.append(money_line("Sub Total", merchant_sub_total, receipt_width))
    lines.append(money_line("Tax", tax_total, receipt_width))
    lines.append(money_line("Total Amount", total_dec, receipt_width))
    lines.append("")

    pay_type = str(payment_type or "").strip().upper()

    if pay_type == "CASH":
        paid_dec = safe_decimal(paid_amount)
        balance = paid_dec - safe_decimal(total_dec)
        if balance < Decimal("0.00"):
            balance = Decimal("0.00")

        lines.append(money_line("Cash", paid_dec, receipt_width))
        lines.append(money_line("Balance", balance, receipt_width))

    elif pay_type == "DEBT":
        paid_dec = safe_decimal(paid_amount)
        debt_balance = safe_decimal(total_dec) - paid_dec
        if debt_balance < Decimal("0.00"):
            debt_balance = Decimal("0.00")

        lines.append(money_line("Paid", paid_dec, receipt_width))
        lines.append(money_line("Debt Balance", debt_balance, receipt_width))

        if debtor_name:
            lines.append(f"Debtor: {str(debtor_name)[:30]}")
        if phone_number:
            lines.append(f"Phone: {str(phone_number)[:20]}")

    else:
        lines.append(f"Payment Type: {pay_type}")

    lines.append("")
    lines.append(getattr(settings, "RECEIPT_FOOTER", "You are Welcomed !").center(receipt_width))
    lines.append(f"Sale Datetime: {sale_datetime_str}".center(receipt_width))
    lines.append("")
    lines.append(f"Served by: {username_str}".center(receipt_width))

    lines.append("")
    lines.append("")
    lines.append("")
    lines.append("")
    lines.append("")
    lines.append("")

    return "\n".join(lines), transaction_dt_obj, merchant_sub_total


from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from django.db.models import Sum, F, DecimalField, ExpressionWrapper
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from urllib.parse import urlencode

# Make sure InventoryHistory is imported in this file:
# from inventory.models import Product, InventoryHistory


def get_current_stock_value():
    """
    Current stock value = sum of qty * cost_price for all products.
    This is the live snapshot only.
    """
    total = Decimal("0.00")

    products = Product.objects.all().only("qty", "cost_price")

    for p in products:
        try:
            qty = safe_decimal(getattr(p, "qty", 0) or 0)
            cost = safe_decimal(getattr(p, "cost_price", 0) or 0)

            if qty > Decimal("1000000"):
                qty = Decimal("0.00")
            if cost > Decimal("100000000"):
                cost = Decimal("0.00")

            item_total = qty * cost

            if item_total.is_nan():
                item_total = Decimal("0.00")

            if item_total > Decimal("999999999999"):
                item_total = Decimal("0.00")

            total += item_total

        except Exception as e:
            print("get_current_stock_value error:", e)
            continue

    try:
        total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        total = Decimal("0.00")

    if total < Decimal("0.00"):
        total = Decimal("0.00")

    return total


def get_previous_close_stock_value(day_date, allow_live_fallback=True):
    """
    Get the latest previous day's closing stock.
    If none exists, fall back to live stock only when allow_live_fallback=True.
    """
    try:
        previous_close = (
            DayClose.objects.filter(close_date__lt=day_date)
            .order_by("-close_date")
            .only("closing_stock_value")
            .first()
        )

        if previous_close:
            value = safe_decimal(previous_close.closing_stock_value)
            if value < Decimal("0.00"):
                return Decimal("0.00")
            return value

    except Exception as e:
        print("get_previous_close_stock_value error:", e)

    if allow_live_fallback:
        return get_current_stock_value()

    return Decimal("0.00")


def calculate_day_stock_summary(day_date):
    """
    Build the daily stock summary for DayClose.
    Formula:
        closing = opening + purchases - cogs
    """
    start_utc, end_utc = _local_day_range_utc(day_date)

    # Sales transactions for the day
    tx_qs = transaction.objects.filter(
        transaction_dt__gte=start_utc,
        transaction_dt__lt=end_utc
    )

    try:
        sales_incl_vat = safe_decimal(
            tx_qs.aggregate(total=Sum("total_sale"))["total"] or Decimal("0.00")
        )
    except Exception as e:
        print("calculate_day_stock_summary sales_incl_vat error:", e)
        sales_incl_vat = Decimal("0.00")

    try:
        vat_total = safe_decimal(
            tx_qs.aggregate(total=Sum("tax_total"))["total"] or Decimal("0.00")
        )
    except Exception as e:
        print("calculate_day_stock_summary vat_total error:", e)
        vat_total = Decimal("0.00")

    sales_excl_vat = (sales_incl_vat - vat_total).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )
    if sales_excl_vat < Decimal("0.00"):
        sales_excl_vat = Decimal("0.00")

    # COGS for the day
    try:
        expr = ExpressionWrapper(
            F("cost_price") * F("qty"),
            output_field=DecimalField(max_digits=20, decimal_places=2)
        )
        cogs = safe_decimal(
            productTransaction.objects.filter(
                transaction_date_time__gte=start_utc,
                transaction_date_time__lt=end_utc
            ).aggregate(total=Sum(expr))["total"] or Decimal("0.00")
        )
    except Exception as e:
        print("calculate_day_stock_summary cogs error:", e)
        cogs = Decimal("0.00")

    if cogs < Decimal("0.00"):
        cogs = Decimal("0.00")

    # Purchases / stock additions for the day
    # IMPORTANT: use the same UTC day range, not timestamp__date
    try:
        purchases_value = safe_decimal(
            InventoryHistory.objects.filter(
                timestamp__gte=start_utc,
                timestamp__lt=end_utc
            ).aggregate(total=Sum("total_cost"))["total"] or Decimal("0.00")
        )
    except Exception as e:
        print("calculate_day_stock_summary purchases error:", e)
        purchases_value = Decimal("0.00")

    if purchases_value < Decimal("0.00"):
        purchases_value = Decimal("0.00")

    # Opening stock = previous day's closing stock
    opening_stock_value = get_previous_close_stock_value(day_date, allow_live_fallback=True)
    if opening_stock_value < Decimal("0.00"):
        opening_stock_value = Decimal("0.00")

    gross_profit = (sales_excl_vat - cogs).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    closing_stock_value = (opening_stock_value + purchases_value - cogs).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )
    if closing_stock_value < Decimal("0.00"):
        closing_stock_value = Decimal("0.00")

    return {
        "opening_stock_value": opening_stock_value,
        "purchases_value": purchases_value,
        "sales_excl_vat": sales_excl_vat,
        "sales_incl_vat": sales_incl_vat,
        "vat_total": vat_total,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "closing_stock_value": closing_stock_value,
    }


def build_stock_report_rows(start_date=None, end_date=None):
    """
    Rebuild the displayed report as a running chain.
    This protects the view even if an old DayClose row had a wrong opening stock.
    """
    qs = DayClose.objects.select_related("closed_by").order_by("close_date")

    if start_date and end_date:
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        qs = qs.filter(close_date__range=(start_date, end_date))
    elif start_date:
        qs = qs.filter(close_date__gte=start_date)
    elif end_date:
        qs = qs.filter(close_date__lte=end_date)

    rows = list(qs)

    # Baseline opening for the first displayed row:
    # previous close before the range, or live stock if nothing exists.
    baseline_opening = None
    if rows:
        first_date = rows[0].close_date
        baseline_opening = get_previous_close_stock_value(first_date, allow_live_fallback=True)

    rebuilt = []
    running_opening = baseline_opening if baseline_opening is not None else Decimal("0.00")

    for row in rows:
        opening = safe_decimal(running_opening)
        purchases = safe_decimal(row.purchases_value)
        cogs = safe_decimal(row.cogs)

        sales_excl_vat = safe_decimal(row.sales_excl_vat)
        sales_incl_vat = safe_decimal(row.sales_incl_vat)
        vat_total = safe_decimal(row.vat_total)
        gross_profit = safe_decimal(row.gross_profit)

        closing = (opening + purchases - cogs).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if closing < Decimal("0.00"):
            closing = Decimal("0.00")

        rebuilt.append(SimpleNamespace(
            close_date=row.close_date,
            opening_stock_value=opening,
            purchases_value=purchases,
            sales_excl_vat=sales_excl_vat,
            sales_incl_vat=sales_incl_vat,
            vat_total=vat_total,
            cogs=cogs,
            gross_profit=gross_profit,
            closing_stock_value=closing,
            closed_by=row.closed_by,
            closed_at=row.closed_at,
            note=row.note,
            cash_total=safe_decimal(row.cash_total),
            ebt_total=safe_decimal(row.ebt_total),
            card_total=safe_decimal(row.card_total),
            debt_total=safe_decimal(row.debt_total),
            total_sales=safe_decimal(row.total_sales),
            transaction_count=row.transaction_count,
        ))

        running_opening = closing

    return rebuilt


@login_required(login_url="/user/login/")
def stock_report(request):
    start_raw = request.GET.get("start_date", "")
    end_raw = request.GET.get("end_date", "")

    start_date = _date_from_str(start_raw)
    end_date = _date_from_str(end_raw)

    # Rebuild rows in a proper running chain
    rebuilt_rows = build_stock_report_rows(start_date=start_date, end_date=end_date)

    paginator = Paginator(rebuilt_rows, 25)
    page_number = request.GET.get("page", 1)

    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    extra_qs = ""
    if get_copy:
        extra_qs = "&" + urlencode(get_copy)

    return render(request, "transaction/stock_report.html", {
        "rows": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "start_date": start_date,
        "end_date": end_date,
        "is_filtered": bool(start_date or end_date),
        "extra_qs": extra_qs,
        "total_rows": len(rebuilt_rows),
    })




# ============================================================
# Forms / Printer
# ============================================================

class DateSelector(forms.Form):
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )


class printer:
    printer = None

    @staticmethod
    def printReceipt(printText, times=0, *args, **kwargs):
        try:
            if printer.printer:
                printer.printer.text(printText)
                printer.printer.text(f"\nPrint Time: {datetime.now():%Y-%m-%d %H:%M}\n\n\n\n\n")
        except Exception:
            try:
                printer.connectPrinter()
                if times < 3:
                    printer.printReceipt(printText, times + 1)
            except Exception as e2:
                print("printer.printReceipt failed:", e2)

    @staticmethod
    def connectPrinter():
        try:
            vid = getattr(settings, "PRINTER_VENDOR_ID", "")
            pid = getattr(settings, "PRINTER_PRODUCT_ID", "")

            try:
                vid_eval = eval(vid) if vid else None
            except Exception:
                try:
                    vid_eval = int(str(vid)) if vid else None
                except Exception:
                    vid_eval = None

            try:
                pid_eval = eval(pid) if pid else None
            except Exception:
                try:
                    pid_eval = int(str(pid)) if pid else None
                except Exception:
                    pid_eval = None

            if vid_eval is not None and pid_eval is not None:
                printer.printer = Usb(vid_eval, pid_eval)
            else:
                printer.printer = None

        except Exception as e:
            print("Printer connection error:", e)
            printer.printer = None


# ============================================================
# End Day / Close Day Helpers
# ============================================================

def get_transaction_qs_for_day(day_date):
    start_utc, end_utc = _local_day_range_utc(day_date)
    return transaction.objects.filter(
        transaction_dt__gte=start_utc,
        transaction_dt__lt=end_utc
    )


def build_day_close_summary(close_date):
    qs = get_transaction_qs_for_day(close_date)

    cash_total = _money(
        qs.filter(payment_type__iexact="CASH").aggregate(s=Sum("total_sale"))["s"]
    )

    ebt_total = _money(
        qs.filter(payment_type__iexact="EBT").aggregate(s=Sum("total_sale"))["s"]
    )

    card_total = _money(
        qs.filter(
            Q(payment_type__iexact="DEBIT/CREDIT") |
            Q(payment_type__iexact="DEBIT_CREDIT") |
            Q(payment_type__iexact="CARD") |
            Q(payment_type__iexact="BANK")
        ).aggregate(s=Sum("total_sale"))["s"]
    )

    debt_total = _money(
        qs.filter(payment_type__iexact="DEBT").aggregate(s=Sum("total_sale"))["s"]
    )

    total_sales = _money(qs.aggregate(s=Sum("total_sale"))["s"])
    transaction_count = qs.count()

    return {
        "close_date": close_date,
        "cash_total": cash_total,
        "ebt_total": ebt_total,
        "card_total": card_total,
        "debt_total": debt_total,
        "total_sales": total_sales,
        "transaction_count": transaction_count,
        "cash_total_display": _fmt_money(cash_total),
        "ebt_total_display": _fmt_money(ebt_total),
        "card_total_display": _fmt_money(card_total),
        "debt_total_display": _fmt_money(debt_total),
        "total_sales_display": _fmt_money(total_sales),
    }


def get_unclosed_previous_sales_date():
    """
    Finds the oldest previous sales day that has transactions but has not been closed.
    Register must block selling if this returns a date.
    """
    today = _today_local_date()
    local_tz = dj_timezone.get_current_timezone()

    qs = transaction.objects.filter(transaction_dt__date__lt=today).order_by("transaction_dt")

    seen_dates = []
    seen_set = set()

    for tx in qs.only("transaction_dt"):
        try:
            local_date = dj_timezone.localtime(tx.transaction_dt, local_tz).date()
        except Exception:
            local_date = tx.transaction_dt.date()

        if local_date >= today:
            continue

        if local_date not in seen_set:
            seen_dates.append(local_date)
            seen_set.add(local_date)

    for day in seen_dates:
        if not DayClose.objects.filter(close_date=day).exists():
            return day

    return None


def can_sell_today():
    return get_unclosed_previous_sales_date() is None


def _block_if_previous_day_not_closed(request):
    unclosed_date = get_unclosed_previous_sales_date()
    if unclosed_date:
        messages.error(
            request,
            f"Sales are blocked. Please close day {unclosed_date} before selling today."
        )
        return redirect(f"{reverse('end_day_home')}?date={unclosed_date}")
    return None


# ============================================================
# End Day / Close Day Views
# ============================================================

from types import SimpleNamespace
from decimal import InvalidOperation
from django.db import connection

@login_required(login_url="/user/login/")
def end_day_home(request):
    today = _today_local_date()
    unclosed_date = get_unclosed_previous_sales_date()

    selected_date = _date_from_str(
        request.GET.get("date"),
        fallback=unclosed_date or today
    )

    summary = build_day_close_summary(selected_date)

    already_closed_exists = DayClose.objects.filter(
        close_date=selected_date
    ).exists()

    already_closed = None
    if already_closed_exists:
        try:
            already_closed = DayClose.objects.select_related("closed_by").get(
                close_date=selected_date
            )
        except InvalidOperation:
            messages.error(
                request,
                "That DayClose row has invalid decimal data. Delete old DayClose records and close the day again."
            )
            already_closed = None
            already_closed_exists = False
        except Exception as e:
            print("end_day_home already_closed error:", e)
            already_closed = None
            already_closed_exists = False

    try:
        recent_closes = list(
            DayClose.objects.select_related("closed_by")
            .order_by("-close_date")[:30]
        )
    except InvalidOperation:
        table = DayClose._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT id, close_date,
                       cash_total, ebt_total, card_total,
                       debt_total, total_sales,
                       transaction_count, closed_at, note,
                       closed_by_id
                FROM {table}
                ORDER BY close_date DESC
                LIMIT 30
            """)
            cols = [c[0] for c in cursor.description]
            recent_closes = []

            for rec in cursor.fetchall():
                data = dict(zip(cols, rec))
                for field in [
                    "cash_total", "ebt_total", "card_total",
                    "debt_total", "total_sales"
                ]:
                    data[field] = safe_decimal(data.get(field))
                recent_closes.append(SimpleNamespace(**data))
    except Exception as e:
        print("end_day_home recent_closes error:", e)
        recent_closes = []

    return render(request, "end_day.html", {
        "summary": summary,
        "selected_date": selected_date,
        "today": today,
        "unclosed_date": unclosed_date,
        "already_closed": already_closed,
        "already_closed_exists": already_closed_exists,
        "recent_closes": recent_closes,
        "currency": currency_symbol(),
    })


@login_required(login_url="/user/login/")
@require_POST
def close_day_action(request):
    close_date = _date_from_str(request.POST.get("close_date"))

    if not close_date:
        messages.error(request, "Invalid close date.")
        return redirect("end_day_home")

    try:
        sales_summary = build_day_close_summary(close_date)
    except Exception as e:
        print("close_day_action sales_summary error:", e)
        sales_summary = {
            "cash_total": Decimal("0.00"),
            "ebt_total": Decimal("0.00"),
            "card_total": Decimal("0.00"),
            "debt_total": Decimal("0.00"),
            "total_sales": Decimal("0.00"),
            "transaction_count": 0,
        }

    try:
        stock_summary = calculate_day_stock_summary(close_date)
    except Exception as e:
        print("close_day_action stock_summary error:", e)
        stock_summary = {
            "opening_stock_value": Decimal("0.00"),
            "purchases_value": Decimal("0.00"),
            "sales_excl_vat": Decimal("0.00"),
            "sales_incl_vat": Decimal("0.00"),
            "vat_total": Decimal("0.00"),
            "cogs": Decimal("0.00"),
            "gross_profit": Decimal("0.00"),
            "closing_stock_value": Decimal("0.00"),
        }

    db_values = {
        "cash_total": safe_decimal(sales_summary["cash_total"]),
        "ebt_total": safe_decimal(sales_summary["ebt_total"]),
        "card_total": safe_decimal(sales_summary["card_total"]),
        "debt_total": safe_decimal(sales_summary["debt_total"]),
        "total_sales": safe_decimal(sales_summary["total_sales"]),
        "transaction_count": int(sales_summary["transaction_count"] or 0),
        "opening_stock_value": safe_decimal(stock_summary["opening_stock_value"]),
        "purchases_value": safe_decimal(stock_summary["purchases_value"]),
        "sales_excl_vat": safe_decimal(stock_summary["sales_excl_vat"]),
        "sales_incl_vat": safe_decimal(stock_summary["sales_incl_vat"]),
        "vat_total": safe_decimal(stock_summary["vat_total"]),
        "cogs": safe_decimal(stock_summary["cogs"]),
        "gross_profit": safe_decimal(stock_summary["gross_profit"]),
        "closing_stock_value": safe_decimal(stock_summary["closing_stock_value"]),
        "closed_by_id": request.user.id,
        "note": (request.POST.get("note") or "").strip(),
    }

    # Update without loading the row first.
    updated = DayClose.objects.filter(close_date=close_date).update(**db_values)

    if not updated:
        DayClose.objects.create(
            close_date=close_date,
            cash_total=db_values["cash_total"],
            ebt_total=db_values["ebt_total"],
            card_total=db_values["card_total"],
            debt_total=db_values["debt_total"],
            total_sales=db_values["total_sales"],
            transaction_count=db_values["transaction_count"],
            opening_stock_value=db_values["opening_stock_value"],
            purchases_value=db_values["purchases_value"],
            sales_excl_vat=db_values["sales_excl_vat"],
            sales_incl_vat=db_values["sales_incl_vat"],
            vat_total=db_values["vat_total"],
            cogs=db_values["cogs"],
            gross_profit=db_values["gross_profit"],
            closing_stock_value=db_values["closing_stock_value"],
            closed_by=request.user,
            note=db_values["note"],
        )

    messages.success(request, f"Day {close_date} closed successfully.")
    return redirect(f"{reverse('end_day_home')}?date={close_date}")




from types import SimpleNamespace
from django.db import connection
from decimal import InvalidOperation



@login_required(login_url="/user/login/")
def day_close_history(request):
    closes = DayClose.objects.select_related("closed_by").order_by("-close_date")

    return render(request, "end_day_history.html", {
        "closes": closes,
        "currency": currency_symbol(),
    })


# ============================================================
# Receipt Views
# ============================================================

def transactionReceipt(request, transNo):
    try:
        obj = transaction.objects.get(transaction_id=transNo)
        receipt = getattr(obj, "receipt", "")
        return render(request, "receiptView.html", {"receipt": receipt, "transNo": transNo})

    except transaction.DoesNotExist:
        raise Http404("No Transactions Found!!!")

    except InvalidOperation as inv:
        print("transactionReceipt InvalidOperation:", inv)
        traceback.print_exc()

        try:
            table = transaction._meta.db_table
            sql = "SELECT receipt FROM %s WHERE transaction_id = ? LIMIT 1" % table
            with connection.cursor() as cursor:
                cursor.execute(sql, [transNo])
                row = cursor.fetchone()

            if not row:
                raise Http404("No Transactions Found!!!")

            return render(request, "receiptView.html", {"receipt": row[0], "transNo": transNo})

        except Exception as e:
            print("transactionReceipt raw fallback error:", e)
            traceback.print_exc()
            raise Http404("No Transactions Found!!!")


def transactionPrintReceipt(request, transNo):
    try:
        try:
            receipt = transaction.objects.get(transaction_id=transNo).receipt
        except InvalidOperation:
            table = transaction._meta.db_table
            sql = "SELECT receipt FROM %s WHERE transaction_id = ? LIMIT 1" % table
            with connection.cursor() as cursor:
                cursor.execute(sql, [transNo])
                row = cursor.fetchone()
            receipt = row[0] if row else ""

        if printer.printer is None:
            printer.connectPrinter()

        if printer.printer and receipt:
            printer.printReceipt(receipt)

        return redirect(f"/transaction_receipt/{transNo}/")

    except Exception as e:
        print("transactionPrintReceipt error:", e)
        traceback.print_exc()
        return redirect("register")


# ============================================================
# Transactions List
# ============================================================

@login_required(login_url="/user/login/")
def transactionView(request):
    local_tz = dj_timezone.get_current_timezone()
    now_local = dj_timezone.localtime(dj_timezone.now(), local_tz)

    default_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    default_end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)

    start_local = default_start_local
    end_local = default_end_local
    start_date_val = None
    end_date_val = None

    start_raw = request.GET.get("start_date", "")
    end_raw = request.GET.get("end_date", "")

    start_date = _date_from_str(start_raw)
    end_date = _date_from_str(end_raw)

    if start_date:
        start_naive = datetime.combine(start_date, dt_time.min)
        start_local = dj_timezone.make_aware(start_naive, local_tz)
        start_date_val = start_date

    if end_date:
        end_naive = datetime.combine(end_date, dt_time.max)
        end_local = dj_timezone.make_aware(end_naive, local_tz)
        end_date_val = end_date

    if end_local < start_local:
        start_local, end_local = end_local, start_local
        start_date_val, end_date_val = end_date_val, start_date_val

    start_utc = start_local.astimezone(py_timezone.utc)
    end_utc = end_local.astimezone(py_timezone.utc)

    qs = transaction.objects.filter(
        transaction_dt__range=(start_utc, end_utc)
    ).order_by("-transaction_dt")

    transactions = []
    for t in qs:
        try:
            local_dt = dj_timezone.localtime(t.transaction_dt, local_tz)
        except Exception:
            local_dt = t.transaction_dt

        transactions.append({
            "transaction_id": t.transaction_id,
            "total_sale": _fmt_amount(t.total_sale),
            "payment_type": t.payment_type or "",
            "transaction_dt": local_dt,
        })

    return render(request, "transactions.html", {
        "transactions": transactions,
        "form": DateSelector(request.GET or None),
        "start_date": start_date_val,
        "end_date": end_date_val,
        "is_filtered": bool(start_date_val or end_date_val),
    })


# ============================================================
# Cart Transaction Actions
# ============================================================

@login_required(login_url="/user/login/")
def returnsTransaction(request):
    block_response = _block_if_previous_day_not_closed(request)
    if block_response:
        return block_response

    Cart(request).returns()
    return redirect("register")


@login_required(login_url="/user/login/")
def suspendTransaction(request):
    """
    Save current cart safely for later recall.
    """
    cart_key = getattr(settings, "CART_SESSION_ID", "cart")
    current_cart = request.session.get(cart_key, {})

    if not isinstance(current_cart, dict) or len(current_cart) <= 0:
        messages.info(request, "No items to suspend.")
        return redirect("register")

    key = datetime.now().strftime("%Y%m%d%H%M%S%f")

    suspended = request.session.get("Cart_Sessions", {})
    if not isinstance(suspended, dict):
        suspended = {}

    suspended[key] = copy.deepcopy(current_cart)

    request.session["Cart_Sessions"] = suspended
    request.session[cart_key] = {}
    request.session.pop("stock_error", None)
    request.session.modified = True

    try:
        request.session.save()
    except Exception:
        pass

    messages.success(request, f"Transaction suspended successfully. ID: {key}")
    return redirect("register")


@login_required(login_url="/user/login/")
def recallTransaction(request, recallTransNo=None):
    """
    Restore suspended cart safely.
    """
    cart_key = getattr(settings, "CART_SESSION_ID", "cart")

    suspended = request.session.get("Cart_Sessions", {})
    if not isinstance(suspended, dict):
        suspended = {}

    current_cart = request.session.get(cart_key, {})

    if recallTransNo:
        recall_key = str(recallTransNo)

        if recall_key not in suspended:
            messages.error(request, f"Suspended transaction not found: {recall_key}")
            return redirect("register")

        # If current cart has items, save it first as another suspended transaction.
        if isinstance(current_cart, dict) and len(current_cart) > 0:
            new_key = datetime.now().strftime("%Y%m%d%H%M%S%f")
            suspended[new_key] = copy.deepcopy(current_cart)

        recalled_cart = copy.deepcopy(suspended.get(recall_key, {}))
        if not isinstance(recalled_cart, dict):
            recalled_cart = {}

        try:
            del suspended[recall_key]
        except Exception:
            pass

        request.session.pop("stock_error", None)
        request.session[cart_key] = recalled_cart
        request.session["Cart_Sessions"] = suspended
        request.session.modified = True

        try:
            request.session.save()
        except Exception:
            pass

        messages.success(request, "Suspended transaction recalled successfully.")
        return redirect("register")

    if suspended and len(suspended) > 0:
        return render(request, "recallTransaction.html", {
            "obj_rt": list(suspended.keys())
        })

    messages.info(request, "No suspended transactions found.")
    return redirect("register")


# ============================================================
# End Transaction Receipt
# ============================================================

@login_required(login_url="/user/login/")
def endTransactionReceipt(request, transNo):
    try:
        change = ""

        if request.GET.get("type") == "cash":
            total = safe_decimal(request.GET.get("total", 0))
            value = safe_decimal(request.GET.get("value", 0))
            change_val = value - total

            change = f"""
                <table class="table text-white h3 p-0 m-0">
                    <tr>
                        <td class="text-left pl-5"> Total : </td>
                        <td class="text-right pr-5"> {fmt(total)} </td>
                    </tr>
                    <tr>
                        <td class="text-left pl-5"> Cash : </td>
                        <td class="text-right pr-5"> {fmt(value)} </td>
                    </tr>
                    <tr class="h1 badge-danger">
                        <td style="padding-top:15px"> Change : </td>
                        <td style="padding-top:15px"> {fmt(change_val)} </td>
                    </tr>
                </table>
            """

        elif request.GET.get("type") == "card":
            total = safe_decimal(request.GET.get("total", 0))
            value = request.GET.get("value", "")

            try:
                value_display = fmt(safe_decimal(value))
            except Exception:
                value_display = str(value)

            change = f"""
                <table class="table text-white h3 p-0 m-0">
                    <tr>
                        <td class="text-left pl-5"> Total : </td>
                        <td class="text-right pr-5"> {fmt(total)} </td>
                    </tr>
                    <tr>
                        <td class="text-left pl-5"> Card : </td>
                        <td class="text-right pr-5"> {value_display}</td>
                    </tr>
                </table>
                <div class="h1 badge-danger p-3">CARD TRANSACTION</div>
            """

        obj = transaction.objects.get(transaction_id=transNo)
        return render(request, "endTransaction.html", {"receipt": obj.receipt, "change": change})

    except transaction.DoesNotExist:
        raise Http404("No Transactions Found!!!")

    except Exception as e:
        print("endTransactionReceipt error:", e)
        traceback.print_exc()
        raise Http404("No Transactions Found!!!")


# ============================================================
# Complete Sale
# ============================================================

@login_required(login_url="/user/login/")
def endTransaction(request, type, value):
    """
    Complete sale.
    Also blocks selling if previous sales day is not closed.
    """
    try:
        block_response = _block_if_previous_day_not_closed(request)
        if block_response:
            return block_response

        cart = get_cart_from_session(request)

        if cart_is_empty(cart):
            print("endTransaction: cart is empty")
            return redirect("register")

        total_dec = sum_cart_field(cart, "line_total")

        if total_dec <= Decimal("0.00"):
            print("endTransaction: total is zero. cart =", cart)
            return redirect("register")

        tx_type = str(type or "").strip().lower()
        raw_value = str(value or "").strip()

        if tx_type == "cash":
            paid_amount = safe_decimal(raw_value)

            if paid_amount < total_dec:
                print("endTransaction: cash paid less than total:", paid_amount, total_dec)
                return redirect("register")

            payment_type = "CASH"

            return_transaction = addTransaction(
                user=request.user,
                payment_type=payment_type,
                total=total_dec,
                cart=cart,
                value=paid_amount,
                paid_amount=paid_amount
            )

        elif tx_type == "card":
            card_value = raw_value.upper()

            if card_value == "EBT":
                payment_type = "EBT"
            elif card_value in ["DEBIT_CREDIT", "DEBIT/CREDIT", "CARD"]:
                payment_type = "DEBIT/CREDIT"
            else:
                payment_type = card_value or "DEBIT/CREDIT"

            return_transaction = addTransaction(
                user=request.user,
                payment_type=payment_type,
                total=total_dec,
                cart=cart,
                value=total_dec,
                paid_amount=total_dec
            )

        else:
            payment_type = str(type or "").strip().upper()

            if payment_type in ["EBT", "DEBIT_CREDIT", "DEBIT/CREDIT", "CARD"]:
                if payment_type == "DEBIT_CREDIT":
                    payment_type = "DEBIT/CREDIT"

                return_transaction = addTransaction(
                    user=request.user,
                    payment_type=payment_type,
                    total=total_dec,
                    cart=cart,
                    value=total_dec,
                    paid_amount=total_dec
                )
            else:
                return_transaction = addTransaction(
                    user=request.user,
                    payment_type=payment_type or "UNKNOWN",
                    total=total_dec,
                    cart=cart,
                    value=total_dec,
                    paid_amount=total_dec
                )

        if not return_transaction:
            print("endTransaction: addTransaction returned None")
            return redirect("register")

        try:
            Cart(request).clear()
        except Exception as e:
            print("endTransaction: Cart clear failed:", e)

        params = {
            "type": type,
            "value": value,
            "total": str(float(total_dec)),
        }

        return redirect(f"/endTransaction/{return_transaction.transaction_id}/?{urlencode(params)}")

    except Exception as e:
        print("endTransaction error:", e, type, value, getattr(request, "user", None))
        traceback.print_exc()
        return redirect("register")


def addTransaction(
    user,
    payment_type,
    total=None,
    cart=None,
    value=None,
    paid_amount=Decimal("0.00"),
    debtor_name=None,
    debt_due_date=None,
    phone_number=None
):
    transaction_id = datetime.now().strftime("%Y%m%d%H%M%S%f")

    try:
        cart_items = get_cart_items(cart)

        enhanced_rows = []
        total_lines_sum = Decimal("0.00")
        tax_total = Decimal("0.00")

        for item in cart_items:
            name = get_item_name(item)
            qty = get_item_qty(item)
            price = get_item_price(item)
            line_total = get_item_line_total(item)

            tax_value = safe_decimal(item.get("tax_value", 0))
            tax_pct = safe_decimal(item.get("tax_percentage", 0))

            if tax_pct <= 0 and tax_value > 0:
                tax_pct = Decimal("18.00")

            if tax_pct > 0:
                try:
                    line_vat = ((line_total * tax_pct) / (Decimal("100.00") + tax_pct)).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP
                    )
                except Exception:
                    line_vat = Decimal("0.00")
            else:
                line_vat = Decimal("0.00")

            tax_total += line_vat
            total_lines_sum += line_total

            enhanced_rows.append({
                "name": str(name).strip(),
                "qty": qty,
                "price": price,
                "amount": line_total,
                "line_vat": line_vat,
            })

        if total is not None:
            total_dec = safe_decimal(total)
        else:
            total_dec = total_lines_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if total_dec <= Decimal("0.00"):
            print("addTransaction: total is zero. cart =", cart)
            return None

        tax_total = safe_decimal(tax_total)

        receipt, transaction_dt_obj, merchant_sub_total = build_receipt_text(
            transaction_id=transaction_id,
            user=user,
            payment_type=payment_type,
            enhanced_rows=enhanced_rows,
            total_dec=total_dec,
            tax_total=tax_total,
            paid_amount=paid_amount,
            debtor_name=debtor_name,
            phone_number=phone_number,
        )

        deposit_total = Decimal("0.00")

        due_date_obj = None
        if debt_due_date:
            try:
                if isinstance(debt_due_date, str):
                    due_date_obj = datetime.strptime(debt_due_date, "%Y-%m-%d").date()
                else:
                    try:
                        due_date_obj = debt_due_date.date()
                    except Exception:
                        due_date_obj = debt_due_date
            except Exception:
                due_date_obj = None

        phone_clean = str(phone_number).strip()[:20] if phone_number else ""

        with db_transaction.atomic():
            header_kwargs = dict(
                transaction_id=transaction_id,
                transaction_dt=transaction_dt_obj,
                user=user,
                total_sale=total_dec,
                sub_total=merchant_sub_total,
                tax_total=tax_total,
                deposit_total=deposit_total,
                payment_type=payment_type,
                receipt=receipt,
                products=str(cart_items),
                debtor_name=(debtor_name or "")[:200],
                debt_due_date=due_date_obj,
            )

            try:
                tx_fields = {
                    f.name for f in transaction._meta.get_fields()
                    if getattr(f, "concrete", True)
                }
                header_kwargs = {k: v for k, v in header_kwargs.items() if k in tx_fields}
            except Exception:
                pass

            obj = transaction.objects.create(**header_kwargs)

            if str(payment_type).strip().upper() == "DEBT":
                candidate_kwargs = {
                    "transaction": obj,
                    "debtor_name": (debtor_name or "")[:200],
                    "total_amount": total_dec,
                    "paid_amount": Decimal("0.00"),
                    "due_date": due_date_obj,
                    "phone_number": phone_clean,
                    "created_by": user,
                }

                try:
                    allowed_debt_fields = {
                        f.name for f in Debt._meta.get_fields()
                        if getattr(f, "concrete", True)
                    }
                except Exception:
                    allowed_debt_fields = set(candidate_kwargs.keys())

                debt_create_kwargs = {
                    k: v for k, v in candidate_kwargs.items()
                    if k in allowed_debt_fields
                }

                debt, created = Debt.objects.get_or_create(
                    transaction=obj,
                    defaults=debt_create_kwargs
                )

                initial_paid = safe_decimal(paid_amount)
                if initial_paid > Decimal("0.00"):
                    actual_initial = initial_paid if initial_paid <= total_dec else total_dec

                    DebtPayment.objects.create(
                        debt=debt,
                        amount=actual_initial,
                        method="CASH",
                        note="Initial payment at sale",
                        paid_by=user
                    )

                try:
                    payments_sum = (
                        DebtPayment.objects
                        .filter(debt=debt)
                        .aggregate(total=Sum("amount"))["total"]
                        or Decimal("0.00")
                    )
                    payments_sum = safe_decimal(payments_sum)

                    if "paid_amount" in allowed_debt_fields:
                        debt.paid_amount = payments_sum

                    try:
                        debt.update_status()
                    except Exception:
                        debt.save()

                except Exception as e:
                    print("addTransaction: failed debt recompute:", e)

        print("Saved transaction:", getattr(obj, "transaction_id", transaction_id))
        return obj

    except Exception as e:
        print("addTransaction: Failed to save transaction:", e)
        traceback.print_exc()
        return None


# ============================================================
# Expenses / Reports
# ============================================================

@login_required(login_url="/user/login/")
def expenses_add(request):
    if request.method == "POST":
        form = ExpenseForm(request.POST)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.created_by = request.user
            expense.save()
            return redirect("expenses_list")
    else:
        form = ExpenseForm()

    return render(request, "transaction/expenses_add.html", {"form": form})


@login_required(login_url="/user/login/")
def expenses_list(request):
    qs = Expense.objects.all().order_by("-id")
    return render(request, "transaction/expenses_list.html", {"expenses": qs})


@login_required(login_url="/user/login/")
def profit_loss(request):
    now = dj_timezone.now()

    start_str = request.GET.get("start_date", "")
    end_str = request.GET.get("end_date", "")

    try:
        if start_str:
            start_date = dj_timezone.make_aware(datetime.strptime(start_str, "%Y-%m-%d"))
        else:
            start_date = dj_timezone.make_aware(datetime(now.year, now.month, 1))
    except Exception:
        start_date = dj_timezone.make_aware(datetime(now.year, now.month, 1))

    try:
        if end_str:
            end_date = dj_timezone.make_aware(datetime.strptime(end_str, "%Y-%m-%d")) + timedelta(days=1)
        else:
            end_date = now + timedelta(seconds=1)
    except Exception:
        end_date = now + timedelta(seconds=1)

    revenue_agg = transaction.objects.filter(
        transaction_dt__gte=start_date,
        transaction_dt__lt=end_date
    ).aggregate(total_revenue=Sum("total_sale"))

    total_revenue = safe_decimal(revenue_agg["total_revenue"] or Decimal("0.00"))

    tax_agg = transaction.objects.filter(
        transaction_dt__gte=start_date,
        transaction_dt__lt=end_date
    ).aggregate(total_tax=Sum("tax_total"))

    total_tax = safe_decimal(tax_agg["total_tax"] or Decimal("0.00"))

    expr = ExpressionWrapper(
        F("cost_price") * F("qty"),
        output_field=DecimalField(max_digits=20, decimal_places=2)
    )

    cogs_agg = productTransaction.objects.filter(
        transaction_date_time__gte=start_date,
        transaction_date_time__lt=end_date
    ).aggregate(total_cogs=Sum(expr))

    total_cogs = safe_decimal(cogs_agg["total_cogs"] or Decimal("0.00"))

    expenses_agg = Expense.objects.filter(
        created_at__gte=start_date,
        created_at__lt=end_date
    ).aggregate(total_expenses=Sum("amount"))

    total_expenses = safe_decimal(expenses_agg["total_expenses"] or Decimal("0.00"))

    gross_profit = total_revenue - total_cogs
    net_profit = gross_profit - total_expenses - total_tax

    return render(request, "transaction/profit_loss.html", {
        "start_date": start_date.date(),
        "end_date": (end_date - timedelta(days=1)).date(),
        "total_revenue": total_revenue,
        "total_cogs": total_cogs,
        "total_tax": total_tax,
        "total_expenses": total_expenses,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
    })


# ============================================================
# Debt Sale
# ============================================================

@login_required(login_url="/user/login/")
def endDebtTransaction(request):
    try:
        block_response = _block_if_previous_day_not_closed(request)
        if block_response:
            return block_response

        if request.method != "POST":
            return HttpResponseBadRequest("POST required")

        cart = get_cart_from_session(request)

        if cart_is_empty(cart):
            return redirect("register")

        total_dec = sum_cart_field(cart, "line_total")

        if total_dec <= Decimal("0.00"):
            print("endDebtTransaction: total zero. cart =", cart)
            return redirect("register")

        paid_amount = safe_decimal(request.POST.get("paid_amount", "0"))
        debtor_name = (request.POST.get("debtor_name", "") or "").strip()
        due_date_raw = (request.POST.get("due_date", "") or "").strip()
        phone_number = (request.POST.get("phone_number", "") or "").strip()

        now = dj_timezone.now()
        window_start = now - timedelta(seconds=6)

        try:
            existing_tx = transaction.objects.filter(
                user=request.user,
                total_sale=total_dec,
                transaction_dt__gte=window_start
            ).order_by("-transaction_dt").first()
        except Exception:
            existing_tx = None

        if existing_tx:
            try:
                Cart(request).clear()
            except Exception:
                pass

            return redirect(
                f"/endTransaction/{existing_tx.transaction_id}/"
                f"?type=debt&value={paid_amount}&total={float(total_dec)}"
            )

        return_transaction = addTransaction(
            user=request.user,
            payment_type="DEBT",
            total=total_dec,
            cart=cart,
            value=str(paid_amount),
            paid_amount=paid_amount,
            debtor_name=debtor_name,
            debt_due_date=(due_date_raw or None),
            phone_number=phone_number
        )

        if return_transaction:
            try:
                Cart(request).clear()
            except Exception:
                pass

            return redirect(
                f"/endTransaction/{return_transaction.transaction_id}/"
                f"?type=debt&value={paid_amount}&total={float(total_dec)}"
            )

        return redirect("register")

    except Exception as e:
        print("endDebtTransaction error:", e)
        traceback.print_exc()
        return redirect("register")


# ============================================================
# Debts
# ============================================================

@login_required(login_url="/user/login/")
def debts_list(request):
    qs = Debt.objects.select_related("transaction", "created_by").order_by("-created_at")

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(debtor_name__icontains=q) |
            Q(phone_number__icontains=q)
        )

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page", 1)

    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    debts_json = json.dumps(
        list(page_obj.object_list.values("id", "debtor_name", "phone_number", "total_amount")),
        cls=DjangoJSONEncoder
    )

    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    extra_qs = ""
    if get_copy:
        extra_qs = "&" + urlencode(get_copy)

    return render(request, "debts_list.html", {
        "debts": page_obj.object_list,
        "debts_json": mark_safe(debts_json),
        "page_obj": page_obj,
        "paginator": paginator,
        "query": q,
        "extra_qs": extra_qs,
    })


@login_required(login_url="/user/login/")
def debt_detail(request, debt_id):
    debt = get_object_or_404(
        Debt.objects.select_related("transaction", "created_by"),
        pk=debt_id
    )
    payments = debt.payments.select_related("paid_by").order_by("-created_at")
    return render(request, "debt_detail.html", {"debt": debt, "payments": payments})


@require_POST
@login_required(login_url="/user/login/")
def pay_debt(request, debt_id):
    try:
        debt = Debt.objects.get(pk=debt_id)
    except Debt.DoesNotExist:
        return JsonResponse({"status": "error", "msg": "Debt not found"}, status=404)

    amount_raw = request.POST.get("amount", "").strip()
    method = request.POST.get("method", "CASH").upper()
    note = request.POST.get("note", "")

    amount = safe_decimal(amount_raw)

    if amount <= Decimal("0.00"):
        return JsonResponse({"status": "error", "msg": "Amount must be > 0"}, status=400)

    try:
        payment_methods = dict(DebtPayment.PAYMENT_METHODS).keys()
    except Exception:
        payment_methods = ["CASH", "DEBIT/CREDIT", "EBT"]

    if method not in payment_methods and method not in ["CASH", "DEBIT/CREDIT", "EBT"]:
        method = "CASH"

    try:
        p = DebtPayment.objects.create(
            debt=debt,
            amount=amount,
            method=method,
            note=note,
            paid_by=request.user
        )

        debt.refresh_from_db()

        return JsonResponse({
            "status": "ok",
            "new_balance": str(getattr(debt, "balance", "0.00")),
            "debt_status": getattr(debt, "status", ""),
            "payment_id": p.pk,
            "paid_at": p.created_at.isoformat()
        })

    except Exception as e:
        print("pay_debt error:", e)
        traceback.print_exc()
        return JsonResponse({"status": "error", "msg": "Failed to record payment"}, status=500)


@login_required(login_url="/user/login/")
def debt_payment(request, debt_id):
    debt = get_object_or_404(Debt, pk=debt_id)

    if request.method == "POST":
        amount = safe_decimal(request.POST.get("amount", "0"))
        method = request.POST.get("method", "CASH").upper()
        note = request.POST.get("note", "")

        if amount <= Decimal("0.00"):
            messages.error(request, "Payment amount must be greater than 0.")
            return redirect("debt_payment", debt_id=debt_id)

        try:
            balance = safe_decimal(debt.balance)
        except Exception:
            balance = Decimal("0.00")

        if amount > balance:
            messages.error(request, "Payment exceeds remaining balance.")
            return redirect("debt_payment", debt_id=debt_id)

        try:
            DebtPayment.objects.create(
                debt=debt,
                amount=amount,
                method=method,
                note=note,
                paid_by=request.user
            )

            try:
                debt.paid_amount = safe_decimal(debt.paid_amount + amount)
            except Exception:
                pass

            try:
                debt.update_status()
            except Exception:
                debt.save()

            messages.success(request, "Payment recorded successfully.")

        except Exception as e:
            print("debt_payment POST error:", e)
            traceback.print_exc()
            messages.error(request, "Failed to record payment.")

        return redirect("debt_detail", debt_id=debt_id)

    return render(request, "debt_payment.html", {"debt": debt})


@login_required(login_url="/user/login/")
def debt_payments_history(request, debt_id):
    debt = get_object_or_404(Debt, pk=debt_id)
    payments = debt.payments.select_related("paid_by").order_by("-created_at")
    return render(request, "debt_payments_history.html", {"debt": debt, "payments": payments})


def qz_certificate(request):
    """
    Sends public QZ certificate to browser/QZ Tray.
    """
    try:
        with open(settings.QZ_CERT_PATH, "r", encoding="utf-8") as f:
            cert = f.read()

        return HttpResponse(cert, content_type="text/plain")

    except Exception as e:
        return HttpResponse(
            f"Certificate error: {e}",
            status=500,
            content_type="text/plain"
        )


@csrf_exempt
def qz_sign(request):
    """
    Signs QZ Tray request using private-key.pem.
    Uses pycryptodome to avoid cryptography/PyO3 Windows issue.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")

    data_to_sign = request.body

    if not data_to_sign:
        return HttpResponseBadRequest("No data to sign")

    try:
        with open(settings.QZ_PRIVATE_KEY_PATH, "rb") as key_file:
            private_key = RSA.import_key(key_file.read())

        digest = SHA256.new(data_to_sign)
        signature = pkcs1_15.new(private_key).sign(digest)

        return HttpResponse(
            base64.b64encode(signature).decode("utf-8"),
            content_type="text/plain"
        )

    except FileNotFoundError:
        return HttpResponse(
            "Private key not found. Put it at qz_keys/private-key.pem",
            status=500,
            content_type="text/plain"
        )

    except Exception as e:
        return HttpResponse(
            f"Signing error: {e}",
            status=500,
            content_type="text/plain"
        )
    
    