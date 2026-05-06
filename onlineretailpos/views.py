from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import os
import shutil
import pytz
import pandas as pd
import plotly.figure_factory as ff
from plotly import express as px
from plotly import offline as po

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone as dj_timezone

from cart.models import Cart, displayed_items
from inventory.models import Product
from transaction.models import productTransaction, transaction
from transaction.views import DateSelector, get_unclosed_previous_sales_date


timezone = pytz.timezone("Africa/Dar_es_Salaam")


# -----------------------------
# Currency formatting helpers
# -----------------------------
def currency_symbol():
    return getattr(settings, "CURRENCY_SYMBOL", "TZS")


def safe_decimal(value, default=Decimal("0.00")):
    try:
        if value is None or value == "":
            return default
        if isinstance(value, Decimal):
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
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


def format_if_number(x, decimals=2):
    try:
        return fmt_no_sym(x, decimals=decimals)
    except Exception:
        return x


def parse_date(value, fallback=None):
    try:
        if value:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        pass
    return fallback


def block_register_if_day_not_closed(request):
    """
    If there is any previous sales day not closed,
    block selling and redirect to End Day page.
    """
    unclosed_date = get_unclosed_previous_sales_date()
    if unclosed_date:
        messages.error(
            request,
            f"Sales are blocked. Please close day {unclosed_date} before selling today."
        )
        return redirect(f"/end-day/?date={unclosed_date}")
    return None


# -----------------------------
# Forms
# -----------------------------
class EnterBarcode(forms.Form):
    barcode = forms.CharField(
        widget=forms.TextInput(
            attrs={
                "autofocus": "autofocus",
                "autocomplete": "off",
                "style": "width:100%",
            }
        ),
        max_length=32,
    )

    qty = forms.IntegerField(
        label="Quantity",
        widget=forms.TextInput(attrs={"style": "width:100%"}),
    )


# -----------------------------
# Register / POS Screen
# -----------------------------
@login_required(login_url="/user/login/")
def register(request):
    block_response = block_register_if_day_not_closed(request)
    if block_response:
        return block_response

    form = EnterBarcode(initial={"qty": 1})

    if request.method == "POST":
        form = EnterBarcode(request.POST)
        if form.is_valid():
            barcode = form.cleaned_data["barcode"]
            qty = form.cleaned_data["qty"]
            return redirect(f"/cart/add/{barcode}/{qty}")

    cart = Cart(request)

    total = Decimal("0.00")
    tax_total = Decimal("0.00")

    for _, item in cart.items:
        line = safe_decimal(item.get("line_total", 0))
        tax = safe_decimal(item.get("tax_value", 0))

        total += line
        tax_total += tax

    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax_total = tax_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    stock_error = request.session.pop("stock_error", None)

    context = {
        "form": form,
        "no_product": True if "ProductNotFound" in request.path else False,
        "cart": cart,
        "total": float(total),
        "tax_total": float(tax_total),
        "total_display": fmt(total),
        "tax_total_display": fmt(tax_total),
        "currency": currency_symbol(),
        "displayed_items": displayed_items.objects.all(),
        "stock_error": stock_error,
    }

    request.session["Total"] = float(total)
    request.session["Tax_Total"] = float(tax_total)
    request.session.modified = True

    return render(request, "retailScreen.html", context=context)


# -----------------------------
# Customer Display Screen
# -----------------------------
@login_required(login_url="/user/login/")
def retail_display(request, values=None):
    if values:
        try:
            cart_key = getattr(settings, "CART_SESSION_ID", "cart")
            cart = request.session.get(cart_key, {})

            if not cart or len(cart) == 0:
                return HttpResponse("IMAGE")

            total = Decimal("0.00")
            for value in cart.values():
                total += safe_decimal(value.get("line_total", 0))

            total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            response = f"""
            <div class="card shadow-sm p-0 m-0" style="width:100%;height:95%">
                <div class="card-header p-0">
                    <table class="table p-0 m-0" style="text-align:right;">
                        <tr>
                            <th style="font-family:bold;color:rgba(0,0,0,.623);width:40%">Barcode/Name</th>
                            <th style="font-family:bold;color:rgba(0,0,0,.623)">Qty</th>
                            <th style="font-family:bold;color:rgba(0,0,0,.623)">Price</th>
                            <th style="font-family:bold;color:rgba(0,0,0,.623)">L-Total<br>Tax</th>
                            <th style="font-family:bold;color:rgba(0,0,0,.623)">L-Total<br>Deposit</th>
                            <th style="font-family:bold;color:rgba(0,0,0,.623)">Line<br>Total</th>
                        </tr>
                    </table>
                </div>
                <div id="table-body" class="card-body" style="overflow:auto;padding:0;">
                    <table class="table p-0 m-0" style="text-align:right;">
            """

            for key, value in cart.items():
                qty = value.get("quantity", "")
                name = value.get("name", "")
                price_display = fmt(value.get("price", 0))
                tax_display = fmt(value.get("tax_value", 0))
                deposit_display = fmt(value.get("deposit_value", 0))
                line_total_display = fmt(value.get("line_total", 0))

                response += f"""
                    <tr>
                        <th style="text-align:left">{key}<br>{name}</th>
                        <td>{qty}</td>
                        <td>{price_display}</td>
                        <td>{tax_display}</td>
                        <td>{deposit_display}</td>
                        <td>{line_total_display}</td>
                    </tr>
                """

            response += f"""
                    </table>
                </div>
                <div class="card-footer py-3">
                    <h1 class="m-0 font-weight-bold text-primary">
                        Transaction Total:
                        <span class="m-0 font-weight-bold text-dark" style="float:right;text-align:right">
                            {fmt(total)}
                        </span>
                    </h1>
                </div>
            </div>
            """

            return HttpResponse(response)

        except Exception as e:
            print("retail_display error:", e)
            return HttpResponse("")

    path = "images4display/"

    if os.path.exists(f"./{path}"):
        shutil.copytree(f"./{path}", f"{settings.STATIC_ROOT}/{path}", dirs_exist_ok=True)

    img_list = []
    if os.path.exists(path):
        img_list = [path + i for i in os.listdir(path) if not i.endswith(".md")]

    return render(
        request,
        "retailDisplay.html",
        context={
            "store_name": settings.STORE_NAME,
            "display_images": img_list,
        },
    )


# -----------------------------
# Department Regular Report
# -----------------------------
@login_required(login_url="/user/login/")
def report_regular(request, start_date, end_date):
    start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

    qs = productTransaction.objects.filter(
        transaction_date_time__date__range=(start_date, end_date)
    ).order_by("-transaction_date_time")

    df = pd.DataFrame(qs.values())

    if not df.shape[0]:
        return redirect("/")

    df["transaction_date_time"] = df["transaction_date_time"].apply(lambda x: x.astimezone(timezone))
    df["date"] = df["transaction_date_time"].dt.date

    # IMPORTANT:
    # sales_price is VAT-inclusive, so do NOT add tax_amount again.
    df["total_sales"] = (df["qty"] * df["sales_price"]) + df["deposit_amount"]
    df["total_pre_sales"] = df["total_sales"] - df["tax_amount"]

    group_cols = ["date", "department", "payment_type"]
    sum_cols = ["qty", "total_pre_sales", "tax_amount", "deposit_amount", "total_sales"]

    date_group = df.groupby(group_cols)[sum_cols].sum()

    table = date_group.reset_index().groupby(["date"])[sum_cols].sum()
    for i, val in table.iterrows():
        date_group.loc[(i, " Day Total", "")] = val

    table = date_group.reset_index().groupby(["date", "department"])[sum_cols].sum()
    for i, val in table.iterrows():
        if i[1] == " Day Total":
            continue
        date_group.loc[(i[0], i[1], " Department Total ")] = val

    date_group.loc[("TOTAL", "TOTAL", " TOTAL")] = df[sum_cols].sum()

    for i, val in df.groupby("payment_type")[sum_cols].sum().iterrows():
        date_group.loc[("TOTAL", "TOTAL", i)] = val

    date_group = date_group.sort_index()
    date_group.fillna("", inplace=True)

    date_group.rename(
        columns={
            "qty": "Quantity",
            "total_pre_sales": "Total Pre_Sales",
            "tax_amount": "Total Tax",
            "deposit_amount": "Total Deposit",
            "total_sales": "Total Sales",
        },
        inplace=True,
    )

    date_group.index.names = ["Date", "Department", "Payment Type"]

    date_group_formatted = date_group.copy()
    for col in ["Total Pre_Sales", "Total Tax", "Total Deposit", "Total Sales"]:
        if col in date_group_formatted.columns:
            date_group_formatted[col] = date_group_formatted[col].apply(lambda x: format_if_number(x))

    return render(
        request,
        "reportsRegular.html",
        context={
            "table_html": date_group_formatted.to_html(
                classes="table table-bordered table-hover h6 text-gray-900 border-5"
            ),
            "start_date": start_date,
            "end_date": end_date,
            "store_name": settings.STORE_NAME,
        },
    )


# -----------------------------
# Product Dashboard
# -----------------------------
@login_required(login_url="/user/login/")
def dashboard_products(request):
    try:
        number = 10
        context = {}

        today = dj_timezone.localtime(dj_timezone.now()).date()
        last_30_date = today - timedelta(days=30)

        qs = productTransaction.objects.filter(
            transaction_date_time__date__range=(last_30_date, today)
        ).order_by("-transaction_date_time")

        df = pd.DataFrame(qs.values())

        context["products_group"] = {}

        if df.shape[0]:
            for department, df_group in df.groupby("department"):
                context["products_group"][department] = (
                    df_group.groupby(["barcode", "name"])[["qty"]]
                    .sum()
                    .reset_index()
                    .sort_values(by=["qty"], ascending=False)
                    .iloc[:number]
                    .to_dict("records")
                )

        context["low_inventory_products"] = Product.objects.all().order_by("qty").values(
            "barcode", "name", "qty"
        )[:50]
        context["number"] = number

    except Exception as e:
        print("dashboard_products error:", e)
        return redirect("/register/")

    return render(request, "productsDashboard.html", context=context)


# -----------------------------
# Department Dashboard
# -----------------------------
@login_required(login_url="/user/login/")
def dashboard_department(request):
    context = {}

    today = dj_timezone.localtime(dj_timezone.now()).date()

    start_date = parse_date(request.GET.get("start_date"), fallback=today)
    end_date = parse_date(request.GET.get("end_date"), fallback=today)

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    form = DateSelector(initial={"start_date": start_date, "end_date": end_date})

    qs = productTransaction.objects.filter(
        transaction_date_time__date__range=(start_date, end_date)
    ).order_by("-transaction_date_time")

    df = pd.DataFrame(qs.values())

    if df.shape[0]:
        # IMPORTANT:
        # sales_price is VAT-inclusive, so do NOT add tax_amount again.
        df["total_sales"] = (df["qty"] * df["sales_price"]) + df["deposit_amount"]
        df["total_pre_sales"] = df["total_sales"] - df["tax_amount"]

        sales_by_payment = df.groupby("payment_type")["total_sales"].sum()

        table_values = [
            [
                "Total QTY",
                "Total Sales Before Tax",
                "Total Tax",
                "Total Deposit",
            ] + [f"Sales by {i}" for i in sales_by_payment.index.to_list()],
            [
                df["qty"].sum(),
                df["total_pre_sales"].sum(),
                df["tax_amount"].sum(),
                df["deposit_amount"].sum(),
            ] + sales_by_payment.to_list(),
        ]

        table_values = [("TOTAL SALES", round(df["total_sales"].sum(), 2))] + list(
            zip(table_values[0], table_values[1])
        )

        table_fig = ff.create_table(table_values, height_constant=25)
        table_fig.update_layout(margin=dict(b=10, t=0, l=0, r=0), height=275)

        context["table_fig"] = po.plot(
            table_fig,
            auto_open=False,
            output_type="div",
            config={"displayModeBar": False},
            include_plotlyjs=False,
        )

        pie_fig = px.pie(
            values=sales_by_payment,
            names=sales_by_payment.index,
            color=sales_by_payment.index,
            color_discrete_map={
                "CASH": "darkgreen",
                "EBT": "royalblue",
                "DEBIT/CREDIT": "darkslategray",
                "DEBT": "orange",
            },
        )
        pie_fig.update_layout(
            margin=dict(b=50, t=10, l=10, r=10),
            height=225,
            title={
                "text": f"Date Period : ({start_date:%Y/%m/%d} - {end_date:%Y/%m/%d})",
                "font_size": 16,
                "y": 0.15,
                "x": 0.5,
                "xanchor": "center",
                "yanchor": "top",
            },
        )
        pie_fig.update_traces(hovertemplate=None)

        context["pie_fig"] = po.plot(
            pie_fig,
            auto_open=False,
            output_type="div",
            config={"displayModeBar": False},
            include_plotlyjs=False,
        )

        sales_by_department = (
            df.groupby(["department", "payment_type"])[
                ["qty", "total_pre_sales", "tax_amount", "deposit_amount", "total_sales"]
            ]
            .sum()
            .reset_index()
        )

        bar_fig = px.bar(
            sales_by_department,
            x="department",
            y="total_sales",
            color="payment_type",
            text_auto=True,
            hover_name="total_sales",
            hover_data={
                "qty": True,
                "total_pre_sales": True,
                "tax_amount": True,
                "deposit_amount": True,
                "total_sales": False,
            },
            labels={
                "qty": "Quantity",
                "payment_type": "Payment Type",
                "department": "Department",
                "total_sales": f"Total Sales ({currency_symbol()})",
                "total_pre_sales": "Total Sales Before Tax",
                "tax_amount": "Total Tax Amount",
                "deposit_amount": "Total Deposit Amount",
            },
            color_discrete_map={
                "CASH": "darkgreen",
                "EBT": "royalblue",
                "DEBIT/CREDIT": "darkslategray",
                "DEBT": "orange",
            },
        )

        bar_fig.update_yaxes(title=f"Total Sales ({start_date:%Y/%m/%d} - {end_date:%Y/%m/%d})")
        bar_fig.update_layout(margin=dict(b=10, pad=0, t=10, l=10, r=10), height=500, showlegend=False)

        context["bar_fig"] = po.plot(
            bar_fig,
            auto_open=False,
            output_type="div",
            config={"displayModeBar": False},
            include_plotlyjs=False,
        )
    else:
        context["table_fig"] = "<div class='text-muted p-3'>No data found for selected date range.</div>"
        context["pie_fig"] = "<div class='text-muted p-3'>No payment data found.</div>"
        context["bar_fig"] = "<div class='text-muted p-3'>No department sales data found.</div>"

    context["report_link"] = f"/department_report/{start_date}/{end_date}/"
    context["form"] = form
    context["start_date"] = start_date
    context["end_date"] = end_date
    context["currency"] = currency_symbol()

    return render(request, "departmentDashboard.html", context=context)


# -----------------------------
# Sales Dashboard
# -----------------------------
@login_required(login_url="/user/login/")
def dashboard_sales(request):
    context = {}

    today = dj_timezone.localtime(dj_timezone.now()).date()
    today_dt = datetime.combine(today, datetime.min.time())

    try:
        qs = transaction.objects.filter(
            transaction_dt__date__gte=datetime(today.year, 1, 1)
        ).values()

        df = pd.DataFrame(qs)

        if not df.shape[0]:
            context["today_total_sales"] = Decimal("0.00")
            context["add_info"] = {
                "Yesterday's Total Sales": fmt(0),
                "Last 7 Days Avg Sales": fmt(0),
                "WTD Total Sales": fmt(0),
                "Last Week Total Sales": fmt(0),
                "MTD Total Sales": fmt(0),
                "YTD Total Sales": fmt(0),
            }
            context["30_Days_Avg_Sales"] = fmt(0)
            context["30_Days_Total_Sales"] = fmt(0)
            context["30_day_sales_graph"] = ""
            context["day_payment_graph"] = ""
            return render(request, "salesDashboard.html", context=context)

        df["transaction_dt"] = df["transaction_dt"].apply(lambda x: x.astimezone(timezone))
        df["date"] = df["transaction_dt"].dt.date

        df_date = df.groupby("date")["total_sale"].sum()
        df_date.index = pd.to_datetime(df_date.index)

        year_start = datetime(today.year, 1, 1)
        if year_start not in df_date.index:
            df_date.loc[year_start] = 0

        if today_dt not in df_date.index:
            df_date.loc[today_dt] = 0

        df_date = df_date.sort_index().asfreq("D", fill_value=0)

        context["today_total_sales"] = df_date.get(today_dt, 0)

        context["add_info"] = {
            "Yesterday's Total Sales": fmt(df_date.get(today_dt - timedelta(days=1), 0)),
            "Last 7 Days Avg Sales": fmt(df_date[df_date.index > today_dt - timedelta(days=7)].sum() / 7),
            "WTD Total Sales": fmt(df_date.resample("W").sum().iloc[-1] if len(df_date.resample("W").sum()) else 0),
            "Last Week Total Sales": fmt(df_date.resample("W").sum().iloc[-2] if len(df_date.resample("W").sum()) > 1 else 0),
            "MTD Total Sales": fmt(df_date.resample("M").sum().iloc[-1] if len(df_date.resample("M").sum()) else 0),
            "YTD Total Sales": fmt(df_date.resample("Y").sum().iloc[-1] if len(df_date.resample("Y").sum()) else 0),
        }

        context["30_Days_Avg_Sales"] = fmt(df_date[df_date.index > today_dt - timedelta(days=30)].mean())
        context["30_Days_Total_Sales"] = fmt(df_date[df_date.index > today_dt - timedelta(days=30)].sum())

        fig = px.bar(
            x=df_date.index,
            y=df_date,
            text_auto=True,
            barmode="group",
            template="plotly_white",
            labels={"x": "Date", "y": f"Total Sales ({currency_symbol()})"},
        )
        fig.update_xaxes(title="Days", tickformat="%a,%d/%m", tickangle=-90)
        fig.update_yaxes(title="Total Sales")
        fig.update_layout(margin=dict(b=10, pad=0, t=10, r=0, l=0))

        context["30_day_sales_graph"] = po.plot(
            fig,
            auto_open=False,
            output_type="div",
            config={"displayModeBar": False},
            include_plotlyjs=False,
        )

        df_day_payment = (
            df[df["date"] == today]
            .groupby("payment_type")["total_sale"]
            .sum()
            .reset_index()
        )

        fig2 = px.pie(
            df_day_payment,
            values="total_sale",
            names="payment_type",
            template="plotly_white",
            height=195,
            labels={"payment_type": "Payment Type", "total_sale": "Total Sales"},
        )
        fig2.update_layout(margin=dict(b=10, pad=0, t=10))

        context["day_payment_graph"] = po.plot(
            fig2,
            auto_open=False,
            output_type="div",
            config={"displayModeBar": False},
            include_plotlyjs=False,
        )

    except Exception as e:
        print("dashboard_sales error:", e)
        return redirect("/register/")

    return render(request, "salesDashboard.html", context=context)


# -----------------------------
# Auth
# -----------------------------
def user_login(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(username=username, password=password)

        if user is not None:
            login(request, user)
            request.session["Total"] = 0.00
            request.session["Tax_Total"] = 0.00
            request.session.modified = True
            return redirect("home")

        return render(
            request,
            "registration/login.html",
            context={
                "error": True,
                "store_name": settings.STORE_NAME,
            },
        )

    return render(
        request,
        "registration/login.html",
        context={
            "store_name": settings.STORE_NAME,
        },
    )


@login_required(login_url="/user/login/")
def user_logout(request):
    logout(request)
    return render(
        request,
        "registration/login.html",
        context={
            "logout": True,
        },
    )