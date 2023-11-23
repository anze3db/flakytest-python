import random
from unittest import TestCase

import pytest


class TestUnittest(TestCase):
    def set_up(self):
        pass

    def test_always_pass(self):
        self.assertTrue(True)

    def test_always_fail(self):
        self.assertTrue(False)

    def test_flaky(self):
        self.assertTrue(random.randint(0, 1) < 0.5)


def test_always_pass():
    assert True


def test_always_fail():
    print("test captured stdout")
    assert False


@pytest.mark.skip()
def test_skipped():
    assert False


def test_flaky():
    assert random.randint(0, 1) < 0.5


def raise_my_exception():
    msg = "my exception"
    raise Exception(msg)


def test_error():
    raise_my_exception()
    assert True
