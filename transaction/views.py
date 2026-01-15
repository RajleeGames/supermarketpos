# transaction/views.py
from datetime import datetime, timedelta, timezone as py_timezone
import traceback
import pandas as pd
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from django.db.models import Sum, F, ExpressionWrapper, DecimalField

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


@login_required(login_url="/user/login/")
def endTransaction(request, type, value):
    """
    Called when completing a sale. Reads session cart, validates payment, calls addTransaction(),
    clears cart and redirects to the endTransaction receipt page.
    """
    try:
        return_transaction = None
        cart = request.session.get(settings.CART_SESSION_ID, {})
        # compute total safely from session cart
        total_dec = sum_cart_field(cart, "line_total")
        total = float(total_dec)

        if type == "card":
            # Card Transaction
            if value == "EBT":
                return_transaction = addTransaction(request.user, "EBT", total, cart, total)
            elif value == "DEBIT_CREDIT":
                return_transaction = addTransaction(request.user, "DEBIT/CREDIT", total, cart, total)
        elif type == "cash":
            # 'value' should be numeric tendered amount for cash
            try:
                value_num = float(value)
            except Exception:
                value_num = 0.0
            value_num = round(value_num, 2)
            if value_num >= total:
                return_transaction = addTransaction(request.user, "CASH", total, cart, value_num)
        if return_transaction:
            Cart(request).clear()
            # ensure passing numeric values in query string are stringified safely
            return redirect(f"/endTransaction/{return_transaction.transaction_id}/?type={type}&value={value}&total={total}")
        return redirect("register")
    except Exception as e:
        print("endTransaction error:", e, type, value, getattr(request, 'user', None))
        traceback.print_exc()
        return redirect("register")
def addTransaction(user, payment_type, total, cart, value):
    """
    Creates receipt text with TZS formatting and saves the transaction.

    Behavior:
      - VAT is EXTRACTED per-line (not added).
      - Per-line VAT formula (VAT included in price):
            line_vat = line_total * tax_pct / (100 + tax_pct)
        Example for 18%: line_vat = line_total * 18 / 118
      - Each line_vat is quantized to 2 decimals then summed to avoid rounding mismatches.
      - Receipt (customer-facing) shows:
            Sub Total  = total_amount (what customer pays)
            Tax        = tax_total (extracted)
            Total Amt  = total_amount
      - DB sub_total saved = total_amount - tax_total (merchant/internal)
    """
    # VAT constants (you can move these to module top)
    VAT_RATE = Decimal("18")
    VAT_DIVISOR = Decimal("118")
    VAT_PERCENT = Decimal("18")  # used if you prefer percent math (100 + tax_pct)

    transaction_id = datetime.now().strftime('%Y%m%d%H%M%S%f')

    # Build cart_df safely (fallback if structure differs)
    try:
        cart_df = pd.DataFrame(cart).T.reset_index(drop=True)
    except Exception as e:
        print("addTransaction: building cart_df fallback:", e)
        cart_df = pd.DataFrame(list(cart.values()))

    # Ensure numeric columns
    for col in ["tax_value", "deposit_value", "price", "quantity", "line_total", "tax_percentage"]:
        if col in cart_df.columns:
            cart_df[col] = pd.to_numeric(cart_df[col], errors="coerce").fillna(0)

    # Reindex for nicer receipt numbering (optional)
    cart_df.index = cart_df.index + 1

    # ---------- Compute per-line amounts and per-line VAT ----------
    total_lines_sum = Decimal("0.00")   # sum of all line totals
    tax_total = Decimal("0.00")         # sum of per-line VAT (quantized)
    taxable_total = Decimal("0.00")     # sum of taxable line totals (unnecessary but useful)
    non_taxable_total = Decimal("0.00")

    # We'll build enhanced rows so we can print line VAT if needed
    enhanced_rows = []  # list of dicts: {name, qty, price, amount, tax_pct, line_vat}

    for _, row in cart_df.iterrows():
        # get name, qty, price, line_total safely
        name = str(row.get("name", "")).strip()
        try:
            qty = int(row.get("quantity", 0))
        except Exception:
            qty = 0

        price = safe_decimal(row.get("price", 0))

        # Determine line_total: prefer provided line_total else compute price * qty
        if "line_total" in row and row.get("line_total", None) not in (None, ""):
            line_total = safe_decimal(row.get("line_total", 0))
        else:
            line_total = (price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Determine tax percent for this line: prefer tax_percentage else fallback to tax_value>0
        if "tax_percentage" in cart_df.columns:
            tax_pct = safe_decimal(row.get("tax_percentage", 0))
        else:
            # fallback: if a tax_value column exists and > 0, treat as taxable with 18%
            tax_pct = Decimal("18") if safe_decimal(row.get("tax_value", 0)) > 0 else Decimal("0")

        # Compute line VAT only if taxable
        if tax_pct > 0:
            # VAT included in price formula: line_vat = line_total * tax_pct / (100 + tax_pct)
            denom = (Decimal("100") + tax_pct)
            try:
                raw_line_vat = (line_total * tax_pct) / denom
            except Exception:
                raw_line_vat = Decimal("0.00")
            # quantize per line to 2 decimals then add
            line_vat = raw_line_vat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            taxable_total += line_total
        else:
            line_vat = Decimal("0.00")
            non_taxable_total += line_total

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

    # deposit_total if any (some implementations store deposit separately)
    deposit_total = safe_decimal(cart_df["deposit_value"].sum()) if "deposit_value" in cart_df else Decimal("0.00")
    deposit_total = deposit_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Add deposit to totals if deposit is not already part of line totals.
    # If your cart includes deposit in line_total, DO NOT add this again.
    # Here we assume deposit_value is a separate column and should be included:
    total_dec = (total_lines_sum + deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Final tax_total quantized (sum of per-line quantized VATs)
    tax_total = tax_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Merchant/internal subtotal (for accounting) = total - VAT
    merchant_sub_total = (total_dec - tax_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if merchant_sub_total < Decimal("0.00"):
        merchant_sub_total = Decimal("0.00")

    # ---------- Build receipt lines (customer-facing) ----------
    rows = []
    rows.append("DESCRIPTION")
    rows.append("QTY   PRICE     AMOUNT")
    receipt_width = int(getattr(settings, "RECEIPT_CHAR_COUNT", 40))
    rows.append("-" * receipt_width)

    # Add items and show line VAT next to line if taxable (helps transparency)
    for r in enhanced_rows:
        name = r["name"]
        # truncate long name
        max_name_len = receipt_width - 2
        if len(name) > max_name_len:
            name = name[:max_name_len - 3] + "..."

        rows.append(name)
        # format: "qty @ price = amount" and if VAT present append " VAT: x"
        line_detail = f"{r['qty']} @ {fmt_no_sym(r['price'])} = {fmt_no_sym(r['amount'])}"
        rows.append(line_detail)
        rows.append("")

    separator = "-" * receipt_width
    cart_string = f"Transaction:{transaction_id}\n{separator}\n" + "\n".join(rows)

    # ---------- Totals block (customer-facing: Sub Total = Total paid) ----------
    total_lines = []
    total_lines.append(separator)
    # Customer-facing Sub Total: show total amount they pay (as you requested)
    total_lines.append(f"Sub Total      {fmt_no_sym(total_dec)}")
    total_lines.append(f"Tax            {fmt_no_sym(tax_total)}")
    total_lines.append(f"Total Amount   {fmt_no_sym(total_dec)}")
    total_lines.append("")

    # ---------- Payment and change/balance ----------
    try:
        value_dec = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        payment_display = fmt(value_dec)
        change_val = (value_dec - total_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        change_display = fmt(change_val)
    except (InvalidOperation, TypeError, ValueError):
        payment_display = str(value)
        change_display = fmt(Decimal("0.00"))

    total_lines.append(f"{str(payment_type).capitalize():<12}{payment_display:>{receipt_width - 12}}")
    total_lines.append(f"{'Balance':<12}{change_display:>{receipt_width - 12}}")

    total_string = "\n".join(total_lines)

    # Compose full receipt
    header = getattr(settings, "RECEIPT_HEADER", "")
    # Use transaction datetime for sale time
    transaction_dt = datetime.strptime(transaction_id[:-6], '%Y%m%d%H%M%S')
    sale_datetime_str = transaction_dt.strftime("%d/%m/%Y %H:%M")
    username_str = getattr(user, "username", "Unknown")

# Compose dynamic footer
    footer_lines = [
     getattr(settings, "RECEIPT_FOOTER", "You are Welcomed !"),  # optional welcome
     f"Sale Date: {sale_datetime_str}",
     f"Served by: {username_str}",
     ""  # blank line for spacing
]
    footer = "\n".join(footer_lines)

                     
    receipt_raw = header + "\n\n" + cart_string + "\n" + total_string + "\n\n" + footer

    # Center each line (thermal printer style you had)
    receipt = "\n".join([i.center(receipt_width) for i in receipt_raw.splitlines()])

    # ---------- Save transaction (DB uses merchant_sub_total for accounting) ----------
    try:
        obj = transaction.objects.create(
            transaction_id=transaction_id,
            transaction_dt=datetime.strptime(transaction_id[:-6], '%Y%m%d%H%M%S'),
            user=user,
            total_sale=total_dec,            # VAT-inclusive (what customer paid)
            sub_total=merchant_sub_total,    # merchant/internal subtotal = total - VAT
            tax_total=tax_total,             # extracted VAT (sum of per-line VAT)
            deposit_total=deposit_total,
            payment_type=payment_type,
            receipt=receipt,
            products=str(cart_df.to_dict('records')),
        )
        print("Saved transaction:", obj.transaction_id)
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
