from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('api/meet', views.api_submit_url, name='api_submit_url'),
    path('api/recordings/<str:fname>', views.api_get_recording, name='api_get_recording'),
    path("api/recordings/<str:fname>/delete", views.api_delete_record, name="api_delete_record"),
]