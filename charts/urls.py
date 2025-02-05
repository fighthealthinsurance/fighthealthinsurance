from django.urls import path

from .views import signups_by_day, users_by_day, sf_signups

urlpatterns = [
    path('signups_by_day', signups_by_day, name='signups_by_day'),
    path('users_by_day', users_by_day, name='users_by_day'),
    path('sf_signups', sf_signups, name='sf_signups'),
]
