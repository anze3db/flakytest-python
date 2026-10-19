from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404, render

from shop.models import Author, Book


def books(request):
    books = Book.objects.select_related("author").annotate(n=Count("reviews"), avg=Avg("reviews__stars"))
    return render(request, "shop/books.html", {"books": books})


def author(request, pk):
    author = get_object_or_404(Author, pk=pk)
    return render(request, "shop/author.html", {"author": author, "books": author.books.prefetch_related("reviews")})
