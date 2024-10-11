from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from django_celery_beat.models import PeriodicTasks
