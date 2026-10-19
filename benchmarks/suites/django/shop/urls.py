from django.urls import path

from shop import views

urlpatterns = [path("", views.books), path("authors/<int:pk>/", views.author)]
