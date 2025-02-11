from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from fighthealthinsurance.models import Appeal, User  # Corrected import
from django.shortcuts import get_object_or_404  # Added import
import json  # Added import


@csrf_exempt
def view_appeals(request):
    if request.method == "GET":
        appeals = Appeal.objects.all()
        data = [
            {"id": appeal.id, "state": appeal.state, "text": appeal.text}
            for appeal in appeals
        ]
        return JsonResponse(data, safe=False)


@csrf_exempt
def view_individual_appeal(request, appeal_id):
    if request.method == "GET":
        appeal = get_object_or_404(Appeal, id=appeal_id)
        data = {"id": appeal.id, "state": appeal.state, "text": appeal.text}
        return JsonResponse(data)


@csrf_exempt
def about_us(request):
    if request.method == "GET":
        data = {"content": "About us content goes here."}
        return JsonResponse(data)


@csrf_exempt
def subscribe_for_updates(request):
    if request.method == "POST":
        data = json.loads(request.body)
        email = data.get("email")
        # Save email to subscription list
        return JsonResponse({"status": "subscribed"})
