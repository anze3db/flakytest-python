from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from shop.models import Author, Book, Review


def make_catalog(authors=5, books=8, reviews=4):
    for a in range(authors):
        author = Author.objects.create(name=f"Author {a}")
        for b in range(books):
            book = Book.objects.create(author=author, title=f"book {a}-{b}", price=Decimal("9.99") + b)
            Review.objects.bulk_create(Review(book=book, stars=(r % 5) + 1, text="good " * 20) for r in range(reviews))


def make_cases(n):
    def decorate(cls):
        for i in range(n):
            setattr(cls, f"test_{i:03d}", lambda self, i=i: self.check(i))
        return cls

    return decorate


@make_cases(300)
class TestPages(TestCase):
    def setUp(self):
        make_catalog()
        self.user = User.objects.create_user("reader", password="secret")
        self.client.login(username="reader", password="secret")

    def check(self, i):
        response = self.client.get("/")
        self.assertContains(response, "Book 0-0")
        author = Author.objects.order_by("id")[i % 5]
        response = self.client.get(f"/authors/{author.pk}/")
        self.assertContains(response, author.name)


@make_cases(300)
class TestQueries(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_catalog(authors=10)

    def check(self, i):
        expensive = Book.objects.filter(price__gte=Decimal("12")).select_related("author")
        self.assertEqual(len(list(expensive)), 50)
        for book in Book.objects.prefetch_related("reviews")[: 10 + i % 10]:
            self.assertEqual(len(book.reviews.all()), 4)
        Review.objects.filter(book__author__name="Author 1").update(stars=5)
        self.assertEqual(Review.objects.filter(stars=5).count() >= 32, True)
