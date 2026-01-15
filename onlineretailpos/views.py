from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django import forms
from cart.models import Cart, displayed_items
from inventory.models import Product
from transaction.models import productTransaction, transaction
from transaction.views import DateSelector
from plotly import express as px
from plotly import offline as po
import plotly.figure_factory as ff
from datetime import datetime, timedelta
import pandas as pd
import pytz, os, shutil
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone as dj_timezone
today_date = dj_timezone.localtime(dj_timezone.now()).date()

timezone = pytz.timezone("Africa/Dar_es_Salaam")


# -----------------------------
# Currency formatting helpers
# -----------------------------
def currency_symbol():
    return getattr(settings, "CURRENCY_SYMBOL", "TZS")


def fmt(amount, decimals=2):
    """
    Format a numeric amount with commas and currency symbol.
    Example: fmt(123456.5) -> "TZS 123,456.50"
    """
    try:
        a = float(amount)
    except Exception:
        a = 0.0
    formatted = format(round(a, decimals), f",.{decimals}f")
    return f"{currency_symbol()} {formatted}"


def fmt_no_sym(amount, decimals=2):
    """Return just the number with commas (no currency symbol)."""
    try:
        a = float(amount)
    except Exception:
        a = 0.0
    return format(round(a, decimals), f",.{decimals}f")


def format_if_number(x, decimals=2):
    """Try to format x as number with commas; else return original."""
    try:
        return fmt_no_sym(float(x), decimals=decimals)
    except Exception:
        return x


# -----------------------------
# Forms
# -----------------------------
class EnterBarcode(forms.Form):
    barcode = forms.CharField(
        widget=forms.TextInput(attrs={'autofocus': "autofocus", ' autocomplete': "off", 'style': "width:100%"}),
        max_length=32)
    qty = forms.IntegerField(label="Quantity", widget=forms.TextInput(attrs={'style': "width:100%"}))


# -----------------------------
# Views
# -----------------------------




@login_required(login_url="/user/login/")
def register(request):
    # Barcode form (same as before)
    form = EnterBarcode(initial={'qty': 1})
    if request.method == "POST":
        form = EnterBarcode(request.POST)
        if form.is_valid():
            return redirect(f"/cart/add/{form.cleaned_data['barcode']}/{form.cleaned_data['qty']}")

    # Build / load cart (use Cart wrapper which handles session)
    cart = Cart(request)

    # helper to safely convert various types to Decimal
    def safe_dec(v):
        try:
            if v is None or v == "":
                return Decimal("0.00")
            if isinstance(v, Decimal):
                return v
            return Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0.00")

    # Compute totals from cart items (cart.items returns list of (barcode, dict))
    total = Decimal("0.00")
    tax_total = Decimal("0.00")
    for _, item in cart.items:
        # item['line_total'] and item['tax_value'] from the Cart implementation are already Decimal-compatible,
        # but we coerce safely here for robustness.
        line = safe_dec(item.get("line_total", 0))
        tax = safe_dec(item.get("tax_value", 0))
        total += line
        tax_total += tax

    # Quantize and convert to float/format as your templates expect
    total = total.quantize(Decimal("0.01"))
    tax_total = tax_total.quantize(Decimal("0.01"))

    # Pop any stock error set by cart/views and pass it once to the template
    stock_error = request.session.pop("stock_error", None)

    context = {
        "form": form,
        "no_product": True if "ProductNotFound" in request.path else False,
        "cart": cart,
        "total": float(total),                # keep numeric for templates / JS
        "tax_total": float(tax_total),
        "total_display": fmt(float(total)),
        "tax_total_display": fmt(float(tax_total)),
        "currency": currency_symbol(),
        "displayed_items": displayed_items.objects.all(),
        "stock_error": stock_error,           # template will show and play sound if present
    }

    # keep session copies if other code expects them (preserve your previous behavior)
    request.session["Total"] = float(total)
    request.session["Tax_Total"] = float(tax_total)
    request.session.modified = True

    return render(request, "retailScreen.html", context=context)



@login_required(login_url="/user/login/")
def retail_display(request, values=None):
    if values:
        try:
            cart = request.session[settings.CART_SESSION_ID]

            if len(cart) == 0:
                return HttpResponse("IMAGE")

            total = round(pd.DataFrame(cart).T["line_total"].astype(float).sum(), 2)
            # Build HTML response with formatted numbers
            response = f"""<div class="card shadow-sm p-0 m-0" style="width:100%;height:95%">
                    <div class="card-header p-0" >
                        <table class="table p-0 m-0" style="text-align:right;">
                            <tr>
                                <th style="font-family: bold;color:rgba(0, 0, 0, 0.623); width:40%" >Barcode/Name</th>
                                <th style="font-family: bold;color:rgba(0, 0, 0, 0.623)">Qty</th>
                                <th style="font-family: bold;color:rgba(0, 0, 0, 0.623)">Price</th>
                                <th style="font-family: bold;color:rgba(0, 0, 0, 0.623)">L-Total<br>Tax</th>
                                <th style="font-family: bold;color:rgba(0, 0, 0, 0.623)">L-Total<br>Deposit</th>
                                <th style="font-family: bold;color:rgba(0, 0, 0, 0.623)">Line<br>Total</th>
                            </tr>
                        </table>
                    </div>
                    <div id="table-body" class="card-body" style="overflow: auto ;padding:0;">
                        <table class="table p-0 m-0" style="text-align:right;">
                """
            if cart:
                for key, value in cart.items():
                    # value fields may be strings; safely parse
                    qty = value.get('quantity', '')
                    price_raw = value.get('price', 0)
                    tax_raw = value.get('tax_value', 0)
                    deposit_raw = value.get('deposit_value', 0)
                    line_total_raw = value.get('line_total', 0)

                    try:
                        price_display = fmt(price_raw)
                    except:
                        price_display = f"{currency_symbol()} {price_raw}"

                    try:
                        tax_display = fmt(tax_raw)
                    except:
                        tax_display = f"{currency_symbol()} {tax_raw}"

                    try:
                        deposit_display = fmt(deposit_raw)
                    except:
                        deposit_display = f"{currency_symbol()} {deposit_raw}"

                    try:
                        line_total_display = fmt(line_total_raw)
                    except:
                        line_total_display = f"{currency_symbol()} {line_total_raw}"

                    response = response + f"""<tr>
                                <th style="text-align:left">{key} <br> {value.get('name','')}</th> 
                                <td>{qty}</td>
                                <td>{price_display}</td>
                                <td>{tax_display}</td>
                                <td>{deposit_display}</td>
                                <td>{line_total_display}</td>
                            </tr> """
            response = response + f"""</table> </div> 
                                        <div class="card-footer py-3">
                                            <h1 class="m-0 font-weight-bold text-primary">Transaction Total:
                                            <span class="m-0 font-weight-bold text-dark" style="float:right;item-align:right">{fmt(total)}</span>
                                            </h1>
                                        </div>
                                    </div>"""
            return HttpResponse(response)
        except Exception as e:
            print("retail_display error:", e)
            return HttpResponse("")

    path = "images4display/"  # insert the path to your directory   
    if os.path.exists(f"./{path}"):
        shutil.copytree(f"./{path}", f"{settings.STATIC_ROOT}/{path}", dirs_exist_ok=True)
    img_list = [path + i for i in os.listdir(path) if not i.endswith('.md')]

    return render(request, 'retailDisplay.html', context={"store_name": settings.STORE_NAME, "display_images": img_list})


@login_required(login_url="/user/login/")
def report_regular(request, start_date, end_date):
    start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    df = pd.DataFrame(productTransaction.objects.filter(transaction_date_time__date__range=(start_date, end_date)).order_by('-transaction_date_time').values())
    if not df.shape[0]:
        return redirect("/")

    df['transaction_date_time'] = df['transaction_date_time'].apply(lambda x: x.astimezone(timezone))
    df['date'] = df['transaction_date_time'].dt.date
    df['total_sales'] = (df['qty'] * df['sales_price']) + df['tax_amount'] + df['deposit_amount']
    df['total_pre_sales'] = df['qty'] * df['sales_price']

    date_group = df.groupby(['date', 'department', 'payment_type'])[['qty', 'total_pre_sales', 'tax_amount', 'deposit_amount', 'total_sales']].apply(lambda x: x.sum())
    table = date_group.reset_index().groupby(['date'])[['total_pre_sales', 'tax_amount', 'deposit_amount', 'total_sales']].apply(lambda x: x.sum())
    for i, val in table.iterrows():
        date_group.loc[(i, " Day Total", "")] = val
    table = date_group.reset_index().groupby(['date', 'department'])[['qty', 'total_pre_sales', 'tax_amount', 'deposit_amount', 'total_sales']].apply(lambda x: x.sum())
    for i, val in table.iterrows():
        if i[1] == " Day Total":
            continue
        date_group.loc[(i[0], i[1], " Department Total ")] = val

    date_group.loc[("TOTAL", "TOTAL", " TOTAL")] = df[['total_pre_sales', 'tax_amount', 'deposit_amount', 'total_sales']].apply(lambda x: x.sum())
    for i, val in df.groupby('payment_type')[['total_pre_sales', 'tax_amount', 'deposit_amount', 'total_sales']].apply(lambda x: x.sum()).iterrows():
        date_group.loc[("TOTAL", "TOTAL", i)] = val

    date_group = date_group.sort_index()
    date_group.fillna("", inplace=True)
    date_group.rename(columns={'qty': 'Quantity', 'total_pre_sales': 'Total Pre_Sales', 'tax_amount': 'Total Tax',
                               'deposit_amount': 'Total Deposit', 'total_sales': 'Total Sales'}, inplace=True)
    date_group.index.names = ['Date', 'Department', 'Payment Type', ]

    # Format numeric columns into comma-separated strings (no currency text inside table, or add currency symbol if you want)
    date_group_formatted = date_group.copy()
    for col in ['Total Pre_Sales', 'Total Tax', 'Total Deposit', 'Total Sales']:
        if col in date_group_formatted.columns:
            date_group_formatted[col] = date_group_formatted[col].apply(lambda x: format_if_number(x))

    return render(request, "reportsRegular.html", context={
        "table_html": date_group_formatted.to_html(classes="table table-bordered table-hover h6 text-gray-900 border-5"),
        "start_date": start_date, "end_date": end_date, "store_name": settings.STORE_NAME,
    })


@login_required(login_url="/user/login/")
def dashboard_products(request):
    try:
        number = 10
        context = {}
        today_date = datetime.now().date()
        last_30_date = datetime.now().date() - timedelta(30)
        df = pd.DataFrame(productTransaction.objects.filter(transaction_date_time__date__range=(last_30_date, today_date)).order_by('-transaction_date_time').values())
        context['products_group'] = {}
        for i, df_group in df.groupby('department'):
            context['products_group'][i] = df_group.groupby(["barcode", "name"])[["qty"]].sum().reset_index().sort_values(by=["qty"], ascending=False).iloc[:number].to_dict('records')

        context['low_inventory_products'] = Product.objects.all().order_by('qty').values('barcode', 'name', 'qty')[:50]
        context['number'] = number
    except:
        return redirect("/register/")
    return render(request, "productsDashboard.html", context=context)


@login_required(login_url="/user/login/")
def dashboard_department(request):
    context = {}
    end_date = datetime.now().date()
    start_date = datetime.now().date()
    form = DateSelector(initial={'end_date': end_date, 'start_date': start_date})
    if request.method == "POST":
        form = DateSelector(request.POST)
        if form.is_valid():
            end_date = form.cleaned_data['end_date']
            start_date = form.cleaned_data['start_date']
    df = pd.DataFrame(productTransaction.objects.filter(transaction_date_time__date__range=(start_date, end_date)).order_by('-transaction_date_time').values())
    if df.shape[0]:
        df['total_sales'] = (df['qty'] * df['sales_price']) + df['tax_amount'] + df['deposit_amount']
        df['total_pre_sales'] = df['qty'] * df['sales_price']
        sales_by_payment = df.groupby('payment_type')['total_sales'].sum()

        tableValues = [['Total QTY', 'Total Sales b4 Tax & Deposit', 'Total Tax', 'Total Deposit'] + [f"Sales by {i}" for i in sales_by_payment.index.to_list()],
                       [df['qty'].sum(), df['total_pre_sales'].sum(), df['tax_amount'].sum(), df['deposit_amount'].sum()] + sales_by_payment.to_list()]
        tableValues = [("TOTAL SALES", round(df['total_sales'].sum(), 2))] + list(zip(tableValues[0], tableValues[1]))
        table_fig = ff.create_table(tableValues, height_constant=25,)
        table_fig.update_layout(margin=dict(b=10, t=0, l=0, r=0), height=275,)
        context['table_fig'] = po.plot(table_fig, auto_open=False, output_type='div', config={'displayModeBar': False}, include_plotlyjs=False)

        pie_fig = px.pie(values=sales_by_payment, names=sales_by_payment.index, color=sales_by_payment.index,
                         color_discrete_map={'CASH': "darkgreen", 'EBT': "royalblue", 'DEBIT/CREDIT': "darkslategray"})
        pie_fig.update_layout(margin=dict(b=50, t=10, l=10, r=10), height=225,
                              title={'text': f"Date Period : ({start_date:%Y/%m/%d} - {end_date:%Y/%m/%d})", 'font_size': 16,
                                     'y': 0.15, 'x': 0.5, 'xanchor': 'center', 'yanchor': 'top'})
        pie_fig.update_traces(hovertemplate=None)
        context['pie_fig'] = po.plot(pie_fig, auto_open=False, output_type='div', config={'displayModeBar': False}, include_plotlyjs=False)

        sales_by_department = df.groupby(['department', 'payment_type'])[['qty', 'total_pre_sales', 'tax_amount', 'deposit_amount', 'total_sales']].apply(lambda x: x.sum())
        sales_by_department = sales_by_department.reset_index()

        bar_fig = px.bar(sales_by_department, x="department", y="total_sales", color="payment_type", text_auto=True, hover_name="total_sales",
                         hover_data={'qty': True, 'total_pre_sales': True, 'tax_amount': True, 'deposit_amount': True, 'total_sales': False,},
                         labels={'qty': "Quantity", 'payment_type': "Payment Type", 'department': "Department", 'total_sales': f"Total Sales ({currency_symbol()})", "total_pre_sales": "Total Sales b4 Tax & Deposit",
                                 'tax_amount': "Total Tax Amount", 'deposit_amount': "Total Deposit Amount"},
                         color_discrete_map={'CASH': "darkgreen", 'EBT': "royalblue", 'DEBIT/CREDIT': "darkslategray"})
        bar_fig.update_yaxes(title=f"Total Sales ({start_date:%Y/%m/%d} - {end_date:%Y/%m/%d})")
        bar_fig.update_layout(margin=dict(b=10, pad=0, t=10, l=10, r=10), height=500, showlegend=False)

        context['bar_fig'] = po.plot(bar_fig, auto_open=False, output_type='div', config={'displayModeBar': False}, include_plotlyjs=False)

    context["report_link"] = f"/department_report/{start_date}/{end_date}/"
    context['form'] = form
    return render(request, "departmentDashboard.html", context=context)


@login_required(login_url="/user/login/")
def dashboard_sales(request):
    context = {}
    today_date = datetime.combine(datetime.now().date(), datetime.min.time())
    try:
        df = pd.DataFrame(transaction.objects.filter(transaction_dt__date__gte=datetime(today_date.year, 1, 1)).values())
        df['transaction_dt'] = df['transaction_dt'].apply(lambda x: x.astimezone(timezone))
        df['date'] = df['transaction_dt'].dt.date
        df_date = df.groupby('date')['total_sale'].sum()
        df_date.index = pd.to_datetime(df_date.index)
        if not df_date.get(datetime(today_date.year, 1, 1)): df_date[datetime(today_date.year, 1, 1)] = 0
        if not df_date.get(today_date): df_date[today_date] = 0
        df_date = df_date.asfreq('D', fill_value=0)

        context['today_total_sales'] = df_date.get(today_date)
        # Add display formatted values too:
        context["add_info"] = {}
        context["add_info"]['Yesterday\'s Total Sales'] = fmt(df_date.get(today_date - timedelta(1)))
        context["add_info"]['Last 7 Days Avg Sales'] = fmt(df_date[df_date.index > today_date - timedelta(7)].sum() / 7)
        context['30_Days_Avg_Sales'] = fmt(df_date[df_date.index > today_date - timedelta(30)].mean())
        context['30_Days_Total_Sales'] = fmt(df_date[df_date.index > today_date - timedelta(30)].sum())
        context["add_info"]['WTD Total Sales'] = fmt(df_date.resample('W').sum()[-1])
        context["add_info"]['Last Week Total Sales'] = fmt(df_date.resample('W').sum()[-2])
        context["add_info"]['MTD Total Sales'] = fmt(df_date.resample('M').sum()[-1])
        context["add_info"]['YTD Total Sales'] = fmt(df_date.resample('Y').sum()[-1])

        fig = px.bar(x=df_date.index, y=df_date, text_auto=True, barmode='group', template="plotly_white", labels={"x": "Date", "y": f"Total Sales ({currency_symbol()})"})
        fig.update_xaxes(title="Days", tickformat='%a,%d/%m', tickangle=-90)
        fig.update_yaxes(title="Total Sales")
        fig.update_layout(margin=dict(b=10, pad=0, t=10, r=0, l=0), )
        div = po.plot(fig, auto_open=False, output_type='div', config={'displayModeBar': False}, include_plotlyjs=False)
        context['30_day_sales_graph'] = div

        df_day_payment = df[df['date'] == today_date.date()].groupby('payment_type')['total_sale'].sum().reset_index()
        fig2 = px.pie(df_day_payment, values='total_sale', names='payment_type', template="plotly_white", height=195,
                      labels={"payment_type": "Payment Type", "total_sale": "Total Sales"})
        fig2.update_layout(margin=dict(b=10, pad=0, t=10), )
        context['day_payment_graph'] = po.plot(fig2, auto_open=False, output_type='div', config={'displayModeBar': False}, include_plotlyjs=False)
    except:
        return redirect("/register/")
    return render(request, "salesDashboard.html", context=context)


def user_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(username=username, password=password)
        if user is not None:
            login(request, user)
            request.session["Total"] = 0.00
            request.session["Tax_Total"] = 0.00
            return redirect('home')
        else:
            return render(request, 'registration/login.html', context={'error': True, "store_name": settings.STORE_NAME})
    else:
        return render(request, 'registration/login.html', context={"store_name": settings.STORE_NAME}, )


@login_required(login_url="/user/login/")
def user_logout(request):
    logout(request)
    return render(request, 'registration/login.html', context={'logout': True})
