from django.urls import include, path

urlpatterns = [
    path("recordings/", include("stapel_recordings.urls")),
]
