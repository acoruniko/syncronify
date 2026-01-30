from django.shortcuts import render
from django.utils import timezone
from django.db.models import Case, When, Value, IntegerField
from .models import LogEvento
from django.contrib.auth.decorators import login_required

@login_required
def panel_logs(request):
    month_year = request.GET.get("month_year")
    order_by = request.GET.get("order_by", "fecha")
    direction = request.GET.get("direction", "desc")
    rotation = int(request.GET.get("rotation", "0"))  # 0=INFO, 1=WARNING, 2=ERROR
    now = timezone.now()

    if month_year:
        year, month = map(int, month_year.split("-"))
    else:
        year, month = now.year, now.month

    qs = LogEvento.objects.filter(fecha__year=year, fecha__month=month)

    if order_by == "nivel":
        # Rotación: cambia el orden según rotation
        order_map = [
            {"INFO": 1, "WARNING": 2, "ERROR": 3},
            {"WARNING": 1, "ERROR": 2, "INFO": 3},
            {"ERROR": 1, "INFO": 2, "WARNING": 3},
        ][rotation % 3]

        qs = qs.order_by(
            Case(
                *(When(nivel=k, then=Value(v)) for k, v in order_map.items()),
                output_field=IntegerField(),
            ),
            "-fecha"  # segundo criterio: fecha descendente
        )
    else:
        prefix = "" if direction == "asc" else "-"
        qs = qs.order_by(f"{prefix}{order_by}")

    return render(request, "logs/panel.html", {
        "logs": qs,
        "current_year": year,
        "current_month": month,
        "order_by": order_by,
        "direction": direction,
        "rotation": rotation,
    })
