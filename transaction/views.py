# transaction/views.py

from datetime import datetime, timedelta, time as dt_time, timezone as py_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from urllib.parse import urlencode
import traceback
import hashlib
import json

import pandas as pd

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
from .models import transaction, productTransaction, Expense, Debt, DebtPayment

getcontext().prec = 28


# ============================================================
# Helpers
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


def get_cart_from_session(request):
    """
    Get cart from session safely.
    """
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
    """
    Convert session cart to list of item dicts.
    Supports dict cart and list cart.
    """
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
    Robust total calculator.
    Fixes issue where transaction returns to barcode/register
    because total becomes 0 due to wrong cart key.
    """
    total = Decimal("0.00")

    for item in get_cart_items(cart):
        try:
            total += get_item_line_total(item)
        except Exception as e:
            print("sum_cart_field error:", e, "item:", item)

    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    Receipt layout like your first image:
    product name line,
    qty @ price = amount line,
    totals aligned,
    footer not cut.
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

    header = getattr(settings, "RECEIPT_HEADER", "")
    if header:
        for line in header.splitlines():
            clean = str(line).strip()
            lines.append(clean.center(receipt_width) if clean else "")
    else:
        lines.append("ADAMS MINI SUPERMARKET".center(receipt_width))
        lines.append("PO BOX 942 MOSHI".center(receipt_width))
        lines.append("J.K. Nyerere Street".center(receipt_width))
        lines.append("+255744844699".center(receipt_width))
        lines.append("adamssupermarket@gmail.com".center(receipt_width))
        lines.append("")
        lines.append("*** Sales Receipt ***".center(receipt_width))
        lines.append("TIN: 102-188-357".center(receipt_width))
        lines.append("*** NON-FISCAL RECEIPT ***".center(receipt_width))

    lines.append("")
    lines.append(f"Receipt No: {transaction_id}")
    lines.append("")
    lines.append("DESCRIPTION".center(receipt_width))
    lines.append("QTY   PRICE      AMOUNT".center(receipt_width))
    lines.append("")

    for row in enhanced_rows:
        for name_line in split_product_name(row["name"], receipt_width):
            lines.append(name_line)

        qty_text = format_qty(row["qty"])
        price_text = fmt_no_sym(row["price"])
        amount_text = fmt_no_sym(row["amount"])

        lines.append(f"{qty_text} @ {price_text} = {amount_text}")
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

    # Space so cutter does not cut the footer text
    lines.append("")
    lines.append("")
    lines.append("")
    lines.append("")
    lines.append("")
    lines.append("")

    return "\n".join(lines), transaction_dt_obj, merchant_sub_total


# ============================================================
# Forms / Printer
# ============================================================

class DateSelector(forms.Form):
    start_date = forms.DateField(widget=forms.SelectDateWidget(), required=False)
    end_date = forms.DateField(widget=forms.SelectDateWidget(), required=False)


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

def _fmt_amount(value):
    try:
        return "{:,.2f}".format(float(value or 0))
    except Exception:
        return "0.00"


@login_required(login_url="/user/login/")
def transactionView(request):
    local_tz = dj_timezone.get_current_timezone()
    now_local = dj_timezone.localtime(dj_timezone.now(), local_tz)

    default_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    default_end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)

    form = DateSelector(request.GET or None)

    start_local = default_start_local
    end_local = default_end_local
    start_date_val = None
    end_date_val = None

    if form.is_valid():
        sd = form.cleaned_data.get("start_date")
        ed = form.cleaned_data.get("end_date")

        if sd:
            start_naive = datetime.combine(sd, dt_time.min)
            start_local = dj_timezone.make_aware(start_naive, local_tz)
            start_date_val = sd

        if ed:
            end_naive = datetime.combine(ed, dt_time.max)
            end_local = dj_timezone.make_aware(end_naive, local_tz)
            end_date_val = ed

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
        "form": form,
        "start_date": start_date_val,
        "end_date": end_date_val,
        "is_filtered": bool(start_date_val or end_date_val),
    })


# ============================================================
# Cart Transaction Actions
# ============================================================

@login_required(login_url="/user/login/")
def returnsTransaction(request):
    Cart(request).returns()
    return redirect("register")


@login_required(login_url="/user/login/")
def suspendTransaction(request):
    if Cart(request).isNotEmpty():
        key = datetime.now().strftime("%Y%m%d%H%M%S%f")
        cart_key = getattr(settings, "CART_SESSION_ID", "cart")

        if "Cart_Sessions" not in request.session:
            request.session["Cart_Sessions"] = {}

        request.session["Cart_Sessions"][key] = request.session.get(cart_key, {})
        request.session.modified = True

    return redirect("cart_clear")


@login_required(login_url="/user/login/")
def recallTransaction(request, recallTransNo=None):
    cart_key = getattr(settings, "CART_SESSION_ID", "cart")

    if Cart(request).isNotEmpty():
        return redirect("suspend_transaction")

    if recallTransNo:
        request.session[cart_key] = request.session["Cart_Sessions"][recallTransNo]
        del request.session["Cart_Sessions"][recallTransNo]
        request.session.modified = True

    elif "Cart_Sessions" in request.session and len(request.session["Cart_Sessions"]):
        return render(request, "recallTransaction.html", {
            "obj_rt": request.session["Cart_Sessions"].keys()
        })

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
    FIXED:
    - No fingerprint blocking.
    - No pending session loop.
    - Robust cart total.
    - Cash/EBT/Card all complete sale.
    """
    try:
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
            # Fallback for URLs that pass EBT/CASH directly as type
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


def addTransaction(user,
                   payment_type,
                   total=None,
                   cart=None,
                   value=None,
                   paid_amount=Decimal("0.00"),
                   debtor_name=None,
                   debt_due_date=None,
                   phone_number=None):
    """
    Create transaction + receipt.
    """
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

    total_revenue = revenue_agg["total_revenue"] or Decimal("0.00")

    tax_agg = transaction.objects.filter(
        transaction_dt__gte=start_date,
        transaction_dt__lt=end_date
    ).aggregate(total_tax=Sum("tax_total"))

    total_tax = tax_agg["total_tax"] or Decimal("0.00")

    expr = ExpressionWrapper(
        F("cost_price") * F("qty"),
        output_field=DecimalField(max_digits=20, decimal_places=2)
    )

    cogs_agg = productTransaction.objects.filter(
        transaction_date_time__gte=start_date,
        transaction_date_time__lt=end_date
    ).aggregate(total_cogs=Sum(expr))

    total_cogs = cogs_agg["total_cogs"] or Decimal("0.00")

    expenses_agg = Expense.objects.filter(
        created_at__gte=start_date,
        created_at__lt=end_date
    ).aggregate(total_expenses=Sum("amount"))

    total_expenses = expenses_agg["total_expenses"] or Decimal("0.00")

    total_revenue = safe_decimal(total_revenue)
    total_cogs = safe_decimal(total_cogs)
    total_tax = safe_decimal(total_tax)
    total_expenses = safe_decimal(total_expenses)

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