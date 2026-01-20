# transaction/views.py
from datetime import datetime, timedelta, timezone as py_timezone
import traceback
import pandas as pd
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from django.db.models import Sum, F, ExpressionWrapper, DecimalField
# add these imports
from .models import transaction, productTransaction, Expense, Debt, DebtPayment
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db import transaction as db_transaction
import hashlib
import json


from django.shortcuts import redirect, render
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.conf import settings
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.contrib.auth.decorators import login_required
from django import forms

from escpos.printer import Usb

from cart.models import Cart
from .forms import ExpenseForm
from .models import transaction, productTransaction, Expense

# make decimal context generous to avoid quantize surprises
getcontext().prec = 28


# -----------------------------
# Helpers
# -----------------------------
def currency_symbol():
    return getattr(settings, "CURRENCY_SYMBOL", "TZS")


def safe_decimal(value, default=Decimal("0.00")):
    """
    Convert value to Decimal safely. Accepts Decimal, int, float, str.
    Returns a Decimal quantized to 2 places, or default on error.
    """
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        try:
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return default
    try:
        # convert via str to avoid binary float pitfalls
        d = Decimal(str(value))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as e:
        print("safe_decimal conversion error:", e, "value:", value)
        return default


def fmt(amount, decimals=2):
    """
    Format a numeric amount with commas and currency symbol.
    Example: fmt(123456.5) -> "TZS 123,456.50"
    Always returns a string.
    """
    try:
        dec = safe_decimal(amount)
        formatted = f"{dec:,.{decimals}f}"
    except Exception:
        formatted = f"{Decimal('0.00'):,.{decimals}f}"
    return f"{currency_symbol()} {formatted}"


def fmt_no_sym(amount, decimals=2):
    """Return just the number with commas (no currency symbol)."""
    try:
        dec = safe_decimal(amount)
        return f"{dec:,.{decimals}f}"
    except Exception:
        return f"{Decimal('0.00'):,.{decimals}f}"


def sum_cart_field(cart, field_name):
    """
    Sum a numeric field from the session cart safely using Decimal arithmetic.
    cart is expected to be a dict mapping key -> {field_name: value, ...}
    """
    total = Decimal("0.00")
    if not cart or not isinstance(cart, dict):
        return total
    for val in cart.values():
        try:
            raw = val.get(field_name, 0)
            total += safe_decimal(raw)
        except Exception as e:
            print("sum_cart_field per-item error:", e, "val:", val)
            continue
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# -----------------------------
# Forms / Printer class
# -----------------------------
class DateSelector(forms.Form):
    start_date = forms.DateField(widget=forms.SelectDateWidget())
    end_date = forms.DateField(widget=forms.SelectDateWidget())


class printer:
    """
    Simple wrapper for escpos. Use settings.PRINTER_VENDOR_ID and PRINTER_PRODUCT_ID.
    Note: environment values may be strings like '0x1234' or integers; original code used eval().
    """
    printer = None

    @staticmethod
    def printReceipt(printText, times=0, *args, **kwargs):
        try:
            if printer.printer:
                printer.printer.text(printText)
                printer.printer.text(f"\nPrint Time: {datetime.now():%Y-%m-%d %H:%M}\n\n\n")
        except Exception as e:
            # try to reconnect and retry a few times
            try:
                printer.connectPrinter()
                if times < 3:
                    printer.printReceipt(printText, times + 1)
            except Exception as e2:
                print("printer.printReceipt: failed after reconnect:", e2)

    @staticmethod
    def connectPrinter():
        try:
            # keep previous behavior: allow PRINTER_VENDOR_ID to be expression (e.g., "0x04b8")
            vid = settings.PRINTER_VENDOR_ID
            pid = settings.PRINTER_PRODUCT_ID
            # If values were saved as strings that represent ints (e.g., "0x04b8"), eval may be used
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


# -----------------------------
# Views
# -----------------------------
def transactionReceipt(request, transNo):
    """
    Show receipt for a transaction. Defensive against DB decimal issues.
    """
    try:
        obj = transaction.objects.get(transaction_id=transNo)
        receipt = getattr(obj, "receipt", "")
        return render(request, 'receiptView.html', context={'receipt': receipt, 'transNo': transNo})
    except transaction.DoesNotExist:
        raise Http404("No Transactions Found!!!")
    except InvalidOperation as inv:
        # fallback to raw SQL cast approach if decimal conversion fails (rare)
        print("transactionReceipt: InvalidOperation in ORM get:", inv)
        traceback.print_exc()
        try:
            table = transaction._meta.db_table
            sql = "SELECT receipt FROM %s WHERE transaction_id = ? LIMIT 1" % table
            with connection.cursor() as cursor:
                cursor.execute(sql, [transNo])
                row = cursor.fetchone()
            if not row:
                raise Http404("No Transactions Found (raw)!!!")
            receipt = row[0]
            return render(request, 'receiptView.html', context={'receipt': receipt, 'transNo': transNo})
        except Exception as e:
            print("transactionReceipt raw fallback error:", e)
            traceback.print_exc()
            raise Http404("No Transactions Found!!!")


def transactionPrintReceipt(request, transNo):
    """
    Send stored receipt text to the physical printer (if available).
    """
    try:
        try:
            receipt = transaction.objects.get(transaction_id=transNo).receipt
        except InvalidOperation as inv:
            print("transactionPrintReceipt: InvalidOperation, raw fallback", inv)
            traceback.print_exc()
            table = transaction._meta.db_table
            sql = "SELECT receipt FROM %s WHERE transaction_id = ? LIMIT 1" % table
            with connection.cursor() as cursor:
                cursor.execute(sql, [transNo])
                row = cursor.fetchone()
            receipt = row[0] if row else ""
        if printer.printer is None:
            printer.connectPrinter()
            print("Connecting Printer")
        if printer.printer and receipt:
            printer.printReceipt(receipt)
        return redirect(f'/transaction_receipt/{transNo}/')
    except Exception as e:
        print("transactionPrintReceipt error:", e)
        traceback.print_exc()
        return redirect('register')

from datetime import datetime, time as dt_time, timezone as py_timezone
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone as dj_timezone

from .forms import DateSelector     # your DateSelector (see earlier recommendation: required=False recommended)
    # adjust if your model is named differently


def _fmt_amount(value):
    """Format numeric value for display (thousands separator, 2 decimals)."""
    try:
        return "{:,.2f}".format(float(value or 0))
    except Exception:
        return "0.00"


@login_required(login_url="/user/login/")
def transactionView(request):
    """
    Transaction list with date filter (GET).
    - User selects local dates -> convert to aware local datetimes -> convert to UTC for DB query.
    - Results' datetimes are converted back to local for display.
    """
    # local timezone (server/user)
    local_tz = dj_timezone.get_current_timezone()
    now_local = dj_timezone.localtime(dj_timezone.now(), local_tz)

    # Default local start/end for "today"
    default_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    default_end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Bind the form to GET so values persist in the querystring and fields stay populated
    form = DateSelector(request.GET or None)

    # Start with defaults
    start_local = default_start_local
    end_local = default_end_local
    start_date_val = None
    end_date_val = None

    if form.is_valid():
        # DateFields return date objects or None (if required=False)
        sd = form.cleaned_data.get("start_date")
        ed = form.cleaned_data.get("end_date")

        if sd:
            start_naive = datetime.combine(sd, dt_time.min)          # 00:00:00
            start_local = dj_timezone.make_aware(start_naive, local_tz)
            start_date_val = sd

        if ed:
            end_naive = datetime.combine(ed, dt_time.max)            # 23:59:59.999999
            end_local = dj_timezone.make_aware(end_naive, local_tz)
            end_date_val = ed

        # If user accidentally set end < start, swap them for safety (or you could return an error)
        if end_local < start_local:
            start_local, end_local = end_local, start_local
            start_date_val, end_date_val = end_date_val, start_date_val

    # Convert local-aware datetimes -> UTC (DB stored in UTC)
    start_utc = start_local.astimezone(py_timezone.utc)
    end_utc = end_local.astimezone(py_timezone.utc)

    # Query DB (make sure Transaction.transaction_dt is timezone-aware / stored as UTC)
    qs = transaction.objects.filter(transaction_dt__range=(start_utc, end_utc)).order_by("-transaction_dt")

    # Prepare list for template: convert dt back to local timezone for display
    transactions = []
    for t in qs:
        local_dt = dj_timezone.localtime(t.transaction_dt, local_tz)
        transactions.append({
            "transaction_id": t.transaction_id,
            "total_sale": _fmt_amount(t.total_sale),
            "payment_type": t.payment_type or "",
            "transaction_dt": local_dt,   # template can use |date filter or display directly
        })

    context = {
        "transactions": transactions,
        "form": form,
        # expose these so template can show badges and keep inputs populated
        "start_date": start_date_val,
        "end_date": end_date_val,
        "is_filtered": bool(start_date_val or end_date_val),
    }

    return render(request, "transactions.html", context)


@login_required(login_url="/user/login/")
def returnsTransaction(request):
    Cart(request).returns()
    return redirect('register')


@login_required(login_url="/user/login/")
def suspendTransaction(request):
    """
    Save current session cart under a timestamp key for later recall.
    """
    if Cart(request).isNotEmpty():
        key = datetime.now().strftime('%Y%m%d%H%M%S%f')
        if "Cart_Sessions" in request.session.keys():
            request.session["Cart_Sessions"][key] = request.session[settings.CART_SESSION_ID]
            request.session.modified = True
        else:
            request.session["Cart_Sessions"] = {}
            request.session["Cart_Sessions"][key] = request.session[settings.CART_SESSION_ID]
    return redirect("cart_clear")


@login_required(login_url="/user/login/")
def recallTransaction(request, recallTransNo=None):
    """
    Restore a suspended cart back to the session.
    """
    if Cart(request).isNotEmpty():
        return redirect("suspend_transaction")
    if recallTransNo:
        request.session[settings.CART_SESSION_ID] = request.session["Cart_Sessions"][recallTransNo]
        del request.session["Cart_Sessions"][recallTransNo]
        request.session.modified = True
    elif "Cart_Sessions" in request.session.keys() and len(request.session["Cart_Sessions"]):
        return render(request, "recallTransaction.html", context={"obj_rt": request.session["Cart_Sessions"].keys()})
    return redirect("register")


@login_required(login_url="/user/login/")
def endTransactionReceipt(request, transNo):
    """
    Show final receipt page after completing transaction (with cash/card details in a small table).
    """
    try:
        change = ""
        if request.GET.get("type") == "cash":
            # compute change properly and format TZS
            total = float(request.GET.get("total", 0))
            value = float(request.GET.get("value", 0))
            change_val = value - total
            change = f"""<table class="table text-white h3 p-0 m-0">
                            <tr>
                                <td class="text-left pl-5"> Total : </td>
                                <td class="text-right pr-5"> {fmt(total)} </td>
                            </tr>
                            <tr>
                                <td class="text-left pl-5"> Cash : </td>
                                <td class="text-right pr-5"> {fmt(value)} </td>
                            </tr>
                            <tr class="h1 badge-danger" >
                                <td style="padding-top:15px"> Change : </td>
                                <td style="padding-top:15px"> {fmt(change_val)} </td>
                            </tr>
                        </table>"""
        elif request.GET.get("type") == "card":
            total = float(request.GET.get("total", 0))
            value = request.GET.get("value", "")
            # For card, 'value' might be transaction reference or amount; if numeric, format it
            try:
                value_num = float(value)
                value_display = fmt(value_num)
            except Exception:
                value_display = str(value)
            change = f"""<table class="table text-white h3 p-0 m-0">
                            <tr>
                                <td class="text-left pl-5"> Total : </td>
                                <td class="text-right pr-5"> {fmt(total)} </td>
                            </tr>
                            <tr>
                                <td class="text-left pl-5"> Card : </td>
                                <td class="text-right pr-5"> {value_display}</td>
                            </tr>
                        </table>
                        <div class="h1 badge-danger p-3">
                             CARD TRANSACTION
                        </div>
                        """

        obj = transaction.objects.get(transaction_id=transNo)
        return render(request, 'endTransaction.html', context={'receipt': obj.receipt, 'change': change})
    except transaction.DoesNotExist:
        raise Http404("No Transactions Found!!!")
    except InvalidOperation as inv:
        # fallback: raw SQL select receipt (avoid Decimal conversion)
        print("endTransactionReceipt: InvalidOperation", inv)
        traceback.print_exc()
        try:
            table = transaction._meta.db_table
            sql = "SELECT receipt FROM %s WHERE transaction_id = ? LIMIT 1" % table
            with connection.cursor() as cursor:
                cursor.execute(sql, [transNo])
                row = cursor.fetchone()
            if not row:
                raise Http404("No Transactions Found!!!")
            receipt = row[0]
            return render(request, 'endTransaction.html', context={'receipt': receipt, 'change': change})
        except Exception as e:
            print("endTransactionReceipt raw fallback error:", e)
            traceback.print_exc()
            raise Http404("No Transactions Found!!!")
    except Exception as e:
        print("endTransactionReceipt unexpected error:", e)
        traceback.print_exc()
        raise Http404("No Transactions Found!!!")

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from urllib.parse import urlencode
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.conf import settings
import traceback

# Make sure these are available in this module (already present elsewhere in your file)
# from .views_helpers import safe_decimal, addTransaction, sum_cart_field
# from cart.models import Cart

@login_required(login_url="/user/login/")
def endTransaction(request, type, value):
    """
    Complete a sale. Prevent duplicate transactions by using a session-based
    fingerprint/pending guard. If a request with the same fingerprint is received
    while another request is processing (or very recently processed), the second
    request will return/redirect to the already-created transaction instead of
    creating a new one.
    """
    try:
        # Get session cart
        cart = request.session.get(settings.CART_SESSION_ID, {})
        if not cart:
            return redirect("register")

        # compute total safely from session cart (Decimal)
        total_dec = sum_cart_field(cart, "line_total")
        try:
            total_float = float(total_dec)
        except Exception:
            total_float = 0.0

        tx_type = str(type).lower()

        # Build payment_type label used for addTransaction and fingerprint
        if tx_type == "card":
            v = (value or "").upper()
            if v == "EBT":
                payment_label = "EBT"
            elif v == "DEBIT_CREDIT":
                payment_label = "DEBIT/CREDIT"
            else:
                payment_label = v if v else "DEBIT/CREDIT"
        elif tx_type == "cash":
            payment_label = "CASH"
        else:
            payment_label = str(type)

        # Build a stable fingerprint for this request: user + payment + total + cart contents
        try:
            cart_serial = json.dumps(cart, sort_keys=True, default=str)
        except Exception:
            # fallback to a simple string if cart isn't JSON-serializable
            cart_serial = str(cart)
        fp_src = f"{request.user.pk}|{payment_label}|{str(total_dec)}|{cart_serial}"
        fingerprint = hashlib.sha256(fp_src.encode("utf-8")).hexdigest()

        now = timezone.now()

        # If we processed this fingerprint recently, redirect to the existing receipt
        last_fp = request.session.get("last_tx_fingerprint")
        last_tx_id = request.session.get("last_tx_id")
        last_ts = request.session.get("last_tx_ts")
        if last_fp == fingerprint and last_tx_id and last_ts:
            try:
                last_dt = timezone.make_aware(datetime.fromisoformat(last_ts)) if isinstance(last_ts, str) else last_ts
            except Exception:
                last_dt = None
            if last_dt is None or (now - last_dt) <= timedelta(seconds=30):
                params = {"type": type, "value": str(value), "total": str(total_float)}
                qs = urlencode(params)
                return redirect(f"/endTransaction/{last_tx_id}/?{qs}")

        # If a pending transaction with same fingerprint exists (another request currently processing),
        # avoid creating a new one. Attempt to return existing last_tx_id if available.
        pending_fp = request.session.get("pending_tx_fingerprint")
        if pending_fp == fingerprint:
            # If first request already finished and recorded last_tx_id, redirect to it.
            if last_tx_id:
                params = {"type": type, "value": str(value), "total": str(total_float)}
                qs = urlencode(params)
                return redirect(f"/endTransaction/{last_tx_id}/?{qs}")
            # otherwise, avoid creating duplicate — redirect back to register as safe fallback
            return redirect("register")

        # Mark pending in session BEFORE calling addTransaction to stop quick duplicates
        request.session["pending_tx_fingerprint"] = fingerprint
        request.session["pending_tx_ts"] = now.isoformat()
        request.session.modified = True

        return_transaction = None

        # Proceed with normal transaction creation logic
        if tx_type == "card":
            v = (value or "").upper()
            if v == "EBT":
                return_transaction = addTransaction(
                    user=request.user,
                    payment_type="EBT",
                    total=total_dec,
                    cart=cart,
                    value=total_dec
                )
            elif v == "DEBIT_CREDIT":
                return_transaction = addTransaction(
                    user=request.user,
                    payment_type="DEBIT/CREDIT",
                    total=total_dec,
                    cart=cart,
                    value=total_dec
                )
            else:
                return_transaction = addTransaction(
                    user=request.user,
                    payment_type=v if v else "DEBIT/CREDIT",
                    total=total_dec,
                    cart=cart,
                    value=total_dec
                )

        elif tx_type == "cash":
            # 'value' should be the tendered amount (numeric). Use Decimal safely.
            try:
                value_dec = Decimal(str(value))
            except Exception:
                value_dec = Decimal("0.00")

            try:
                value_dec = value_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except Exception:
                value_dec = Decimal("0.00")

            if value_dec >= total_dec:
                return_transaction = addTransaction(
                    user=request.user,
                    payment_type="CASH",
                    total=total_dec,
                    cart=cart,
                    value=value_dec,
                    paid_amount=value_dec
                )
            else:
                # Tendered amount insufficient — clear pending flag and abort
                try:
                    del request.session["pending_tx_fingerprint"]
                    del request.session["pending_tx_ts"]
                    request.session.modified = True
                except Exception:
                    pass
                return redirect("register")

        # If transaction succeeded, record fingerprint as last and clear pending
        if return_transaction:
            # record last transaction in session for short-term idempotency
            try:
                request.session["last_tx_fingerprint"] = fingerprint
                request.session["last_tx_id"] = return_transaction.transaction_id
                request.session["last_tx_ts"] = now.isoformat()
                # remove pending marker
                request.session.pop("pending_tx_fingerprint", None)
                request.session.pop("pending_tx_ts", None)
                request.session.modified = True
            except Exception:
                pass

            # clear cart then redirect to receipt
            try:
                Cart(request).clear()
            except Exception:
                print("Warning: Cart clear failed after saving transaction")

            params = {
                "type": type,
                "value": str(value),
                "total": str(total_float)
            }
            qs = urlencode(params)
            return redirect(f"/endTransaction/{return_transaction.transaction_id}/?{qs}")

        # If addTransaction returned None (failed), clear pending marker
        try:
            request.session.pop("pending_tx_fingerprint", None)
            request.session.pop("pending_tx_ts", None)
            request.session.modified = True
        except Exception:
            pass

        # fallback (nothing created)
        return redirect("register")

    except Exception as e:
        # Ensure pending marker is cleared on unexpected errors
        try:
            request.session.pop("pending_tx_fingerprint", None)
            request.session.pop("pending_tx_ts", None)
            request.session.modified = True
        except Exception:
            pass

        print("endTransaction error:", e, type, value, getattr(request, "user", None))
        traceback.print_exc()
        return redirect("register")



from django.conf import settings
from decimal import Decimal
# at top of file ensure these imports exist
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime
import traceback
import pandas as pd
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Sum
# models
from .models import transaction, productTransaction, Expense, Debt, DebtPayment
# helpers already in your file: safe_decimal, fmt_no_sym, fmt
# paste into a Django module where Transaction, Debt, DebtPayment are available
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime
import pandas as pd
import traceback

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Sum

# Ensure you have these model names imported in this module:



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
    Creates and saves a transaction.
    - total: authoritative Decimal or None to compute from cart.
    - For DEBT: create Debt and initial DebtPayment record (if paid_amount > 0).
    - phone_number is optional and will be added to Debt if the field exists.
    NOTE: This function expects your Django models to be available in the module:
          - `transaction` (Transaction model class)
          - `Debt` (Debt model class)
          - `DebtPayment` (DebtPayment model class)
    """

    VAT_RATE = Decimal("18")
    transaction_id = datetime.now().strftime('%Y%m%d%H%M%S%f')

    # Build cart_df defensively
    try:
        if cart is None:
            cart_df = pd.DataFrame()
        else:
            # Accept both dict-of-items and list-of-dicts
            if isinstance(cart, dict):
                # if values are dict-like
                cart_df = pd.DataFrame(list(cart.values())).reset_index(drop=True)
            else:
                cart_df = pd.DataFrame(cart).reset_index(drop=True)
    except Exception as e:
        print("addTransaction: building cart_df fallback:", e)
        try:
            cart_df = pd.DataFrame(list(cart.values())) if cart is not None else pd.DataFrame()
        except Exception:
            cart_df = pd.DataFrame()

    # Normalize numeric columns
    for col in ["tax_value", "deposit_value", "price", "quantity", "line_total", "tax_percentage"]:
        if col in cart_df.columns:
            cart_df[col] = pd.to_numeric(cart_df[col], errors="coerce").fillna(0)

    if not cart_df.empty:
        cart_df.index = cart_df.index + 1

    total_lines_sum = Decimal("0.00")
    tax_total = Decimal("0.00")
    enhanced_rows = []

    if not cart_df.empty:
        for _, row in cart_df.iterrows():
            name = str(row.get("name", "")).strip()
            try:
                qty = int(row.get("quantity", 0))
            except Exception:
                qty = 0
            price = safe_decimal(row.get("price", 0))

            if "line_total" in row and row.get("line_total", None) not in (None, ""):
                line_total = safe_decimal(row.get("line_total", 0))
            else:
                line_total = (price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if "tax_percentage" in cart_df.columns:
                tax_pct = safe_decimal(row.get("tax_percentage", 0))
            else:
                tax_pct = Decimal("18") if safe_decimal(row.get("tax_value", 0)) > 0 else Decimal("0")

            if tax_pct > 0:
                denom = (Decimal("100") + tax_pct)
                try:
                    raw_line_vat = (line_total * tax_pct) / denom
                except Exception:
                    raw_line_vat = Decimal("0.00")
                line_vat = raw_line_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                line_vat = Decimal("0.00")

            tax_total += line_vat
            total_lines_sum += line_total

            enhanced_rows.append({
                "name": name,
                "qty": qty,
                "price": price,
                "amount": line_total,
                "tax_pct": tax_pct,
                "line_vat": line_vat,
            })

    INCLUDE_DEPOSIT_IN_TOTAL = getattr(settings, "INCLUDE_DEPOSIT_IN_TOTAL", False)
    if INCLUDE_DEPOSIT_IN_TOTAL and "deposit_value" in cart_df.columns:
        try:
            deposit_total = safe_decimal(cart_df["deposit_value"].sum())
        except Exception:
            deposit_total = Decimal("0.00")
    else:
        deposit_total = Decimal("0.00")

    if total is not None:
        total_dec = safe_decimal(total)
    else:
        total_dec = (total_lines_sum + deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    tax_total = tax_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    merchant_sub_total = (total_dec - tax_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if merchant_sub_total < Decimal("0.00"):
        merchant_sub_total = Decimal("0.00")

    # Build receipt (simple)
    rows = ["DESCRIPTION", "QTY   PRICE     AMOUNT"]
    receipt_width = int(getattr(settings, "RECEIPT_CHAR_COUNT", 40))
    rows.append("-" * receipt_width)
    for r in enhanced_rows:
        name = r["name"][:receipt_width - 2] if r.get("name") else ""
        rows.append(name)
        rows.append(f"{r['qty']} @ {fmt_no_sym(r['price'])} = {fmt_no_sym(r['amount'])}")
        rows.append("")
    separator = "-" * receipt_width
    cart_string = f"Transaction:{transaction_id}\n{separator}\n" + "\n".join(rows)

    # Totals block string building (kept minimal)
    total_lines = [
        separator,
        f"Sub Total      {fmt_no_sym(merchant_sub_total)}",
        f"Tax            {fmt_no_sym(tax_total)}",
        f"Total Amount   {fmt_no_sym(total_dec)}",
        ""
    ]

    total_string = "\n".join(total_lines)
    header = getattr(settings, "RECEIPT_HEADER", "")
    # parse datetime from transaction_id without microseconds
    try:
        transaction_dt = datetime.strptime(transaction_id[:-6], '%Y%m%d%H%M%S')
        sale_datetime_str = transaction_dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        sale_datetime_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    username_str = getattr(user, "username", "Unknown")
    footer_lines = [
        getattr(settings, "RECEIPT_FOOTER", "You are Welcomed !"),
        f"Sale Date: {sale_datetime_str}",
        f"Served by: {username_str}",
        ""
    ]
    footer = "\n".join(footer_lines)
    receipt_raw = header + "\n\n" + cart_string + "\n" + total_string + "\n\n" + footer
    receipt = "\n".join([i.center(receipt_width) for i in receipt_raw.splitlines()])

    # Parse debt_due_date
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

    # Normalize phone
    phone_clean = None
    if phone_number:
        try:
            phone_clean = str(phone_number).strip()[:20]
        except Exception:
            phone_clean = None

    # Save transaction + Debt inside atomic block
    try:
        with db_transaction.atomic():
            header_kwargs = dict(
                transaction_id=transaction_id,
                transaction_dt=datetime.strptime(transaction_id[:-6], '%Y%m%d%H%M%S'),
                user=user,
                total_sale=total_dec,
                sub_total=merchant_sub_total,
                tax_total=tax_total,
                deposit_total=deposit_total,
                payment_type=payment_type,
                receipt=receipt,
                products=str(cart_df.to_dict('records')),
                debtor_name=(debtor_name or "")[:200],
                debt_due_date=due_date_obj,
            )

            # Filter to actual transaction model fields (if `transaction` model variable exists)
            try:
                tx_fields = {f.name for f in transaction._meta.get_fields() if getattr(f, "concrete", True)}
                header_kwargs = {k: v for k, v in header_kwargs.items() if k in tx_fields}
            except Exception:
                # if transaction model not present or introspection fails, keep header_kwargs as-is
                pass

            obj = transaction.objects.create(**header_kwargs)

            # DEBT creation
            if str(payment_type).strip().upper() == "DEBT":
                candidate_kwargs = {
                    "transaction": obj,
                    "debtor_name": (debtor_name or "")[:200],
                    "total_amount": total_dec,
                    "paid_amount": Decimal("0.00"),
                    "due_date": due_date_obj,
                    "phone_number": phone_clean or "",
                    "created_by": user,
                }

                # Determine allowed Debt fields (defensive)
                try:
                    allowed_debt_fields = {f.name for f in Debt._meta.get_fields() if getattr(f, "concrete", True)}
                except Exception:
                    allowed_debt_fields = set(candidate_kwargs.keys())

                debt_create_kwargs = {k: v for k, v in candidate_kwargs.items() if k in allowed_debt_fields}
                dropped = [k for k in candidate_kwargs.keys() if k not in debt_create_kwargs]
                if dropped:
                    print("addTransaction: dropping unexpected Debt fields:", dropped)

                # Create or get existing Debt
                try:
                    debt, created = Debt.objects.get_or_create(
                        transaction=obj,  # ensure this is the same transaction object
                        defaults=debt_create_kwargs
                    )

                    if not created:
                        # Optional: update fields if debt already exists
                        for field, value in debt_create_kwargs.items():
                            setattr(debt, field, value)
                        debt.save()
                        print(f"addTransaction: debt already existed for transaction {getattr(obj, 'id', transaction_id)}, updated fields")
                    else:
                        print(f"addTransaction: debt created for transaction {getattr(obj, 'id', transaction_id)}")

                except TypeError as te:
                    print("addTransaction: Debt.get_or_create TypeError:", te)
                    minimal_kw = {}
                    if "transaction" in allowed_debt_fields:
                        minimal_kw["transaction"] = obj
                    if "total_amount" in allowed_debt_fields:
                        minimal_kw["total_amount"] = total_dec
                    debt, _ = Debt.objects.get_or_create(
                        transaction=obj,
                        defaults=minimal_kw
                    )

                # ===== Initial payment logic =====
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

                # recompute paid_amount from payments
                try:
                    payments_sum = DebtPayment.objects.filter(debt=debt).aggregate(total=Sum('amount'))["total"] or Decimal("0.00")
                    payments_sum = safe_decimal(payments_sum)
                    if "paid_amount" in allowed_debt_fields:
                        debt.paid_amount = payments_sum
                    try:
                        # Some Debt models may have update_status helper
                        debt.update_status()
                    except Exception:
                        debt.save()
                except Exception as e:
                    print("addTransaction: failed to recompute debt.paid_amount:", e)
                    try:
                        debt.save()
                    except Exception:
                        pass

        # End atomic
        print("Saved transaction:", getattr(obj, "transaction_id", transaction_id))
        return obj

    except Exception as e:
        print("addTransaction: Failed to save transaction:", e)
        traceback.print_exc()
        return None



# -----------------------------
# Expenses / Reports
# -----------------------------
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
    qs = Expense.objects.all()
    return render(request, "transaction/expenses_list.html", {"expenses": qs})


@login_required(login_url="/user/login/")
def profit_loss(request):
    """
    Profit & loss between two dates (inclusive).
    """
    # date range handling
    now = timezone.now()
    start_str = request.GET.get("start_date", "")
    end_str = request.GET.get("end_date", "")
    try:
        if start_str:
            start_date = timezone.make_aware(datetime.strptime(start_str, "%Y-%m-%d"))
        else:
            start_date = timezone.make_aware(datetime(now.year, now.month, 1))
    except Exception:
        start_date = timezone.make_aware(datetime(now.year, now.month, 1))

    try:
        if end_str:
            end_date = timezone.make_aware(datetime.strptime(end_str, "%Y-%m-%d")) + timedelta(days=1)
        else:
            end_date = now + timedelta(seconds=1)
    except Exception:
        end_date = now + timedelta(seconds=1)

    # Revenue
    revenue_agg = transaction.objects.filter(
        transaction_dt__gte=start_date,
        transaction_dt__lt=end_date
    ).aggregate(total_revenue=Sum("total_sale"))
    total_revenue = revenue_agg["total_revenue"] or 0

    # Taxes
    tax_agg = transaction.objects.filter(
        transaction_dt__gte=start_date,
        transaction_dt__lt=end_date
    ).aggregate(total_tax=Sum("tax_total"))
    total_tax = tax_agg["total_tax"] or 0

    # COGS from productTransaction (cost_price * qty)
    expr = ExpressionWrapper(F("cost_price") * F("qty"), output_field=DecimalField(max_digits=20, decimal_places=2))
    cogs_agg = productTransaction.objects.filter(
        transaction_date_time__gte=start_date,
        transaction_date_time__lt=end_date
    ).aggregate(total_cogs=Sum(expr))
    total_cogs = cogs_agg["total_cogs"] or 0

    # Expenses
    expenses_agg = Expense.objects.filter(
        created_at__gte=start_date,
        created_at__lt=end_date
    ).aggregate(total_expenses=Sum("amount"))
    total_expenses = expenses_agg["total_expenses"] or 0

    # calculate (ensure Decimal)
    total_revenue = Decimal(total_revenue)
    total_cogs = Decimal(total_cogs)
    total_tax = Decimal(total_tax)
    total_expenses = Decimal(total_expenses)

    gross_profit = total_revenue - total_cogs
    net_profit = gross_profit - total_expenses - total_tax

    context = {
        "start_date": start_date.date(),
        "end_date": (end_date - timedelta(days=1)).date(),
        "total_revenue": total_revenue,
        "total_cogs": total_cogs,
        "total_tax": total_tax,
        "total_expenses": total_expenses,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
    }
    return render(request, "transaction/profit_loss.html", context)


from django.http import HttpResponseBadRequest
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

@login_required(login_url="/user/login/")
def endDebtTransaction(request):
    """
    Complete a sale that is recorded as DEBT (credit).
    POST params expected:
      - paid_amount (optional)
      - debtor_name (optional)
      - due_date (YYYY-MM-DD) optional
      - phone_number (optional)   <-- NEW
    Adds a short idempotency check to avoid duplicate transactions on double-submit.
    """
    try:
        if request.method != "POST":
            return HttpResponseBadRequest("POST required")

        cart = request.session.get(settings.CART_SESSION_ID, {})
        if not cart:
            return redirect("register")

        # compute totals
        total_dec = sum_cart_field(cart, "line_total")

        # parse incoming debt fields
        paid_amount_raw = request.POST.get("paid_amount", "0")
        debtor_name = (request.POST.get("debtor_name", "") or "").strip()
        due_date_raw = (request.POST.get("due_date", "") or "").strip()
        phone_number = (request.POST.get("phone_number", "") or "").strip()

        # normalize numeric values
        try:
            paid_amount = Decimal(str(paid_amount_raw))
        except Exception:
            paid_amount = Decimal("0.00")
        paid_amount = safe_decimal(paid_amount)

        # SIMPLE IDEMPOTENCY: if a transaction for this user with same total was created
        # moments ago, return that instead of creating a duplicate.
        try:
            now = timezone.now()
            window_start = now - timedelta(seconds=6)  # 6s window
            existing_tx = transaction.objects.filter(
                user=request.user,
                total_sale=total_dec,
                transaction_dt__gte=window_start
            ).order_by('-transaction_dt').first()
        except Exception:
            existing_tx = None

        if existing_tx:
            # Clear cart (best-effort) and redirect to existing receipt
            try:
                Cart(request).clear()
            except Exception:
                pass
            return redirect(f"/endTransaction/{existing_tx.transaction_id}/?type=debt&value={paid_amount}&total={float(total_dec)}")

        # Build the transaction object via addTransaction helper; pass phone_number
        return_transaction = addTransaction(
            user=request.user,
            payment_type="DEBT",
            total=total_dec,
            cart=cart,
            value=str(paid_amount),  # used for receipt display
            paid_amount=paid_amount,
            debtor_name=debtor_name,
            debt_due_date=(due_date_raw or None),
            phone_number=phone_number   # <-- important: now passed into addTransaction
        )

        if return_transaction:
            try:
                Cart(request).clear()
            except Exception:
                pass
            return redirect(f"/endTransaction/{return_transaction.transaction_id}/?type=debt&value={paid_amount}&total={float(total_dec)}")

        return redirect("register")

    except Exception as e:
        print("endDebtTransaction error:", e)
        traceback.print_exc()
        return redirect("register")



# views.py (only the debts_list view shown / replace the existing one)
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.safestring import mark_safe
from django.db.models import Q
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from urllib.parse import urlencode
import json

from .models import Debt

@login_required(login_url="/user/login/")
def debts_list(request):
    """
    Server-side searchable, paginated debts list.
    GET params:
      - q: search query (debtor name or phone)
      - page: pagination
    """
    qs = Debt.objects.select_related('transaction', 'created_by').order_by('-created_at')

    # server-side search term
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(debtor_name__icontains=q) |
            Q(phone_number__icontains=q)
        )

    # paginate (25 per page)
    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page", 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # minimal JSON payload for client-side use if needed
    debts_json = json.dumps(
        list(page_obj.object_list.values("id", "debtor_name", "phone_number", "total_amount")),
        cls=DjangoJSONEncoder
    )

    # preserve other GET params on pagination links (except page)
    get_copy = request.GET.copy()
    if 'page' in get_copy:
        del get_copy['page']
    extra_qs = ''
    if get_copy:
        extra_qs = '&' + urlencode(get_copy)

    return render(request, "debts_list.html", {
        "debts": page_obj.object_list,   # rows for current page
        "debts_json": mark_safe(debts_json),
        "page_obj": page_obj,
        "paginator": paginator,
        "query": q,
        "extra_qs": extra_qs,
    })



@login_required(login_url="/user/login/")
def debt_detail(request, debt_id):
    """
    Show debt detail including payments history.
    """
    try:
        debt = Debt.objects.select_related('transaction', 'created_by').get(pk=debt_id)
    except Debt.DoesNotExist:
        raise Http404("Debt not found")
    payments = debt.payments.select_related('paid_by').order_by("-created_at")
    return render(request, "debt_detail.html", {"debt": debt, "payments": payments})


@require_POST
@login_required(login_url="/user/login/")
def pay_debt(request, debt_id):
    """
    AJAX endpoint to record a payment for a debt.
    POST params:
      - amount (required)
      - method (one of CASH, DEBIT/CREDIT, EBT) optional default CASH
      - note (optional)
    Returns JSON: {'status':'ok','new_balance': '123.45','debt_status':'PARTIAL'} or error
    """
    try:
        debt = Debt.objects.get(pk=debt_id)
    except Debt.DoesNotExist:
        return JsonResponse({"status": "error", "msg": "Debt not found"}, status=404)

    amount_raw = request.POST.get("amount", "").strip()
    method = request.POST.get("method", "CASH").upper()
    note = request.POST.get("note", "")

    try:
        amount = safe_decimal(Decimal(str(amount_raw)))
    except Exception:
        return JsonResponse({"status": "error", "msg": "Invalid amount"}, status=400)
    if amount <= Decimal("0.00"):
        return JsonResponse({"status": "error", "msg": "Amount must be > 0"}, status=400)

    if method not in dict(DebtPayment.PAYMENT_METHODS).keys() and method not in ["CASH", "DEBIT/CREDIT", "EBT"]:
        # allow strings, but default to CASH if unknown
        method = "CASH"

    # create payment (DebtPayment.save will update Debt.paid_amount and status)
    try:
        p = DebtPayment.objects.create(
            debt=debt,
            amount=amount,
            method=method,
            note=note,
            paid_by=request.user
        )
        # reload debt for updated balance
        debt.refresh_from_db()
        return JsonResponse({
            "status": "ok",
            "new_balance": str(debt.balance),
            "debt_status": debt.status,
            "payment_id": p.pk,
            "paid_at": p.created_at.isoformat()
        })
    except Exception as e:
        print("pay_debt error:", e)
        traceback.print_exc()
        return JsonResponse({"status": "error", "msg": "Failed to record payment"}, status=500)


from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from decimal import Decimal

# make sure Debt and DebtPayment are imported (you already used them)
# from .models import Debt, DebtPayment
# and safe_decimal is already defined in this file

@login_required(login_url="/user/login/")
def debt_payment(request, debt_id):
    """
    Render a small page/form to accept a payment for a debt (non-AJAX).
    POST -> records DebtPayment and updates Debt balance/status, then redirects to debt_detail.
    """
    debt = get_object_or_404(Debt, pk=debt_id)

    if request.method == "POST":
        try:
            amount = safe_decimal(request.POST.get("amount", "0"))
        except Exception:
            amount = Decimal("0.00")
        method = request.POST.get("method", "CASH").upper()
        note = request.POST.get("note", "")

        if amount <= Decimal("0.00"):
            messages.error(request, "Payment amount must be greater than 0.")
            return redirect("debt_payment", debt_id=debt_id)

        # if you want to allow overpayment, adjust here (this disallows overpay)
        if amount > debt.balance:
            messages.error(request, "Payment exceeds remaining balance.")
            return redirect("debt_payment", debt_id=debt_id)

        # Create DebtPayment and update Debt totals safely
        try:
            p = DebtPayment.objects.create(
                debt=debt,
                amount=amount,
                method=method,
                note=note,
                paid_by=request.user
            )
            # update debt paid_amount and status
            debt.paid_amount = safe_decimal(debt.paid_amount + amount)
            # if your Debt model has update_status() method, call it:
            try:
                debt.update_status()
            except Exception:
                debt.save()
            messages.success(request, "Payment recorded successfully.")
        except Exception as e:
            print("debt_payment POST error:", e)
            messages.error(request, "Failed to record payment.")
        return redirect("debt_detail", debt_id=debt_id)

    # GET -> render form
    return render(request, "debt_payment.html", {"debt": debt})


@login_required(login_url="/user/login/")
def debt_payments_history(request, debt_id):
    """
    Show list of payments for a debt (simple page).
    """
    debt = get_object_or_404(Debt, pk=debt_id)
    payments = debt.payments.select_related('paid_by').order_by("-created_at")
    return render(request, "debt_payments_history.html", {"debt": debt, "payments": payments})
